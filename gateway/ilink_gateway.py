"""Tencent iLink Bot (Personal WeChat direct connection) client and poller gateway."""

import asyncio
import base64
import json
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

from config import Settings
from gateway.terminal_cleaner import WeChatSplitter

logger = logging.getLogger("wechat_agy.ilink")

ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
# (2 << 16) | (2 << 8) | 0
ILINK_APP_CLIENT_VERSION = 131584

EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"


class ILinkAPIClient:
    """Low-level HTTP API client for Tencent iLink Bot protocol."""

    @staticmethod
    def make_headers(token: Optional[str] = None, body_len: int = 0) -> Dict[str, str]:
        """Construct standard iLink API request headers."""
        random_uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode("utf-8")).decode("ascii")
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_uin,
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if body_len > 0:
            headers["Content-Length"] = str(body_len)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @classmethod
    async def fetch_qr_code(
        cls,
        base_url: str = ILINK_DEFAULT_BASE_URL,
        bot_type: str = "3",
        client: Optional[httpx.AsyncClient] = None,
    ) -> Tuple[str, str]:
        """Request a new WeChat login QR code from iLink API.
        
        Returns:
            Tuple of (qrcode_value, qrcode_img_content_url)
        """
        headers = cls.make_headers()
        url = f"{base_url.rstrip('/')}/{EP_GET_BOT_QR}?bot_type={bot_type}"

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
            should_close = True

        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            qrcode = str(data.get("qrcode") or "")
            qrcode_url = str(data.get("qrcode_img_content") or "")
            return qrcode, qrcode_url
        finally:
            if should_close:
                await client.aclose()

    @classmethod
    async def poll_qr_status(
        cls,
        qrcode: str,
        base_url: str = ILINK_DEFAULT_BASE_URL,
        timeout_seconds: int = 35,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        """Poll the scan/confirmation status of a QR code.
        
        Returns:
            Dictionary with status: 'wait' | 'scaned' | 'confirmed' | 'expired'
        """
        headers = cls.make_headers()
        url = f"{base_url.rstrip('/')}/{EP_GET_QR_STATUS}?qrcode={qrcode}"

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=float(timeout_seconds + 5))
            should_close = True

        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            return {"ret": 0, "status": "wait"}
        finally:
            if should_close:
                await client.aclose()

    @classmethod
    async def get_updates(
        cls,
        token: str,
        sync_buf: str = "",
        base_url: str = ILINK_DEFAULT_BASE_URL,
        timeout_seconds: int = 35,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        """Long-poll incoming messages for this bot account."""
        url = f"{base_url.rstrip('/')}/{EP_GET_UPDATES}"
        payload = {
            "get_updates_buf": sync_buf,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = cls.make_headers(token=token, body_len=len(body.encode("utf-8")))

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=float(timeout_seconds + 5))
            should_close = True

        try:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
        finally:
            if should_close:
                await client.aclose()

    @classmethod
    async def send_message(
        cls,
        token: str,
        to_user_id: str,
        text: str,
        context_token: Optional[str] = None,
        base_url: str = ILINK_DEFAULT_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Dict[str, Any]:
        """Send a single text message reply to a WeChat user."""
        if not text:
            return {"ret": 0, "msg": "empty text"}

        url = f"{base_url.rstrip('/')}/{EP_SEND_MESSAGE}"
        client_id = f"agy-wechat-{uuid.uuid4().hex}"

        msg: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,  # Bot message
            "message_state": 2, # Finished
            "item_list": [
                {
                    "type": 1,  # Text item
                    "text_item": {"text": text},
                }
            ],
        }
        if context_token:
            msg["context_token"] = context_token

        payload = {
            "msg": msg,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = cls.make_headers(token=token, body_len=len(body.encode("utf-8")))

        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
            should_close = True

        try:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        finally:
            if should_close:
                await client.aclose()


class ILinkPusher:
    """Pusher implementation sending messages via Tencent iLink Bot API."""

    def __init__(self, settings: Settings, token_store: "ContextTokenStore", client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.token_store = token_store
        self.client = client
        self.splitter = WeChatSplitter(max_length=settings.WECHAT_MAX_MSG_LEN)

    async def send_raw(self, user_id: str, content: str, msg_type: str = "text") -> bool:
        """Send content chunks to the WeChat user via iLink Bot sendmessage."""
        raw_token = self.settings.WEIXIN_TOKEN
        token = raw_token.get_secret_value() if hasattr(raw_token, "get_secret_value") else raw_token
        if not token:
            logger.warning("WEIXIN_TOKEN is not configured. iLink push skipped.")
            return False

        base_url = self.settings.WEIXIN_BASE_URL or ILINK_DEFAULT_BASE_URL
        context_token = self.token_store.get(user_id)

        try:
            resp = await ILinkAPIClient.send_message(
                token=token,
                to_user_id=user_id,
                text=content,
                context_token=context_token,
                base_url=base_url,
                client=self.client,
            )
            ret = resp.get("ret")
            errcode = resp.get("errcode")

            # Check if context token was expired/stale (-14 or -2 with stale session)
            if ret in (-14, -2) or errcode in (-14, -2):
                if context_token:
                    logger.warning(f"iLink session stale for {user_id}; retrying without context_token...")
                    self.token_store.remove(user_id)
                    resp = await ILinkAPIClient.send_message(
                        token=token,
                        to_user_id=user_id,
                        text=content,
                        context_token=None,
                        base_url=base_url,
                        client=self.client,
                    )
            return True
        except Exception as e:
            logger.error(f"Failed to send iLink message to {user_id}: {e}")
            raise


class ContextTokenStore:
    """Disk-backed storage for user context tokens required by iLink Bot replies."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._tokens: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        if self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._tokens = {str(k): str(v) for k, v in data.items()}
            except Exception as e:
                logger.warning(f"Failed to load context tokens from {self.storage_path}: {e}")

    def save(self) -> None:
        try:
            self.storage_path.write_text(json.dumps(self._tokens, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to persist context tokens to {self.storage_path}: {e}")

    def get(self, user_id: str) -> Optional[str]:
        return self._tokens.get(user_id)

    def set(self, user_id: str, context_token: str) -> None:
        self._tokens[user_id] = context_token
        self.save()

    def remove(self, user_id: str) -> None:
        if user_id in self._tokens:
            self._tokens.pop(user_id, None)
            self.save()


class ILinkGatewayWorker:
    """Background worker that long-polls iLink Bot updates and coordinates with WeChatGateway."""

    def __init__(self, settings: Settings, router: Any, token_store: Optional[ContextTokenStore] = None):
        self.settings = settings
        self.router = router
        root_dir = Path(__file__).resolve().parent.parent
        self.token_store = token_store or ContextTokenStore(root_dir / ".context_tokens.json")
        self.pusher = ILinkPusher(settings=settings, token_store=self.token_store)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sync_buf = ""
        self.active_user: Optional[str] = None

    def is_authorized(self, user_id: str) -> bool:
        """Check if user_id is in ALLOWED_USERS whitelist."""
        if "*" in self.settings.ALLOWED_USERS:
            return True
        return user_id in self.settings.ALLOWED_USERS

    async def on_terminal_output(self, text: str) -> None:
        """Callback invoked by PTY Bridge when terminal emits cleaned output."""
        target_user = None
        if self.router and hasattr(self.router, "active_user") and self.router.active_user:
            target_user = self.router.active_user
        if not target_user:
            target_user = self.active_user
        if target_user:
            chunks = self.pusher.splitter.split(text)
            for chunk in chunks:
                if chunk.strip():
                    await self.pusher.send_raw(target_user, chunk)

    def start(self) -> None:
        """Start background polling worker."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("iLink WeChat background polling worker started.")

    async def stop(self) -> None:
        """Stop background polling worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError as err:
                logger.debug("Task cancel handled: %s", err)
            self._task = None
        logger.info("iLink WeChat background polling worker stopped.")

    async def _poll_loop(self) -> None:
        """Core long-polling loop for WeChat incoming messages."""
        raw_token = self.settings.WEIXIN_TOKEN
        token = raw_token.get_secret_value() if hasattr(raw_token, "get_secret_value") else raw_token
        if not token:
            logger.warning("No WEIXIN_TOKEN configured. iLink poller cannot start.")
            return

        base_url = self.settings.WEIXIN_BASE_URL or ILINK_DEFAULT_BASE_URL
        client = httpx.AsyncClient(timeout=45.0)

        consecutive_errors = 0
        try:
            while self._running:
                try:
                    data = await ILinkAPIClient.get_updates(
                        token=token,
                        sync_buf=self._sync_buf,
                        base_url=base_url,
                        timeout_seconds=35,
                        client=client,
                    )
                    consecutive_errors = 0

                    ret = data.get("ret", 0)
                    if ret != 0:
                        logger.warning(f"iLink getupdates returned ret={ret}: {data}")
                        await asyncio.sleep(2)
                        continue

                    self._sync_buf = str(data.get("get_updates_buf") or self._sync_buf)
                    msgs = data.get("msgs") or []

                    for msg in msgs:
                        await self._handle_message(msg)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    delay = min(consecutive_errors * 2, 30)
                    logger.error(f"Error in iLink poll loop ({consecutive_errors}): {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
        finally:
            await client.aclose()

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Parse and dispatch a single message received from iLink Bot."""
        from_user = str(msg.get("from_user_id") or "").strip()
        context_token = str(msg.get("context_token") or "").strip()

        if not from_user:
            return

        if context_token:
            self.token_store.set(from_user, context_token)

        # Extract text content from item_list
        item_list = msg.get("item_list") or []
        text_content = ""
        for item in item_list:
            if item.get("type") == 1 and "text_item" in item:
                text_content += item["text_item"].get("text", "")

        text_content = text_content.strip()
        if not text_content:
            return

        logger.info(f"Received WeChat message from {from_user}: {text_content[:50]}...")

        if not self.is_authorized(from_user):
            logger.warning(f"Unauthorized WeChat user {from_user} attempted command: {text_content}")
            await self.pusher.send_raw(from_user, "⛔ 权限受限：您不在当前 WeChat-AGY Bridge 的白名单内。")
            return

        self.active_user = from_user

        # Route message to WeChatGateway / Router
        try:
            if hasattr(self.router, "route"):
                reply = await self.router.route(from_user, text_content)
            elif hasattr(self.router, "process_inbound_message"):
                reply = await self.router.process_inbound_message(from_user, text_content)
            else:
                reply = "Router does not implement route or process_inbound_message."
            if reply:
                chunks = self.pusher.splitter.split(reply)
                for chunk in chunks:
                    if chunk.strip():
                        await self.pusher.send_raw(from_user, chunk)
        except Exception as e:
            logger.error(f"Error routing WeChat message: {e}")
            await self.pusher.send_raw(from_user, f"❌ 处理指令出错：{e}")
