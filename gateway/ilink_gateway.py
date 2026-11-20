"""Tencent iLink Bot Pusher, ContextTokenStore & Long-Polling Gateway Worker."""
import asyncio
from collections import OrderedDict
import inspect
import json
import logging
from pathlib import Path
import threading
import time
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
    """Disk-backed thread-safe storage for user context tokens required by iLink Bot replies."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._tokens: Dict[str, str] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.storage_path.exists():
                try:
                    data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        self._tokens = {str(k): str(v) for k, v in data.items()}
                except (OSError, ValueError) as e:
                    logger.warning("Failed to load context tokens from %s: %s", self.storage_path, e)

    def save(self) -> None:
        with self._lock:
            try:
                self.storage_path.write_text(json.dumps(self._tokens, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                logger.warning("Failed to persist context tokens to %s: %s", self.storage_path, e)

    def get(self, user_id: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(user_id)

    def set(self, user_id: str, context_token: str) -> None:
        with self._lock:
            self._tokens[user_id] = context_token
            self.save()

    def remove(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._tokens:
                self._tokens.pop(user_id, None)
                self.save()

    def latest_user_id(self) -> Optional[str]:
        """Return the most recently stored user ID, if any."""
        with self._lock:
            return next(reversed(self._tokens), None)


class ILinkPusher:
    """Pusher implementation sending messages via Tencent iLink Bot API."""

    def __init__(self, settings: Settings, token_store: ContextTokenStore, client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.token_store = token_store
        self.client = client
        self.splitter = WeChatSplitter(max_length=settings.WECHAT_MAX_MSG_LEN)

    async def send_raw(self, user_id: str, content: str) -> bool:
        """Send content chunks to the WeChat user via iLink Bot sendmessage."""
        raw_token = self.settings.WEIXIN_TOKEN
        token = raw_token.get_secret_value() if hasattr(raw_token, "get_secret_value") else raw_token
        if not token:
            logger.warning("WEIXIN_TOKEN is not configured. iLink push skipped.")
            return False

        base_url = self.settings.WEIXIN_BASE_URL or ILINK_DEFAULT_BASE_URL
        context_token = self.token_store.get(user_id)
        chunks = [c for c in self.splitter.split(content) if c.strip()] or ([content] if content else [])
        if not chunks:
            return True

        try:
            for chunk in chunks:
                resp = await ILinkAPIClient.send_message(
                    token=token,
                    to_user_id=user_id,
                    text=chunk,
                    context_token=context_token,
                    base_url=base_url,
                    client=self.client,
                )
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if (ret in (-14, -2) or errcode in (-14, -2)) and context_token:
                    logger.warning("iLink session stale for %s; retrying without context_token...", user_id)
                    self.token_store.remove(user_id)
                    context_token = None
                    resp = await ILinkAPIClient.send_message(
                        token=token,
                        to_user_id=user_id,
                        text=chunk,
                        context_token=None,
                        base_url=base_url,
                        client=self.client,
                    )
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
                    logger.warning("iLink sendmessage returned error for %s: %s", user_id, resp)
                    return False
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
        self._sync_buf_path = self.token_store.storage_path.with_suffix(".sync_buf")
        self.pusher = ILinkPusher(settings=settings, token_store=self.token_store)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sync_buf = self._load_sync_buf()
        self._seen_msg_ids: "OrderedDict[str, float]" = OrderedDict()

    def _load_sync_buf(self) -> str:
        try:
            if self._sync_buf_path.is_file():
                return self._sync_buf_path.read_text(encoding="utf-8").strip()
        except OSError as err:
            logger.debug("Failed to load iLink sync_buf: %s", err)
        return ""

    def _save_sync_buf(self, value: str) -> None:
        if not value or value == self._sync_buf:
            return
        self._sync_buf = value
        try:
            self._sync_buf_path.write_text(value, encoding="utf-8")
        except OSError as err:
            logger.debug("Failed to persist iLink sync_buf: %s", err)

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen_msg_ids and (now - self._seen_msg_ids[msg_id]) < 600.0:
            return True
        self._seen_msg_ids[msg_id] = now
        while len(self._seen_msg_ids) > 512:
            self._seen_msg_ids.popitem(last=False)
        return False

    def is_authorized(self, user_id: str) -> bool:
        if "*" in self.settings.ALLOWED_USERS:
            return True
        return user_id in self.settings.ALLOWED_USERS

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
                    ret = data.get("ret", 0)
                    errcode = data.get("errcode", 0)
                    if ret != 0 or errcode != 0:
                        logger.warning(
                            "iLink getupdates returned non-zero status: ret=%s errcode=%s errmsg=%s",
                            ret,
                            errcode,
                            data.get("errmsg", ""),
                        )
                        await asyncio.sleep(2)
                        continue
                    self._save_sync_buf(str(data.get("get_updates_buf") or self._sync_buf))
                    for msg in data.get("msgs") or []:
                        await self._handle_message(msg)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    logger.warning("iLink getupdates error (streak=%d): %s", consecutive_errors, e)
                    await asyncio.sleep(min(consecutive_errors * 2, 30))
        finally:
            await client.aclose()

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        from_user = str(msg.get("from_user_id") or "").strip()
        context_token = str(msg.get("context_token") or "").strip()
        if not from_user:
            return
        dedup_id = str(msg.get("msg_id") or msg.get("message_id") or msg.get("client_id") or "").strip()
        if self._is_duplicate_msg(dedup_id):
            return
        if context_token:
            await asyncio.to_thread(self.token_store.set, from_user, context_token)

        text_parts = []
        has_voice = False
        has_text = False
        for item in msg.get("item_list") or []:
            if item.get("type") == 1 and "text_item" in item:
                txt = str(item["text_item"].get("text", ""))
                if txt:
                    has_text = True
                    text_parts.append(txt)
            elif "voice_item" in item and isinstance(item["voice_item"], dict):
                has_voice = True
                vtxt = str(item["voice_item"].get("text") or item["voice_item"].get("recognition") or "")
                if vtxt:
                    text_parts.append(vtxt)
            elif item.get("type") == 3:
                has_voice = True
        text_content = "".join(text_parts).strip()
        if not text_content and not has_voice:
            return

        if not self.is_authorized(from_user):
            await self.pusher.send_raw(from_user, "⛔ 权限受限：您不在当前 WeChat-AGY Bridge 的白名单内。")
            return

        inferred_type = "voice" if (has_voice and not has_text) else "text"
        try:
            if hasattr(self.router, "route"):
                route_fn = self.router.route
                if inferred_type == "voice" and "msg_type" in inspect.signature(route_fn).parameters:
                    result = await route_fn(from_user, text_content, msg_type="voice")
                else:
                    result = await route_fn(from_user, text_content)
                reply = result.reply if hasattr(result, "reply") else str(result or "")
            elif hasattr(self.router, "process_inbound_message"):
                proc_fn = self.router.process_inbound_message
                if inferred_type == "voice" and "msg_type" in inspect.signature(proc_fn).parameters:
                    reply = await proc_fn(from_user, text_content, msg_type="voice")
                else:
                    reply = await proc_fn(from_user, text_content)
            else:
                reply = "Router does not implement route or process_inbound_message."
            if reply:
                await self.pusher.send_raw(from_user, reply)
        except Exception as e:
            logger.error("Error routing WeChat message: %s", e)
            await self.pusher.send_raw(from_user, f"❌ 处理指令出错：{e}")
