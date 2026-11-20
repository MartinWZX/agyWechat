"""Unit tests for Tencent iLink Bot client, ContextTokenStore, pusher splitting, and gateway voice boundary."""
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from config import Settings, load_yaml_config
from gateway.ilink_gateway import (
    ContextTokenStore,
    ILinkAPIClient,
    ILinkGatewayWorker,
    ILinkPusher,
)
from gateway.wechat_gateway import NON_ENGINE_VOICE_DENIED_MSG, WeChatGateway
from memory.core_state_store import CoreStateStore


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

    call_args = mock_client.post.call_args
    assert call_args is not None
    posted_body = json.loads(call_args.kwargs["content"])
    msg = posted_body["msg"]
    assert msg["to_user_id"] == "user_1"
    assert msg["context_token"] == "ctx_token_1"
    assert msg["item_list"][0]["text_item"]["text"] == "Hello World"


def test_context_token_store_threadsafe(temp_token_store: ContextTokenStore):
    temp_token_store.set("user_a", "ctx_a")
    assert temp_token_store.get("user_a") == "ctx_a"
    assert temp_token_store.latest_user_id() == "user_a"

    new_store = ContextTokenStore(temp_token_store.storage_path)
    assert new_store.get("user_a") == "ctx_a"

    temp_token_store.remove("user_a")
    assert temp_token_store.get("user_a") is None


