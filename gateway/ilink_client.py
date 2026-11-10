"""Low-Level Tencent iLink Bot Protocol HTTP Client."""
import base64
import json
import secrets
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx

ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
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
        random_uin = base64.b64encode(
            str(int.from_bytes(secrets.token_bytes(4), "big")).encode("utf-8")
        ).decode("ascii")
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
        """Request a new WeChat login QR code from iLink API."""
        headers = cls.make_headers()
        url = f"{base_url.rstrip('/')}/{EP_GET_BOT_QR}?bot_type={bot_type}"

        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("qrcode") or ""), str(data.get("qrcode_img_content") or "")
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
        """Poll the scan/confirmation status of a QR code."""
        headers = cls.make_headers()
        url = f"{base_url.rstrip('/')}/{EP_GET_QR_STATUS}?qrcode={qrcode}"

        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=float(timeout_seconds + 5))
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

        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=float(timeout_seconds + 5))
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
        msg: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"agy-wechat-{uuid.uuid4().hex}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token

        payload = {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}}
        body = json.dumps(payload, separators=(",", ":"))
        headers = cls.make_headers(token=token, body_len=len(body.encode("utf-8")))

        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
        try:
            resp = await client.post(url, content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        finally:
            if should_close:
                await client.aclose()
