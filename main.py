"""FastAPI Webhook & Tencent iLink Service Entrypoint for agyWechat Gateway."""
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
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx

from bridge.multi_cli_hub import MultiCLIHub
from config import Settings, get_settings, load_yaml_config, resolve_setting_or_yaml
from gateway.ilink_gateway import ILinkGatewayWorker
from gateway.terminal_cleaner import WeChatSplitter
from gateway.wechat_gateway import WeChatGateway
from memory.core_state_store import CoreStateStore
from telemetry.health_monitor import HealthMonitor

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("wechat_agy.main")

LOCK_FILE = "/tmp/agent_core/locks/main.lock"
DEFAULT_DEV_TOKEN = "dev_token_123456"
MAX_DEDUP_IDS = 1024
_lock_fd = None


def acquire_main_lock() -> Any:
    """Acquire OS-level non-blocking flock to enforce single active server instance."""
    global _lock_fd
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd = f
        return f
    except OSError:
        f.close()
        logger.warning("Another instance of %s is already running.", APP_NAME)
        return None


def release_main_lock(lock_file: Any) -> None:
    """Release acquired flock synchronously (intended for thread offload on shutdown)."""
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    except OSError as err:
        logger.debug("Lock release exception: %s", err)


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
_sys_cfg = yaml_config.get("system", {})
APP_NAME = str(_sys_cfg.get("app_name", "agyWechat"))
_effective_db_path = resolve_setting_or_yaml(settings, "DB_PATH", _sys_cfg.get("db_path"), "memory/agent_core.db")

store = CoreStateStore(_effective_db_path)
hub = MultiCLIHub(config=yaml_config, store=store, settings=settings)
monitor = HealthMonitor(settings=settings, store=store)
gateway = WeChatGateway(
    config=yaml_config,
    hub=hub,
    store=store,
    health_monitor=monitor,
    settings=settings,
    db_path=str(_effective_db_path),
)
ilink_worker: Optional[ILinkGatewayWorker] = (
    ILinkGatewayWorker(settings=settings, router=gateway) if settings.WEIXIN_TOKEN else None
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


async def _post_webhook_push(target_user: str, text: str) -> None:
    """Deliver outbound text chunks to configured non-mock WeChat webhook push URL."""
    headers = {"Content-Type": "application/json"}
    secret = settings.WECHAT_BOT_SECRET.get_secret_value()
    if secret:
        headers["X-Bot-Secret"] = secret
    splitter = WeChatSplitter(max_length=settings.WECHAT_MAX_MSG_LEN)
    chunks = [c for c in splitter.split(text) if c.strip()] or ([text] if text else [])
    if not chunks:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for chunk in chunks:
                await client.post(
                    settings.WECHAT_PUSH_URL,
                    json={"to_user": target_user, "content": chunk},
                    headers=headers,
                )
    except httpx.HTTPError as exc:
        logger.warning("[Push -> %s] Webhook push failed: %s", target_user, exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: acquire lock, manage optional iLink worker, and clean shutdown."""
    lock = acquire_main_lock()
    if ilink_worker and lock is not None:
        ilink_worker.start()
    elif ilink_worker and lock is None:
        logger.warning("Skipping iLink worker startup because another instance holds %s.", LOCK_FILE)
    try:
        yield
    finally:
        if ilink_worker:
            await ilink_worker.stop()
        await asyncio.to_thread(store.close)
        if lock:
            await asyncio.to_thread(release_main_lock, lock)


app = FastAPI(
    title="agyWechat Gateway",
    description="WeChat Lightweight Router + Host Workspace Viewer + 4 Native CLI Engines Gateway",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    """Root gateway metadata endpoint."""
    return {
        "app_name": APP_NAME,
        "system": "WeChat Multi-Engine Gateway",
        "status": "operational",
        "installed_engines": hub.get_installed_engines(),
        "uptime_seconds": round(time.time() - monitor.start_time, 2),
        "timestamp": time.time(),
    }


@app.get("/health")
async def health():
    """Real gateway health and engine readiness endpoint."""
    return {
        "status": "healthy",
        "uptime_seconds": round(time.time() - monitor.start_time, 2),
        "total_incoming": monitor.total_incoming,
        "command_hits": monitor.command_hits,
        "engine_calls": dict(monitor.engine_calls),
        "installed_engines": hub.get_installed_engines(),
    }


@app.get("/wechat/webhook")
async def wechat_webhook_handshake(request: Request):
    """Standard WeChat Official Account / Enterprise WeChat GET echostr verification."""
    verify_webhook_auth(request, settings)
    echostr = request.query_params.get("echostr", "ok")
    return PlainTextResponse(echostr)


def _is_xml_payload(text: str) -> bool:
    """Return True if payload starts with an XML root or XML declaration header."""
    stripped = (text or "").lstrip()
    return stripped.startswith("<xml>") or stripped.startswith("<?xml")


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

    if "json" in content_type.lower():
        try:
            data = json.loads(body_str)
            from_user = str(data.get("from_user", "unknown_user"))
            content = str(data.get("content", ""))
            msg_id = str(data.get("msg_id", msg_id))
            msg_type = str(data.get("msg_type", "text"))
        except ValueError:
            content = body_str
    elif _is_xml_payload(body_str):
        content = body_str
        msg_type = "text"
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
                msg_type = type_node.text.strip().lower()
            if msg_type == "text":
                txt_node = root_el.find("Content")
                content = txt_node.text.strip() if txt_node is not None and txt_node.text else ""
        except ET.ParseError as exc:
            raise HTTPException(status_code=400, detail="Malformed WeChat XML payload.") from exc
    else:
        content = body_str

    if "*" not in settings.ALLOWED_USERS and from_user not in settings.ALLOWED_USERS:
        raise HTTPException(status_code=403, detail="Sender not in ALLOWED_USERS whitelist.")

    if _is_duplicate_msg(msg_id):
        return JSONResponse({"status": "success", "reply": ""})

    reply = ""
    if content or msg_type == "voice":
        reply = await gateway.handle_message(msg_id, from_user, msg_type, content)
        if reply and settings.WECHAT_PUSH_URL and "127.0.0.1:8000/mock/" not in settings.WECHAT_PUSH_URL:
            await _post_webhook_push(from_user, reply)

    return JSONResponse({"status": "success", "reply": reply})


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting agyWechat Gateway on %s:%s", settings.SERVICE_HOST, settings.SERVICE_PORT)
    uvicorn.run("main:app", host=settings.SERVICE_HOST, port=settings.SERVICE_PORT, log_level="info")
