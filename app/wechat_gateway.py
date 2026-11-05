"""WeChat Cloud Bot Webhook gateway and asynchronous push queue."""

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.terminal_cleaner import WeChatSplitter

logger = logging.getLogger("wechat_agy.gateway")


@dataclass
class PushMessage:
    """Represents an outgoing push message destined for a WeChat user."""
    user_id: str
    content: str
    msg_type: str = "text"
    retry_count: int = 0


class WeChatPayload(BaseModel):
    """Flexible schema for incoming WeChat webhook JSON payloads."""
    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow",
    )

    FromUserName: Optional[str] = Field(default=None, alias="from_user")
    ToUserName: Optional[str] = Field(default=None, alias="to_user")
    MsgType: Optional[str] = Field(default="text", alias="msg_type")
    Content: Optional[str] = Field(default="", alias="content")

    def get_user_id(self) -> str:
        """Extract user identifier from various common field names."""
        for key in ("FromUserName", "from_user", "openid", "user_id", "From"):
            val = getattr(self, key, None)
            if not val and hasattr(self, "__pydantic_extra__") and self.__pydantic_extra__:
                val = self.__pydantic_extra__.get(key)
            if val:
                return str(val)
        return ""

    def get_content(self) -> str:
        """Extract message text content."""
        if self.Content:
            return str(self.Content)
        if hasattr(self, "__pydantic_extra__") and self.__pydantic_extra__:
            for key in ("content", "text", "message"):
                if key in self.__pydantic_extra__:
                    return str(self.__pydantic_extra__[key])
        return ""


class WebhookValidator:
    """Validates WeChat Webhook signatures."""

    @staticmethod
    def verify_wechat_sha1(token: str, signature: str, timestamp: str, nonce: str) -> bool:
        """Standard WeChat SHA-1 signature check."""
        if not signature or not timestamp or not nonce:
            return False
        parts = sorted([token, timestamp, nonce])
        calculated = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        return hmac.compare_digest(calculated, signature)

    @staticmethod
    def verify_hmac_sha256(token: str, body: bytes, signature: str) -> bool:
        """HMAC-SHA256 signature check for custom webhooks."""
        if not signature:
            return False
        calculated = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(calculated, signature)


class WeChatPusher:
    """Sends active push messages to WeChat Cloud Bot API with retry support."""

    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.client = client
        self.splitter = WeChatSplitter(max_length=settings.WECHAT_MAX_MSG_LEN)

    async def send_raw(self, user_id: str, content: str, msg_type: str = "text") -> bool:
        """Send a single chunk to the configured WeChat Push API."""
        if not self.settings.WECHAT_PUSH_URL:
            logger.warning("WECHAT_PUSH_URL not configured. Push skipped.")
            return True

        headers = {"Content-Type": "application/json"}
        push_url = self.settings.WECHAT_PUSH_URL

        if self.settings.WECHAT_BOT_SECRET:
            headers["Authorization"] = f"Bearer {self.settings.WECHAT_BOT_SECRET}"
            # Official WeChat / WeCom compatibility: append token/key if not already present
            if "api.weixin.qq.com" in push_url and "access_token=" not in push_url:
                delim = "&" if "?" in push_url else "?"
                push_url = f"{push_url}{delim}access_token={self.settings.WECHAT_BOT_SECRET}"
            elif "qyapi.weixin.qq.com" in push_url and "key=" not in push_url:
                delim = "&" if "?" in push_url else "?"
                push_url = f"{push_url}{delim}key={self.settings.WECHAT_BOT_SECRET}"

        payload = {
            "touser": user_id,
            "msgtype": msg_type,
            msg_type: {
                "content": content
            }
        }

        should_close = False
        client = self.client
        if client is None:
            client = httpx.AsyncClient(timeout=10.0)
            should_close = True

        try:
            resp = await client.post(
                push_url,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            logger.debug(f"Pushed message to {user_id}: status={resp.status_code}")
            return True
        except Exception as e:
            logger.error(f"Failed to push message to WeChat user {user_id}: {e}")
            raise
        finally:
            if should_close:
                await client.aclose()


class PushQueue:
    """Asynchronous background push queue with exponential retry."""

    def __init__(self, pusher: WeChatPusher, max_retries: int = 3):
        self.pusher = pusher
        self.max_retries = max_retries
        self.queue: asyncio.Queue[PushMessage] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        """Start the background consumer worker."""
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._worker_loop())
            logger.info("PushQueue background worker started.")

    async def stop(self) -> None:
        """Stop background worker and wait for queue drain."""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("PushQueue background worker stopped.")

    async def push(self, user_id: str, content: str, msg_type: str = "text") -> None:
        """Split content if needed and enqueue chunks."""
        if not user_id or not content:
            return
        chunks = self.pusher.splitter.split(content)
        for chunk in chunks:
            if chunk.strip():
                await self.queue.put(PushMessage(user_id=user_id, content=chunk, msg_type=msg_type))

    async def _worker_loop(self) -> None:
        """Continuous queue consumer with exponential backoff retry."""
        while self._running:
            try:
                msg = await self.queue.get()
                try:
                    await self.pusher.send_raw(msg.user_id, msg.content, msg.msg_type)
                except Exception as e:
                    if msg.retry_count < self.max_retries:
                        msg.retry_count += 1
                        backoff = 2 ** msg.retry_count
                        logger.warning(
                            f"Push failed for {msg.user_id}, retrying ({msg.retry_count}/{self.max_retries}) in {backoff}s: {e}"
                        )
                        async def _retry_after_delay(retry_msg: PushMessage, delay: float):
                            await asyncio.sleep(delay)
                            if self._running:
                                await self.queue.put(retry_msg)

                        asyncio.create_task(_retry_after_delay(msg, backoff))
                    else:
                        logger.error(f"Push to {msg.user_id} dropped after {self.max_retries} retries: {e}")
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in PushQueue worker: {e}")
                await asyncio.sleep(0.5)


