"""Single Process Entrypoint with flock, Authenticated/Idempotent FastAPI Webhook, Multi-CLI Hub & Governed RSI."""
import asyncio
from collections import OrderedDict
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from bridge.multi_cli_hub import MultiCLIHub
from config import Settings, get_settings, load_yaml_config
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.ilink_gateway import ILinkGatewayWorker
from gateway.wechat_gateway import WeChatGateway
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from rsi.evolution_engine import EvolutionEngine
from sandbox.approval_guard import ApprovalGuard
from scheduler.code_first_cron import CodeFirstCronScheduler
from skills.skill_manager import SkillManager
from telemetry.health_monitor import HealthMonitor

logger = logging.getLogger("wechat_agy.main")
LOCK_FILE = "/tmp/agent_core/locks/main.lock"
DEFAULT_DEV_TOKEN = "dev_token_123456"
MAX_DEDUP_IDS = 1024
_lock_fd = None


def acquire_main_lock() -> Any:
    """Acquire OS-level flock to ensure global single-process execution."""
    global _lock_fd
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd = f
        return f
    except OSError:
        f.close()
        logger.warning("Another instance of autonomous_multi_cli_agent is already running.")
        return None


def compute_wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    """Compute standard WeChat SHA-1 callback signature over sorted (token, timestamp, nonce)."""
    joined = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def verify_webhook_auth(request: Request, active_settings: Settings) -> None:
    """Verify WeChat SHA-1 query signature or X-WeChat-Token bearer when configured/supplied."""
    secret = active_settings.WECHAT_TOKEN.get_secret_value()
    sig = request.query_params.get("signature") or request.headers.get("x-wechat-signature") or ""
    timestamp = request.query_params.get("timestamp") or request.headers.get("x-wechat-timestamp") or ""
    nonce = request.query_params.get("nonce") or request.headers.get("x-wechat-nonce") or ""
    header_token = request.headers.get("x-wechat-token") or ""

    must_verify = active_settings.WEBHOOK_REQUIRE_SIGNATURE or (secret and secret != DEFAULT_DEV_TOKEN)
    has_credentials = bool(sig or header_token)

    if not must_verify and not has_credentials:
        return

    if header_token and hmac.compare_digest(header_token, secret):
        return

    if sig and timestamp and nonce:
        expected = compute_wechat_signature(secret, timestamp, nonce)
        if hmac.compare_digest(sig.lower(), expected.lower()):
            return

    raise HTTPException(status_code=403, detail="Invalid or missing WeChat webhook signature.")


# Runtime wired graph
settings = get_settings()
yaml_config = load_yaml_config()
store = CoreStateStore(settings.DB_PATH)
skill_manager = SkillManager(workspace_root=settings.WORKSPACE_ROOT, settings=settings)
approval_guard = ApprovalGuard(store=store, settings=settings)
governor = WindControlGovernor(store_or_db=store, config_or_settings=settings, config=yaml_config)
crystallizer = PatternCrystallizer(store=store, skill_manager=skill_manager, settings=settings)
rsi_engine = EvolutionEngine(
    store=store,
    skill_manager=skill_manager,
    workspace_dir=str(settings.WORKSPACE_ROOT),
    config=yaml_config,
)
hub = MultiCLIHub(
    config=yaml_config,
    governor=governor,
    store=store,
    evolution_engine=rsi_engine,
    crystallizer=crystallizer,
)
monitor = HealthMonitor(governor=governor, settings=settings, store=store)
gateway = WeChatGateway(
    config=yaml_config,
    hub=hub,
    store=store,
    governor=governor,
    approval_guard=approval_guard,
    crystallizer=crystallizer,
    skill_manager=skill_manager,
    health_monitor=monitor,
    settings=settings,
    rsi_engine=rsi_engine,
)
ilink_worker: Optional[ILinkGatewayWorker] = (
    ILinkGatewayWorker(settings=settings, router=gateway) if settings.WEIXIN_TOKEN else None
)


async def _push_cron_notification(target_user: str, text: str) -> None:
    if ilink_worker is not None:
        await ilink_worker.pusher.send_raw(target_user, text)
    else:
        logger.info("[Cron Push -> %s] %s", target_user, text)


scheduler = CodeFirstCronScheduler(
    skill_manager=skill_manager,
    hub=hub,
    push_message_func=_push_cron_notification,
    settings=settings,
    rsi_engine=rsi_engine,
)
_seen_msg_ids: "OrderedDict[str, float]" = OrderedDict()


