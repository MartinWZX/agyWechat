"""Unit tests for WeChat gateway, signature verification, and push queue."""

import asyncio
import hashlib
import hmac
import pytest
import httpx
from app.config import Settings
from app.wechat_gateway import (
    WebhookValidator,
    WeChatPayload,
    WeChatPusher,
    PushQueue,
    WeChatGateway,
)


def test_wechat_sha1_signature():
    token = "my_secret_token"
    timestamp = "1700000000"
    nonce = "random123"

    # Correct signature calculation
    parts = sorted([token, timestamp, nonce])
    valid_sig = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    assert WebhookValidator.verify_wechat_sha1(token, valid_sig, timestamp, nonce)
    assert not WebhookValidator.verify_wechat_sha1(token, "invalid_sig", timestamp, nonce)
    assert not WebhookValidator.verify_wechat_sha1("wrong_token", valid_sig, timestamp, nonce)


def test_wechat_hmac_sha256_signature():
    token = "my_secret_token"
    body = b'{"msg": "hello"}'
    valid_sig = hmac.new(token.encode("utf-8"), body, hashlib.sha256).hexdigest()

    assert WebhookValidator.verify_hmac_sha256(token, body, valid_sig)
    assert not WebhookValidator.verify_hmac_sha256(token, body, "bad_sig")


def test_payload_parsing_variations():
    # Format 1: CamelCase standard WeChat
    p1 = WeChatPayload(**{
        "FromUserName": "wx_user_1",
        "ToUserName": "bot_1",
        "Content": "hello world",
    })
    assert p1.get_user_id() == "wx_user_1"
    assert p1.get_content() == "hello world"

    # Format 2: lowercase json
    p2 = WeChatPayload(**{
        "from_user": "wx_user_2",
        "content": "/ls",
    })
    assert p2.get_user_id() == "wx_user_2"
    assert p2.get_content() == "/ls"

    # Format 3: openid
    p3 = WeChatPayload(**{
        "openid": "wx_user_3",
        "text": "/pwd",
    })
    assert p3.get_user_id() == "wx_user_3"
    assert p3.get_content() == "/pwd"


@pytest.mark.asyncio
async def test_push_queue_with_mock_client():
    sent_requests = []

    async def mock_handler(request: httpx.Request):
        sent_requests.append(request)
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    settings = Settings(
        WECHAT_PUSH_URL="https://api.weixin.qq.com/push",
        WECHAT_BOT_SECRET="test_secret",
    )
    pusher = WeChatPusher(settings=settings, client=mock_client)
    push_queue = PushQueue(pusher=pusher)

    push_queue.start()
    await push_queue.push(user_id="user_123", content="Test message content")

    # Give worker loop a moment to consume
    await asyncio.sleep(0.1)
    await push_queue.stop()
    await mock_client.aclose()

    assert len(sent_requests) == 1
    req = sent_requests[0]
    assert req.headers["authorization"] == "Bearer test_secret"
    body_str = req.content.decode("utf-8")
    assert "user_123" in body_str
    assert "Test message content" in body_str


@pytest.mark.asyncio
async def test_push_queue_retry_on_failure():
    attempts = 0

    async def mock_handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    settings = Settings(WECHAT_PUSH_URL="https://api.weixin.qq.com/push")
    pusher = WeChatPusher(settings=settings, client=mock_client)
    push_queue = PushQueue(pusher=pusher, max_retries=2)

    push_queue.start()
    await push_queue.push(user_id="user_fail", content="Will retry")

    # Backoff for first retry is 2^1 = 2 seconds, wait slightly longer
    await asyncio.sleep(2.5)
    await push_queue.stop()
    await mock_client.aclose()

    assert attempts >= 2


@pytest.mark.asyncio
async def test_active_user_protection_against_unauthorized_user():
    settings = Settings(ALLOWED_USERS=["admin_user"])
    gateway = WeChatGateway(settings=settings)

    # Initial active user defaults to first allowed user
    assert gateway.active_user == "admin_user"

    # Unauthorized user sends incoming message
    await gateway.enqueue_incoming("hacker_user", "malicious_payload")
    # Active user MUST NOT be changed to hacker_user!
    assert gateway.active_user == "admin_user"

    # Authorized user sends incoming message
    await gateway.enqueue_incoming("admin_user", "/pwd")
    assert gateway.active_user == "admin_user"


@pytest.mark.asyncio
async def test_wechat_pusher_access_token_query_injection():
    recorded_urls = []

    async def mock_handler(request: httpx.Request):
        recorded_urls.append(str(request.url))
        return httpx.Response(200, json={"errcode": 0})

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    settings = Settings(
        WECHAT_PUSH_URL="https://api.weixin.qq.com/cgi-bin/message/custom/send",
        WECHAT_BOT_SECRET="my_access_token_123",
    )
    pusher = WeChatPusher(settings=settings, client=mock_client)
    await pusher.send_raw("target_user", "test_content")
    await mock_client.aclose()

    assert len(recorded_urls) == 1
    assert "access_token=my_access_token_123" in recorded_urls[0]