class WeChatGateway:
    """Coordinates incoming webhook dispatch, message routing, and outgoing pushes."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        pusher: Optional[WeChatPusher] = None,
    ):
        self.settings = settings or get_settings()
        self.pusher = pusher or WeChatPusher(self.settings)
        self.push_queue = PushQueue(self.pusher)
        self.incoming_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._last_active_user: Optional[str] = None
        self.router: Any = None  # Bound during startup

    def start(self, router: Any) -> None:
        """Start gateway workers."""
        self.router = router
        self.push_queue.start()
        self._dispatcher_task = asyncio.create_task(self._incoming_dispatcher_loop())
        logger.info("WeChatGateway started.")

    async def stop(self) -> None:
        """Stop all gateway workers."""
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
        await self.push_queue.stop()
        logger.info("WeChatGateway stopped.")

    @property
    def active_user(self) -> str:
        """Active user ID for terminal PTY output push."""
        if self._last_active_user:
            return self._last_active_user
        if self.settings.ALLOWED_USERS and self.settings.ALLOWED_USERS[0] != "*":
            return self.settings.ALLOWED_USERS[0]
        return "default_user"

    def is_authorized(self, user_id: str) -> bool:
        """Check if user_id is in ALLOWED_USERS whitelist."""
        if "*" in self.settings.ALLOWED_USERS:
            return True
        return user_id in self.settings.ALLOWED_USERS

    def set_active_user(self, user_id: str) -> None:
        """Record last active user who sent a command."""
        self._last_active_user = user_id

    async def enqueue_incoming(self, user_id: str, content: str) -> None:
        """Enqueue incoming user message for fast 100ms ACK (only update active_user if authorized)."""
        if self.is_authorized(user_id):
            self.set_active_user(user_id)
        await self.incoming_queue.put((user_id, content))

    async def on_terminal_output(self, text: str) -> None:
        """Callback invoked by SingletonAGYManager when PTY emits cleaned output."""
        target_user = None
        if self.router and hasattr(self.router, "manager") and self.router.manager:
            target_user = self.router.manager.session_owner
        if not target_user:
            target_user = self.active_user
        if target_user:
            await self.push_queue.push(user_id=target_user, content=text)

    async def _incoming_dispatcher_loop(self) -> None:
        """Worker that processes incoming messages and sends router responses."""
        while True:
            try:
                user_id, content = await self.incoming_queue.get()
                try:
                    if self.router:
                        response = await self.router.route(user_id, content)
                        if response:
                            await self.push_queue.push(user_id=user_id, content=response)
                except Exception as e:
                    logger.error(f"Error handling message from {user_id}: {e}")
                    await self.push_queue.push(user_id=user_id, content=f"❌ 处理消息出错：{e}")
                finally:
                    self.incoming_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in incoming dispatcher: {e}")
                await asyncio.sleep(0.5)
