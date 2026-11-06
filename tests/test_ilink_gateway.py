"""Unit tests for Tencent iLink Bot (Personal WeChat direct connection) client and poller."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from config import Settings
from gateway.ilink_gateway import (
    ContextTokenStore,
    ILinkAPIClient,
    ILinkGatewayWorker,
    ILinkPusher,
)


@pytest.fixture
def temp_token_store(tmp_path: Path) -> ContextTokenStore:
    return ContextTokenStore(tmp_path / "test_tokens.json")


def test_make_headers():
    headers = ILinkAPIClient.make_headers(token="test_token_123", body_len=42)
    assert headers["Authorization"] == "Bearer test_token_123"
    assert headers["Content-Length"] == "42"
    assert headers["iLink-App-Id"] == "bot"
    assert "X-WECHAT-UIN" in headers


@pytest.mark.asyncio
async def test_fetch_qr_code():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "qrcode": "qr_abc123",
        "qrcode_img_content": "https://liteapp.weixin.qq.com/q/xyz",
        "ret": 0,
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    qrcode, url = await ILinkAPIClient.fetch_qr_code(client=mock_client)
    assert qrcode == "qr_abc123"
    assert url == "https://liteapp.weixin.qq.com/q/xyz"


@pytest.mark.asyncio
async def test_poll_qr_status():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "confirmed",
        "ilink_bot_id": "bot_123@im.bot",
        "bot_token": "token_secret_xyz",
        "ilink_user_id": "wx_user_999@im.wechat",
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_resp

    res = await ILinkAPIClient.poll_qr_status(qrcode="qr_test", client=mock_client)
    assert res["status"] == "confirmed"
    assert res["ilink_bot_id"] == "bot_123@im.bot"


@pytest.mark.asyncio
async def test_get_updates():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "ret": 0,
        "get_updates_buf": "buf_v2",
        "msgs": [
            {
                "from_user_id": "user_1",
                "context_token": "ctx_token_1",
                "item_list": [{"type": 1, "text_item": {"text": "/pwd"}}],
            }
        ],
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = mock_resp

    updates = await ILinkAPIClient.get_updates(token="bot_token", sync_buf="buf_v1", client=mock_client)
    assert updates["ret"] == 0
    assert updates["get_updates_buf"] == "buf_v2"
    assert len(updates["msgs"]) == 1


@pytest.mark.asyncio
async def test_send_message():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ret": 0, "msg": "ok"}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = mock_resp

    res = await ILinkAPIClient.send_message(
        token="bot_token",
        to_user_id="user_1",
        text="Hello World",
        context_token="ctx_token_1",
        client=mock_client,
    )
    assert res["ret"] == 0

    # Verify posted payload
    call_args = mock_client.post.call_args
    assert call_args is not None
    posted_body = json.loads(call_args.kwargs["content"])
    msg = posted_body["msg"]
    assert msg["to_user_id"] == "user_1"
    assert msg["context_token"] == "ctx_token_1"
    assert msg["item_list"][0]["text_item"]["text"] == "Hello World"


def test_context_token_store(temp_token_store: ContextTokenStore):
    temp_token_store.set("user_a", "ctx_a")
    assert temp_token_store.get("user_a") == "ctx_a"

    # Reload from disk
    new_store = ContextTokenStore(temp_token_store.storage_path)
    assert new_store.get("user_a") == "ctx_a"

    temp_token_store.remove("user_a")
    assert temp_token_store.get("user_a") is None


@pytest.mark.asyncio
async def test_ilink_gateway_worker_dispatch(temp_token_store: ContextTokenStore):
    settings = Settings(
        ALLOWED_USERS=["user_auth"],
        WEIXIN_TOKEN="test_token",
        WEIXIN_ACCOUNT_ID="bot_id",
    )
    mock_router = AsyncMock()
    mock_router.route.return_value = "Echo: /help"

    worker = ILinkGatewayWorker(settings=settings, router=mock_router, token_store=temp_token_store)
    worker.pusher.send_raw = AsyncMock(return_value=True)

    # Authorized user message
    msg = {
        "from_user_id": "user_auth",
        "context_token": "token_auth_ctx",
        "item_list": [{"type": 1, "text_item": {"text": "/help"}}],
    }
    await worker._handle_message(msg)

    assert temp_token_store.get("user_auth") == "token_auth_ctx"
    mock_router.route.assert_called_once_with("user_auth", "/help")
    worker.pusher.send_raw.assert_called_once_with("user_auth", "Echo: /help")


@pytest.mark.asyncio
async def test_ilink_gateway_worker_unauthorized(temp_token_store: ContextTokenStore):
    settings = Settings(
        ALLOWED_USERS=["user_auth"],
        WEIXIN_TOKEN="test_token",
    )
    mock_router = AsyncMock()
    worker = ILinkGatewayWorker(settings=settings, router=mock_router, token_store=temp_token_store)
    worker.pusher.send_raw = AsyncMock(return_value=True)

    # Unauthorized message
    msg = {
        "from_user_id": "intruder",
        "context_token": "ctx",
        "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
    }
    await worker._handle_message(msg)

    # Router should NOT be called
    mock_router.route.assert_not_called()
    assert worker.pusher.send_raw.call_count == 1
    call_args = worker.pusher.send_raw.call_args[0]
    assert call_args[0] == "intruder"
    assert "权限受限" in call_args[1]
