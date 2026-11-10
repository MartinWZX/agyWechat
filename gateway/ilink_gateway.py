"""Tencent iLink Bot Pusher, ContextTokenStore & Long-Polling Gateway Worker."""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from config import Settings
from gateway.ilink_client import (
    CHANNEL_VERSION,
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    EP_GET_UPDATES,
    EP_SEND_MESSAGE,
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
    ILINK_DEFAULT_BASE_URL,
    ILinkAPIClient,
)
from gateway.terminal_cleaner import WeChatSplitter

logger = logging.getLogger("wechat_agy.ilink")

__all__ = [
    "CHANNEL_VERSION",
    "ContextTokenStore",
    "EP_GET_BOT_QR",
    "EP_GET_QR_STATUS",
    "EP_GET_UPDATES",
    "EP_SEND_MESSAGE",
    "ILINK_APP_CLIENT_VERSION",
    "ILINK_APP_ID",
    "ILINK_DEFAULT_BASE_URL",
    "ILinkAPIClient",
    "ILinkGatewayWorker",
    "ILinkPusher",
]


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
            except (OSError, ValueError) as e:
                logger.warning("Failed to load context tokens from %s: %s", self.storage_path, e)

    def save(self) -> None:
        try:
            self.storage_path.write_text(json.dumps(self._tokens, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to persist context tokens to %s: %s", self.storage_path, e)

    def get(self, user_id: str) -> Optional[str]:
        return self._tokens.get(user_id)

    def set(self, user_id: str, context_token: str) -> None:
        self._tokens[user_id] = context_token
        self.save()

    def remove(self, user_id: str) -> None:
        if user_id in self._tokens:
            self._tokens.pop(user_id, None)
            self.save()


class ILinkPusher:
    """Pusher implementation sending messages via Tencent iLink Bot API."""

    def __init__(self, settings: Settings, token_store: ContextTokenStore, client: Optional[httpx.AsyncClient] = None):
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
            if (ret in (-14, -2) or errcode in (-14, -2)) and context_token:
                logger.warning("iLink session stale for %s; retrying without context_token...", user_id)
                self.token_store.remove(user_id)
                await ILinkAPIClient.send_message(
                    token=token,
                    to_user_id=user_id,
                    text=content,
                    context_token=None,
                    base_url=base_url,
                    client=self.client,
                )
            return True
        except Exception as e:
            logger.error("Failed to send iLink message to %s: %s", user_id, e)
            raise


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
        if "*" in self.settings.ALLOWED_USERS:
            return True
        return user_id in self.settings.ALLOWED_USERS

    async def on_terminal_output(self, text: str) -> None:
        target_user = getattr(self.router, "active_user", None) if self.router else None
        if not target_user:
            target_user = self.active_user
        if target_user:
            for chunk in self.pusher.splitter.split(text):
                if chunk.strip():
                    await self.pusher.send_raw(target_user, chunk)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("iLink WeChat background polling worker started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError as err:
                logger.debug("Task cancel handled: %s", err)
            self._task = None

    async def _poll_loop(self) -> None:
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
                    if data.get("ret", 0) != 0:
                        await asyncio.sleep(2)
                        continue
                    self._sync_buf = str(data.get("get_updates_buf") or self._sync_buf)
                    for msg in data.get("msgs") or []:
                        await self._handle_message(msg)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    await asyncio.sleep(min(consecutive_errors * 2, 30))
        finally:
            await client.aclose()

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        from_user = str(msg.get("from_user_id") or "").strip()
        context_token = str(msg.get("context_token") or "").strip()
        if not from_user:
            return
        if context_token:
            self.token_store.set(from_user, context_token)

        text_content = ""
        for item in msg.get("item_list") or []:
            if item.get("type") == 1 and "text_item" in item:
                text_content += item["text_item"].get("text", "")
        text_content = text_content.strip()
        if not text_content:
            return

        if not self.is_authorized(from_user):
            await self.pusher.send_raw(from_user, "⛔ 权限受限：您不在当前 WeChat-AGY Bridge 的白名单内。")
            return

        self.active_user = from_user
        try:
            if hasattr(self.router, "route"):
                reply = await self.router.route(from_user, text_content)
            elif hasattr(self.router, "process_inbound_message"):
                reply = await self.router.process_inbound_message(from_user, text_content)
            else:
                reply = "Router does not implement route or process_inbound_message."
            if reply:
                for chunk in self.pusher.splitter.split(reply):
                    if chunk.strip():
                        await self.pusher.send_raw(from_user, chunk)
        except Exception as e:
            logger.error("Error routing WeChat message: %s", e)
            await self.pusher.send_raw(from_user, f"❌ 处理指令出错：{e}")