def _is_duplicate_msg(msg_id: str) -> bool:
    now = time.time()
    if msg_id in _seen_msg_ids and (now - _seen_msg_ids[msg_id]) < 600.0:
        return True
    _seen_msg_ids[msg_id] = now
    while len(_seen_msg_ids) > MAX_DEDUP_IDS:
        _seen_msg_ids.popitem(last=False)
    return False


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: start background scheduler, RSI daemon, and optional iLink worker."""
    lock = acquire_main_lock()
    scheduler_task = asyncio.create_task(scheduler.start())
    rsi_task = asyncio.create_task(rsi_engine.start_daemon())
    if ilink_worker:
        ilink_worker.start()
    try:
        yield
    finally:
        if ilink_worker:
            await ilink_worker.stop()
        scheduler.stop()
        rsi_engine.stop_daemon()
        scheduler_task.cancel()
        rsi_task.cancel()
        if lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()
            except OSError as err:
                logger.debug("Lock release exception: %s", err)


app = FastAPI(
    title="WeChat-AGY Autonomous Multi-CLI Agent",
    description="Code-First, LLM-Last Autonomous Multi-CLI Agent System",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    """Root status endpoint."""
    return {
        "system": "WeChat-AGY Autonomous Agent",
        "status": "operational",
        "code_offload_ratio": monitor.get_offload_rate(),
        "rsi_stage": rsi_engine.phase_controller.get_stage_policy().stage.value,
        "timestamp": time.time(),
    }


@app.get("/health")
async def health():
    """Health observability endpoint."""
    policy = rsi_engine.phase_controller.get_stage_policy()
    return {
        "status": "healthy",
        "offload_rate": monitor.get_offload_rate(),
        "total_incoming": monitor.total_incoming,
        "l0_intercepted": monitor.l0_intercepted,
        "agy_invocations": monitor.agy_invocations,
        "maturity_score": policy.maturity_score,
        "rsi_stage": policy.stage.value,
    }


@app.get("/wechat/webhook")
async def wechat_webhook_handshake(request: Request):
    """Standard WeChat Official Account / Enterprise WeChat GET echostr verification."""
    verify_webhook_auth(request, settings)
    echostr = request.query_params.get("echostr", "ok")
    return PlainTextResponse(echostr)


@app.post("/wechat/webhook")
async def wechat_webhook(request: Request):
    """Authenticated, idempotent, non-blocking async WeChat Webhook endpoint (JSON & XML)."""
    verify_webhook_auth(request, settings)

    content_type = request.headers.get("content-type", "")
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="ignore")

    from_user = "unknown_user"
    content = ""
    msg_type = "text"
    msg_id = f"msg_{int(time.time() * 1000)}_{os.urandom(2).hex()}"

    if "json" in content_type:
        try:
            data = json.loads(body_str)
            from_user = str(data.get("from_user", "unknown_user"))
            content = str(data.get("content", ""))
            msg_id = str(data.get("msg_id", msg_id))
            msg_type = str(data.get("msg_type", "text"))
        except ValueError:
            content = body_str
    elif body_str.lstrip().startswith("<xml>"):
        content = body_str
        msg_type = "voice"
        try:
            root_el = ET.fromstring(body_str.strip())
            user_node = root_el.find("FromUserName")
            if user_node is not None and user_node.text:
                from_user = user_node.text.strip()
            msg_node = root_el.find("MsgId")
            if msg_node is not None and msg_node.text:
                msg_id = msg_node.text.strip()
            type_node = root_el.find("MsgType")
            if type_node is not None and type_node.text:
                msg_type = type_node.text.strip()
            if msg_type == "text":
                txt_node = root_el.find("Content")
                if txt_node is not None and txt_node.text:
                    content = txt_node.text.strip()
        except ET.ParseError:
            from_user = "xml_user"
    else:
        content = body_str

    if "*" not in settings.ALLOWED_USERS and from_user not in settings.ALLOWED_USERS:
        raise HTTPException(status_code=403, detail="Sender not in ALLOWED_USERS whitelist.")

    if _is_duplicate_msg(msg_id):
        return PlainTextResponse("success")

    if content:
        await gateway.handle_message(msg_id, from_user, msg_type, content)

    return PlainTextResponse("success")


if __name__ == "__main__":
    import uvicorn
    print("[SYSTEM] Starting WeChat-AGY Code-First Autonomous Agent...")
    uvicorn.run("main:app", host=settings.SERVICE_HOST, port=settings.SERVICE_PORT, log_level="info")
