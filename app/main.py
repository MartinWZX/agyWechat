"""FastAPI application entry point, lifecycle management, and HTTP routes."""

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app import __version__
from app.config import Settings, get_settings
from app.command_router import CommandRouter
from app.singleton_agy import SingletonAGYManager, SystemState, get_agy_manager
from app.wechat_gateway import WeChatGateway, WeChatPayload, WebhookValidator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wechat_agy.main")

# In-memory storage for mock pushed messages during development/testing
MOCK_PUSHED_MESSAGES: List[Dict[str, Any]] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown lifecycle."""
    settings = getattr(app.state, "settings", None) or get_settings()
    logger.info("Initializing WeChat-AGY Bridge...")

    # 1. Initialize Gateway
    gateway = getattr(app.state, "gateway", None)
    if gateway is None:
        gateway = WeChatGateway(settings=settings)
        app.state.gateway = gateway

    # 2. Initialize Singleton AGY Manager with gateway output callback
    manager = getattr(app.state, "manager", None)
    if manager is None:
        manager = get_agy_manager(
            settings=settings,
            push_callback=gateway.on_terminal_output,
        )
        app.state.manager = manager
    else:
        manager.push_callback = gateway.on_terminal_output

    # 3. Initialize CommandRouter and link with Gateway
    router = CommandRouter(manager=manager, settings=settings)
    app.state.router = router
    gateway.start(router=router)

    logger.info("WeChat-AGY Bridge started successfully. Mode: HOST_MODE")
    yield

    # Shutdown
    logger.info("Shutting down WeChat-AGY Bridge...")
    try:
        await manager.terminate()
    except Exception as e:
        logger.error(f"Error terminating agy manager on shutdown: {e}")

    try:
        await gateway.stop()
    except Exception as e:
        logger.error(f"Error stopping gateway on shutdown: {e}")

    logger.info("WeChat-AGY Bridge stopped cleanly.")


def create_app(
    settings: Optional[Settings] = None,
    manager: Optional[SingletonAGYManager] = None,
    gateway: Optional[WeChatGateway] = None,
) -> FastAPI:
    """Application factory for FastAPI app."""
    app_settings = settings or get_settings()
    app = FastAPI(
        title="WeChat-AGY Single-Process Bridge",
        version=__version__,
        description="Bridge between WeChat Cloud Bot and Google Antigravity CLI",
        lifespan=lifespan,
    )

    app.state.settings = app_settings
    if manager is not None:
        app.state.manager = manager
    if gateway is not None:
        app.state.gateway = gateway

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        mgr: SingletonAGYManager = app.state.manager
        return {
            "status": "healthy",
            "version": __version__,
            "state": mgr.state.value,
            "agy_running": mgr.is_running(),
            "pid": mgr.pid,
        }

    @app.get("/status")
    async def system_status():
        """Detailed system status endpoint."""
        mgr: SingletonAGYManager = app.state.manager
        gw: WeChatGateway = app.state.gateway
        return {
            "status": "online",
            "version": __version__,
            "state": mgr.state.value,
            "cwd": str(mgr.GLOBAL_CWD),
            "workspace_root": str(app_settings.WORKSPACE_ROOT),
            "agy_binary": app_settings.AGY_BINARY,
            "pid": mgr.pid,
            "active_user": gw.active_user,
            "allowed_users": app_settings.ALLOWED_USERS,
        }

    @app.get("/webhook")
    async def verify_wechat_webhook(
        signature: Optional[str] = Query(default=None),
        timestamp: Optional[str] = Query(default=None),
        nonce: Optional[str] = Query(default=None),
        echostr: Optional[str] = Query(default=None),
    ):
        """WeChat webhook validation endpoint for setup."""
        if echostr:
            # Check standard WeChat signature if provided
            if signature and timestamp and nonce:
                is_valid = WebhookValidator.verify_wechat_sha1(
                    token=app_settings.WECHAT_TOKEN,
                    signature=signature,
                    timestamp=timestamp,
                    nonce=nonce,
                )
                if not is_valid:
                    raise HTTPException(status_code=403, detail="Invalid WeChat signature")
            return PlainTextResponse(content=echostr)

        return {"status": "ok", "message": "WeChat Webhook endpoint active"}

    @app.post("/webhook")
    async def receive_webhook(
        request: Request,
        signature: Optional[str] = Query(default=None),
        timestamp: Optional[str] = Query(default=None),
        nonce: Optional[str] = Query(default=None),
        x_wechat_signature: Optional[str] = Header(default=None, alias="X-WeChat-Signature"),
    ):
        """Receive WeChat Webhook message and deliver fast 100ms ACK."""
        gw: WeChatGateway = app.state.gateway
        body_bytes = await request.body()

        # Check signature if provided
        sig = signature or x_wechat_signature
        if sig:
            valid = False
            if timestamp and nonce:
                valid = WebhookValidator.verify_wechat_sha1(
                    token=app_settings.WECHAT_TOKEN,
                    signature=sig,
                    timestamp=timestamp,
                    nonce=nonce,
                )
            if not valid:
                valid = WebhookValidator.verify_hmac_sha256(
                    token=app_settings.WECHAT_TOKEN,
                    body=body_bytes,
                    signature=sig,
                )
            if not valid:
                raise HTTPException(status_code=403, detail="Invalid webhook signature")

        # Parse message payload (supports JSON and XML)
        user_id = ""
        content = ""
        try:
            data = await request.json()
            payload = WeChatPayload(**data)
            user_id = payload.get_user_id()
            content = payload.get_content()
        except Exception:
            # Fallback 1: Try XML parsing (standard WeChat Official Account / WeCom format)
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(body_bytes.decode("utf-8", errors="replace"))
                user_id = root.findtext("FromUserName") or root.findtext("from_user") or ""
                content = root.findtext("Content") or root.findtext("content") or ""
            except Exception:
                # Fallback 2: raw text body
                user_id = ""
                content = body_bytes.decode("utf-8", errors="replace").strip()

        if not user_id:
            user_id = "anonymous"

        if content:
            await gw.enqueue_incoming(user_id=user_id, content=content)

        # Fast 100ms ACK response (XML requests get "success" per WeChat Official Account spec)
        content_type = request.headers.get("content-type", "").lower()
        if "xml" in content_type or body_bytes.strip().startswith(b"<xml"):
            return PlainTextResponse("success")

        return {"errcode": 0, "errmsg": "ok"}

    @app.post("/mock/wechat/push")
    async def mock_wechat_push(payload: Dict[str, Any]):
        """Mock push endpoint for local testing and development."""
        MOCK_PUSHED_MESSAGES.append(payload)
        logger.info(f"[Mock WeChat Push Received]: {payload}")
        return {"errcode": 0, "errmsg": "ok"}

    @app.get("/mock/wechat/messages")
    async def get_mock_messages():
        """Retrieve recorded mock pushed messages."""
        return {"count": len(MOCK_PUSHED_MESSAGES), "messages": MOCK_PUSHED_MESSAGES}

    @app.delete("/mock/wechat/messages")
    async def clear_mock_messages():
        """Clear recorded mock pushed messages."""
        MOCK_PUSHED_MESSAGES.clear()
        return {"status": "cleared"}

    return app


app = create_app()