@pytest.mark.asyncio
async def test_ilink_gateway_worker_dispatch_no_double_split(
    temp_token_store: ContextTokenStore, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(
        _env_file=None,
        ALLOWED_USERS=["user_auth"],
        WEIXIN_TOKEN="test_token",
        WEIXIN_ACCOUNT_ID="bot_id",
        WECHAT_MAX_MSG_LEN=10,
    )
    long_reply = "A" * 25
    mock_router = AsyncMock()
    mock_router.route.return_value = long_reply

    sent_chunks: list[str] = []

    async def fake_send_message(*, token, to_user_id, text, context_token, base_url, client=None):
        sent_chunks.append(text)
        return {"ret": 0}

    monkeypatch.setattr(ILinkAPIClient, "send_message", fake_send_message)
    worker = ILinkGatewayWorker(settings=settings, router=mock_router, token_store=temp_token_store)

    split_spy_calls = 0
    orig_split = worker.pusher.splitter.split

    def spy_split(text: str):
        nonlocal split_spy_calls
        split_spy_calls += 1
        return orig_split(text)

    worker.pusher.splitter.split = spy_split

    await worker._handle_message({
        "from_user_id": "user_auth",
        "context_token": "token_auth_ctx",
        "item_list": [{"type": 1, "text_item": {"text": "/help"}}],
    })

    assert temp_token_store.get("user_auth") == "token_auth_ctx"
    mock_router.route.assert_called_once_with("user_auth", "/help")
    # Split happens ONCE inside ILinkPusher.send_raw, never double-split in _handle_message
    assert split_spy_calls == 1
    assert "".join(sent_chunks) == long_reply
    assert len(sent_chunks) == 3


@pytest.mark.asyncio
async def test_ilink_gateway_worker_unauthorized(temp_token_store: ContextTokenStore):
    settings = Settings(_env_file=None, ALLOWED_USERS=["user_auth"], WEIXIN_TOKEN="test_token")
    mock_router = AsyncMock()
    worker = ILinkGatewayWorker(settings=settings, router=mock_router, token_store=temp_token_store)
    worker.pusher.send_raw = AsyncMock(return_value=True)

    await worker._handle_message({
        "from_user_id": "intruder",
        "context_token": "ctx",
        "item_list": [{"type": 1, "text_item": {"text": "/status"}}],
    })

    mock_router.route.assert_not_called()
    worker.pusher.send_raw.assert_awaited_once()
    args = worker.pusher.send_raw.call_args[0]
    assert args[0] == "intruder"
    assert "权限受限" in args[1]


@pytest.mark.asyncio
async def test_ilink_gateway_voice_two_state_boundary_with_real_gateway(
    tmp_path: Path,
    temp_token_store: ContextTokenStore,
):
    prompt_log = tmp_path / "captured_prompts.txt"
    mock_bin = tmp_path / "mock_agy_voice.py"
    mock_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"LOG_P = {str(prompt_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args: print('agy 1.5.9'); sys.exit(0)\n"
        "if '--help' in args: print('Usage: agy [flags]\\n  -p  print'); sys.exit(0)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "with open(LOG_P, 'a', encoding='utf-8') as f: f.write(p + '\\n')\n"
        "print(json.dumps({'status': 'SUCCESS', 'response': 'ILINK_VOICE_INNER_OK'}))\n",
        encoding="utf-8",
    )
    mock_bin.chmod(0o755)

    cfg = load_yaml_config()
    cfg["system"]["workspace_dir"] = str(tmp_path)
    cfg["engines"]["agy"]["binary"] = str(mock_bin)
    cfg["engines"]["agy"]["dangerously_skip_permissions_flag"] = ""
    local_settings = Settings(
        _env_file=None,
        ALLOWED_USERS=["user_voice"],
        WEIXIN_TOKEN="test_token",
        WORKSPACE_ROOT=tmp_path,
        DB_PATH=tmp_path / "voice_gateway.db",
        AGY_BINARY=str(mock_bin),
    )
    local_store = CoreStateStore(local_settings.DB_PATH)
    try:
        gw = WeChatGateway(
            config=cfg, store=local_store, settings=local_settings
        )
        worker = ILinkGatewayWorker(
            settings=local_settings, router=gw, token_store=temp_token_store
        )
        worker.pusher.send_raw = AsyncMock(return_value=True)

        # 1. Outer Non-Engine state strictly forbids voice control
        await worker._handle_message({
            "from_user_id": "user_voice",
            "message_id": "msg_voice_outer_001",
            "context_token": "ctx_voice_001",
            "item_list": [{"type": 3, "voice_item": {"text": "查看状态"}}],
        })
        outer_reply = worker.pusher.send_raw.call_args[0][1]
        assert outer_reply == NON_ENGINE_VOICE_DENIED_MSG

        # 2. Enter /agy via text command, then iLink voice passes native Recognition verbatim
        worker.pusher.send_raw.reset_mock()
        await worker._handle_message({
            "from_user_id": "user_voice",
            "message_id": "msg_enter_text_002",
            "context_token": "ctx_voice_002",
            "item_list": [{"type": 1, "text_item": {"text": "/agy"}}],
        })
        assert "已进入 [AGY]" in worker.pusher.send_raw.call_args[0][1]

        # 3. Empty voice recognition inside engine returns explicit notice & never invokes engine
        worker.pusher.send_raw.reset_mock()
        await worker._handle_message({
            "from_user_id": "user_voice",
            "message_id": "msg_voice_empty_003",
            "context_token": "ctx_voice_003",
            "item_list": [{"type": 3, "voice_item": {"recognition": ""}}],
        })
        assert "未获取到微信原生语音解析结果" in worker.pusher.send_raw.call_args[0][1]
        assert not prompt_log.exists()

        # 4. Non-empty voice passes verbatim (even phrases that used to be homophone-hijacked)
        worker.pusher.send_raw.reset_mock()
        await worker._handle_message({
            "from_user_id": "user_voice",
            "message_id": "msg_voice_inner_004",
            "context_token": "ctx_voice_004",
            "item_list": [{"type": 3, "voice_item": {"text": "呃 丰控看板 帮我分析代码"}}],
        })
        inner_reply = worker.pusher.send_raw.call_args[0][1]
        assert "语音识别" in inner_reply and "ILINK_VOICE_INNER_OK" in inner_reply
        assert prompt_log.read_text(encoding="utf-8").strip() == "呃 丰控看板 帮我分析代码"
    finally:
        local_store.close()


@pytest.mark.asyncio
async def test_driver_probe_version_timeout_reaps_killed_child(tmp_path: Path):
    from bridge.drivers.agy_driver import AgyDriver

    script_path = tmp_path / "term_ignorer.sh"
    script_path.write_text("#!/bin/sh\ntrap '' TERM\nsleep 10\n", encoding="utf-8")
    script_path.chmod(0o755)

    drv = AgyDriver(binary_path=str(script_path))
    ver = await drv.probe_version(workspace_dir=str(tmp_path), timeout=0.35)
    assert ver == ""

    my_pid = str(os.getpid())
    zombies = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rparen = stat_text.rfind(")")
        if rparen == -1:
            continue
        fields = stat_text[rparen + 2 :].split()
        if len(fields) >= 2 and fields[0] == "Z" and fields[1] == my_pid:
            zombies.append(entry.name)
    assert zombies == []
