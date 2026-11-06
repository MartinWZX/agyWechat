"""Single Process Entrypoint with flock, FastAPI Webhook, Multi-CLI Hub, Gateway & Scheduler."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import time
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from bridge.multi_cli_hub import MultiCLIHub
from config import get_settings, load_yaml_config
from gateway.wechat_gateway import WeChatGateway
from gateway.ilink_gateway import ILinkGatewayWorker
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from rsi.evolution_engine import EvolutionEngine
from scheduler.code_first_cron import CodeFirstCronScheduler
from telemetry.health_monitor import HealthMonitor

logger = logging.getLogger("wechat_agy.main")
LOCK_FILE = "/tmp/agent_core/locks/main.lock"
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
    except IOError:
        logger.warning("Another instance of autonomous_multi_cli_agent is already running.")
        return None


# Module singletons
settings = get_settings()
yaml_config = load_yaml_config()
store = CoreStateStore(settings.DB_PATH)
governor = WindControlGovernor(store_or_db=store, config_or_settings=settings, config=yaml_config)
hub = MultiCLIHub(config=yaml_config, governor=governor)
monitor = HealthMonitor(governor=governor, settings=settings, store=store)
gateway = WeChatGateway(config=yaml_config, hub=hub, store=store, governor=governor, health_monitor=monitor, settings=settings)
scheduler = CodeFirstCronScheduler(hub=hub, settings=settings)
rsi_engine = EvolutionEngine(store=store, config=yaml_config)
ilink_worker = ILinkGatewayWorker(settings=settings, router=gateway) if settings.WEIXIN_TOKEN else None


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
            except Exception as err:
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
        "timestamp": time.time(),
    }


@app.get("/health")
async def health():
    """Health observability endpoint."""
    return {
        "status": "healthy",
        "offload_rate": monitor.get_offload_rate(),
        "total_incoming": monitor.total_incoming,
        "l0_intercepted": monitor.l0_intercepted,
        "agy_invocations": monitor.agy_invocations,
    }


@app.post("/wechat/webhook")
async def wechat_webhook(request: Request):
    """WeChat Webhook endpoint supporting JSON and XML inbound messages."""
    content_type = request.headers.get("content-type", "")
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="ignore")

    from_user = "unknown_user"
    content = ""
    msg_id = f"msg_{int(time.time() * 1000)}"

    if "json" in content_type:
        try:
            data = json.loads(body_str)
            from_user = data.get("from_user", "unknown_user")
            content = data.get("content", "")
            msg_id = data.get("msg_id", msg_id)
        except Exception:
            content = body_str
    elif body_str.startswith("<xml>"):
        content = body_str
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(body_str)
            user_node = root.find("FromUserName")
            if user_node is not None and user_node.text:
                from_user = user_node.text
            msg_node = root.find("MsgId")
            if msg_node is not None and msg_node.text:
                msg_id = msg_node.text
        except Exception:
            from_user = "xml_user"
    else:
        content = body_str

    if content:
        if content.startswith("<xml>"):
            gateway.process_incoming_message(msg_id, from_user, "voice", content)
        else:
            await gateway.process_inbound_message(from_user, content)

    return PlainTextResponse("success")


if __name__ == "__main__":
    import uvicorn
    print("[SYSTEM] Starting WeChat-AGY Code-First Autonomous Agent...")
    uvicorn.run("main:app", host=settings.SERVICE_HOST, port=settings.SERVICE_PORT, log_level="info")
