"""Comprehensive End-to-End Test Matrix for WeChat Gateway Commands & Full `agy` CLI Native/Interactive Parity."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import httpx
import pytest

from bridge.drivers.base_driver import resolve_binary
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings
from gateway.ilink_gateway import ContextTokenStore, ILinkGatewayWorker
from gateway.wechat_gateway import (
    EMPTY_RECOGNITION_MSG,
    NON_ENGINE_VOICE_DENIED_MSG,
    WeChatGateway,
)
from memory.core_state_store import CoreStateStore
from telemetry.health_monitor import HealthMonitor


def _write_exec(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return str(path)


def _init_git_repo(repo_dir: Path, files: Dict[str, str]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo_dir), check=True)


def _make_comprehensive_agy_mock(path: Path, exec_log: Path) -> str:
    """Create a faithful mock `agy 1.5.13` CLI reproducing subcommands, print-mode restrictions, spinners & JSON."""
    body = (
        "import json, sys\n"
        f"LOG = {str(exec_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('1.5.13'); sys.exit(0)\n"
        "if args and args[0] == 'help':\n"
        "    topic = args[1] if len(args) > 1 else ''\n"
        "    print(f'Usage: agy {topic} [flags]'); sys.exit(0)\n"
        "if args and args[0] == 'models':\n"
        "    sys.stdout.write('\\r⠋ Fetching available models...\\rgemini-3.8-flash-low\\tGemini 3.8 Flash (Low)\\ngemini-4-neon-high\\tGemini 4 Neon (High)\\n')\n"
        "    sys.exit(0)\n"
        "if args and args[0] == 'agents':\n"
        "    print('planner\\ncode-auditor'); sys.exit(0)\n"
        "if args and args[0] in ('mcp', 'plugin', 'remote-control', 'changelog', 'install', 'update', 'acp', 'mic-serve'):\n"
        "    print(f'SUBCMD:{args[0]} ARGS:{args[1:]}'); sys.exit(0)\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "with open(LOG, 'a', encoding='utf-8') as lf:\n"
        "    lf.write(json.dumps({'args': args, 'prompt': prompt}, ensure_ascii=False) + '\\n')\n"
        "if prompt == '/usage':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': 'Gemini Models\\t91%'})); sys.exit(0)\n"
        "if prompt == '/credits':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': 'Remaining credits\\t42'})); sys.exit(0)\n"
        "if prompt == '/skills':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': 'antigravity-guide\\tAGY Guide\\nrun-workflow\\tRun Workflow', 'command': {'name': 'skills', 'data': {'skills': ['antigravity-guide', 'run-workflow']}}})); sys.exit(0)\n"
        "if prompt == '/plugin list':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': 'swarm\\tenabled\\tUnlock agent swarms', 'command': {'name': 'plugin', 'data': {'plugins': [{'name': 'swarm', 'enabled': True}]}}})); sys.exit(0)\n"
        "if prompt == '/config':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': 'agentMode\\tauto\\nmodel\\tOrchestrator', 'command': {'name': 'config', 'data': {'agentMode': 'auto'}}})); sys.exit(0)\n"
        "if prompt == '/hooks':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': '', 'command': {'name': 'hooks', 'data': {'hooks': ['pre-commit']}}})); sys.exit(0)\n"
        "if prompt == '/permissions':\n"
        "    print(json.dumps({'conversation_id': '', 'status': 'SUCCESS', 'response': '', 'command': {'name': 'permissions', 'data': {'permissions': [{'scope': 'project'}]}}})); sys.exit(0)\n"
        "if prompt.strip() == '/compact':\n"
        "    print(json.dumps({'conversation_id': 'c_mock_1', 'status': 'SUCCESS', 'response': '', 'duration_seconds': 2.4, 'usage': {'total_tokens': 1200}})); sys.exit(0)\n"
        "restricted = ('/resume', '/conversation', '/new', '/fork', '/branch', '/rename', '/rewind', '/undo', '/model', '/add-dir', '/diff', '/open', '/cs', '/codesearch', '/context', '/copy', '/artifact', '/projects', '/tasks', '/feedback', '/statusline', '/keybindings', '/voice', '/record', '/logout', '/btw', '/remote-control', '/quit')\n"
        "if prompt.strip().split()[0] in restricted:\n"
        "    sys.stderr.write(f'{prompt} is not available in print mode'); sys.exit(2)\n"
        "conv = args[args.index('--conversation') + 1] if '--conversation' in args else 'c_mock_1'\n"
        "model_used = args[args.index('--model') + 1] if '--model' in args else 'default'\n"
        "print(json.dumps({'conversation_id': conv, 'status': 'SUCCESS', 'response': f'CLI_OK model={model_used} prompt={prompt}'}))\n"
    )
    return _write_exec(path, body)


@pytest.mark.asyncio
async def test_state1_wechat_complete_command_and_channel_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exhaustively verify State-1 WeChat commands, path sandbox, assets, Webhook SHA-1, dedup & audit endpoint."""
    import main

    ws_main = tmp_path / "ws_main"
    ws_sub = ws_main / "backend"
    ws_alt = tmp_path / "ws_alt"
    _init_git_repo(ws_main, {"app.py": "print('hi')\n", "backend/api.py": "URL = '/api/v1/ping'\n"})
    _init_git_repo(ws_alt, {"README.md": "# Alt Project\n"})
    exec_log = tmp_path / "state1_exec.log"
    agy_bin = _make_comprehensive_agy_mock(tmp_path / "mock_agy.py", exec_log)

    cfg = {
        "system": {"workspace_dir": str(ws_main)},
        "engines": {"primary": "agy", "agy": {"binary": agy_bin, "dangerously_skip_permissions_flag": ""}},
    }
    s = Settings(
        _env_file=None,
        WORKSPACE_ROOT=ws_main,
        DB_PATH=tmp_path / "state1.db",
        ALLOWED_USERS=["wx_u1"],
        WEIXIN_TOKEN="ilink_tok_s1",
        WECHAT_TOKEN="sec_tok_987",
        WEBHOOK_REQUIRE_SIGNATURE=True,
        AGY_BINARY=agy_bin,
    )
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    mon = HealthMonitor(settings=s, store=store)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, health_monitor=mon, settings=s)

    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "monitor", mon)
    monkeypatch.setattr(main, "gateway", gw)
    monkeypatch.setattr(main, "ilink_worker", None)

    try:
        # 1. All allowed State-1 text commands
        assert "/status" in await gw.process_inbound_message("wx_u1", "/help")
        assert "CPU 负载" in await gw.process_inbound_message("wx_u1", "/status")
        r_eng = await gw.process_inbound_message("wx_u1", "/engines")
        assert "🟢 已安装 (1.5.13)" in r_eng and "非引擎网关路由态" in r_eng
        assert str(ws_main.resolve()) in await gw.process_inbound_message("wx_u1", "/pwd")
        r_ls = await gw.process_inbound_message("wx_u1", "/ls")
        assert "backend/" in r_ls and "app.py" in r_ls
        assert "api.py" in await gw.process_inbound_message("wx_u1", "/ls backend")

        # 2. /workspace switching (relative, bare, invalid, forbidden system & credential paths)
        assert str(ws_main.resolve()) in await gw.process_inbound_message("wx_u1", "/workspace")
        assert str(ws_sub.resolve()) in await gw.process_inbound_message("wx_u1", "/workspace backend")
        assert "目录不存在" in await gw.process_inbound_message("wx_u1", "/workspace ./no_such_dir_xyz")
        assert "禁止将工作目录切换至系统目录" in await gw.process_inbound_message("wx_u1", "/workspace /etc")
        gcloud_dir = ws_main / ".config" / "gcloud"
        gcloud_dir.mkdir(parents=True)
        assert "禁止访问凭据敏感目录" in await gw.process_inbound_message("wx_u1", f"/workspace {gcloud_dir}")
        assert str(ws_alt.resolve()) in await gw.process_inbound_message("wx_u1", f"/workspace {ws_alt}")
        assert str(ws_main.resolve()) in await gw.process_inbound_message("wx_u1", f"/workspace {ws_main}")

        # 3. /export and /engines audit (all / valid engine / invalid engine)
        r_exp = await gw.process_inbound_message("wx_u1", "/export")
        assert "每引擎指令审计报告 · ALL" in r_exp
        assert (ws_main / ".wechat_audit" / "engine_audit_all.json").is_file()
        assert "每引擎指令审计报告 · AGY" in await gw.process_inbound_message("wx_u1", "/engines audit agy")
        assert "无效引擎名称" in await gw.process_inbound_message("wx_u1", "/export unknown_eng")

        # 4. State-1 strict guards: voice ban, non-text ban, entry-with-args ban, unknown command ban, missing engine
        assert await gw.process_inbound_message("wx_u1", "你好", msg_type="voice") == NON_ENGINE_VOICE_DENIED_MSG
        assert "仅支持纯文本控制命令" in await gw.process_inbound_message("wx_u1", "", msg_type="video")
        assert "不支持在入口命令后直接附带任务指令" in await gw.process_inbound_message("wx_u1", "/agy do something")
        assert "网关路由状态" in await gw.process_inbound_message("wx_u1", "/plan test")
        assert "网关路由状态" in await gw.process_inbound_message("wx_u1", "hello ai")

        # 5. State-1 asset drop-off saves safely without invoking CLI
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n12345").decode("ascii")
        r_img = await gw.process_inbound_message(
            "wx_u1", "", msg_type="image", media_meta={"file_name": "../hack.png", "base64": png_b64}
        )
        assert "已将微信图片自动落盘" in r_img and (ws_main / ".wechat_assets" / "hack.png").is_file()
        assert not exec_log.exists()

        # 6. FastAPI Webhook SHA-1 Auth, GET handshake, whitelist, dedup & GET /audit/engines
        ts = "1700000000"
        nonce = "nonce123"
        sig = main.compute_wechat_signature("sec_tok_987", ts, nonce)
        with TestClient(main.app) as client:
            assert client.get("/wechat/webhook?echostr=ok_hi").status_code == 403
            r_hs = client.get(f"/wechat/webhook?signature={sig}&timestamp={ts}&nonce={nonce}&echostr=ok_hi")
            assert r_hs.status_code == 200 and r_hs.text == "ok_hi"

            # Whitelist rejection
            r_unauth = client.post(
                "/wechat/webhook",
                json={"from_user": "stranger", "msg_id": "m_s1", "content": "/pwd"},
                headers={"X-WeChat-Token": "sec_tok_987"},
            )
            assert r_unauth.status_code == 403

            # Valid POST + Dedup
            r_ok1 = client.post(
                f"/wechat/webhook?signature={sig}&timestamp={ts}&nonce={nonce}",
                json={"from_user": "wx_u1", "msg_id": "dedup_m1", "content": "/pwd"},
            )
            assert r_ok1.status_code == 200 and str(ws_main.resolve()) in r_ok1.json()["reply"]
            r_ok2 = client.post(
                "/wechat/webhook",
                json={"from_user": "wx_u1", "msg_id": "dedup_m1", "content": "/pwd"},
                headers={"X-WeChat-Token": "sec_tok_987"},
            )
            assert r_ok2.status_code == 200 and r_ok2.json()["reply"] == ""

            # Audit endpoint
            r_aud = client.get("/audit/engines?engine=agy", headers={"X-WeChat-Token": "sec_tok_987"})
            assert r_aud.status_code == 200 and r_aud.json()["status"] == "ok"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_state2_all_agy_native_commands_aliases_and_interactive_flows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify all 50 agy TUI commands, 6 aliases, Unix absolute-path interactive replies & pending cleanup."""
    fake_home = tmp_path / "fake_home"
    proj_dir = fake_home / ".gemini" / "config" / "projects"
    proj_dir.mkdir(parents=True)
    (proj_dir / "my-proj.json").write_text(json.dumps({"id": "proj-alpha", "name": "Alpha Proj"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    ws = tmp_path / "repo_a"
    ws_sibling = tmp_path / "repo_b"
    extra_abs = tmp_path / "extra_shared"
    extra_abs.mkdir()
    _init_git_repo(ws, {
        "main.py": "def run():\n    return '/api/v1/health'\n",
        "PLAN.md": "# Plan\nStep 1\n",
    })
    _init_git_repo(ws_sibling, {"README.md": "# Repo B\n"})
    (ws / "main.py").write_text("def run():\n    return '/api/v1/health_v2'\n", encoding="utf-8")

    exec_log = tmp_path / "state2_exec.log"
    agy_bin = _make_comprehensive_agy_mock(tmp_path / "mock_agy.py", exec_log)
    cfg = {
        "system": {"workspace_dir": str(ws)},
        "engines": {"primary": "agy", "agy": {"binary": agy_bin, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"}},
    }
    s = Settings(_env_file=None, WORKSPACE_ROOT=ws, DB_PATH=tmp_path / "state2.db", AGY_BINARY=agy_bin)
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, settings=s)

    def _has_pending() -> bool:
        st = gw.det_router.engine_router.get_session_state("u_full")
        return bool((st.get("meta") or {}).get("pending_interaction"))

    try:
        assert "已进入 [AGY]" in await gw.process_inbound_message("u_full", "/agy")
        assert "/copy [n|btw|list]" in await gw.process_inbound_message("u_full", "/help")
        assert "Usage: agy mcp" in await gw.process_inbound_message("u_full", "/help mcp")
        assert "/resume" in await gw.process_inbound_message("u_full", "/help conversation")

        # 1. AI turn #1 & #2 + /btw + /copy n|btw|list + /rewind & /undo
        assert "CLI_OK model=default prompt=first turn" in await gw.process_inbound_message("u_full", "first turn")
        assert "CLI_OK model=default prompt=second turn" in await gw.process_inbound_message("u_full", "second turn")

        # Interactive /btw with voice reply should NOT set conversation_id or turn_count, SHOULD populate /copy btw
        assert "/btw 临时插问" in await gw.process_inbound_message("u_full", "/btw")
        assert _has_pending()
        r_btw = await gw.process_inbound_message("u_full", "side q", msg_type="voice")
        assert "CLI_OK" in r_btw and "side q" in r_btw
        assert not _has_pending()
        assert gw.det_router.engine_router.get_session_state("u_full")["meta"]["turn_count"] == 2

        assert "side q" in await gw.process_inbound_message("u_full", "/copy btw")
        assert "prompt=second turn" in await gw.process_inbound_message("u_full", "/copy")
        assert "prompt=first turn" in await gw.process_inbound_message("u_full", "/copy 2")
        assert "选择要复制的回复" in await gw.process_inbound_message("u_full", "/copy list")
        assert _has_pending()
        assert "side q" in await gw.process_inbound_message("u_full", "btw")
        assert not _has_pending()

        # Rewind & alias /undo must exclude internal commands and accurately decrement turn_count
        r_rw_menu = await gw.process_inbound_message("u_full", "/undo")
        assert "最近 2 轮 AI 对话" in r_rw_menu and "first turn" in r_rw_menu and "second turn" in r_rw_menu
        assert _has_pending()
        assert "请输入正整数" in await gw.process_inbound_message("u_full", "abc")
        assert _has_pending()
        assert "已从本地会话记录回退 1 轮" in await gw.process_inbound_message("u_full", "1")
        assert not _has_pending()
        assert gw.det_router.engine_router.get_session_state("u_full")["meta"]["turn_count"] == 1

        # 2. Unix absolute path replies MUST NOT be hijacked by t.startswith("/")!
        assert "附加工作目录" in await gw.process_inbound_message("u_full", "/add-dir")
        assert _has_pending()
        r_add_abs = await gw.process_inbound_message("u_full", str(extra_abs))
        assert str(extra_abs.resolve()) in r_add_abs and "已更新附加工作目录" in r_add_abs
        assert not _has_pending()

        # /cs (alias for /codesearch) with slash-prefixed regex '/api/v1'
        assert "工作区代码检索" in await gw.process_inbound_message("u_full", "/cs")
        assert _has_pending()
        r_cs = await gw.process_inbound_message("u_full", "/api/v1/health_v2")
        assert "main.py:2:" in r_cs
        assert not _has_pending()

        # /open with invalid attempt keeps pending -> valid reply clears pending
        assert "选择要查看的文件" in await gw.process_inbound_message("u_full", "/open")
        assert _has_pending()
        assert "安全限制" in await gw.process_inbound_message("u_full", "/etc/passwd")
        assert _has_pending()
        assert "def run():" in await gw.process_inbound_message("u_full", "main.py")
        assert not _has_pending()

        # /artifact interactive pick
        assert "文档与产物列表" in await gw.process_inbound_message("u_full", "/artifact")
        assert "Step 1" in await gw.process_inbound_message("u_full", "1")
        assert not _has_pending()

        # /projects with Unix abs path + host --project binding + /project menu
        r_projs = await gw.process_inbound_message("u_full", "/projects")
        assert "proj-alpha" in r_projs and _has_pending()
        assert str(ws_sibling.resolve()) in await gw.process_inbound_message("u_full", str(ws_sibling))
        assert not _has_pending()
        assert "proj-alpha" in await gw.process_inbound_message("u_full", "/project")
        assert "已绑定为：`proj-alpha`" in await gw.process_inbound_message("u_full", "1")
        assert not _has_pending()

        # Switch back to ws
        assert str(ws.resolve()) in await gw.process_inbound_message("u_full", f"/projects {ws}")

        # 3. /model spinner strip, invalid retry, one-shot `/model <id> <prompt>`, voice cancel
        assert "gemini-3.8-flash-low" in await gw.process_inbound_message("u_full", "/models")
        r_m_menu = await gw.process_inbound_message("u_full", "/model")
        assert "1. `gemini-3.8-flash-low`" in r_m_menu and "⠋" not in r_m_menu
        assert "已切换为：`gemini-4-neon-high`" in await gw.process_inbound_message("u_full", "2")
        assert not _has_pending()

        # One-shot /model <id> <prompt> overrides --model only for this call without changing session config
        r_oneshot = await gw.process_inbound_message("u_full", "/model gemini-3.8-flash-low run quick check")
        assert "CLI_OK model=gemini-3.8-flash-low prompt=run quick check" in r_oneshot
        assert gw.det_router.engine_router.get_session_state("u_full")["meta"]["config"]["model"] == "gemini-4-neon-high"

        # /mode via voice reply
        assert "AUTO".lower() in (await gw.process_inbound_message("u_full", "/mode")).lower()
        r_mode_v = await gw.process_inbound_message("u_full", "2", msg_type="voice")
        assert "语音识别" in r_mode_v and "`/mode` 已设置为：`plan`" in r_mode_v
        assert not _has_pending()

        # /config -> 3 (effort) -> voice cancel "取消"
        assert "1. model:" in await gw.process_inbound_message("u_full", "/config")
        assert "1. `low`" in await gw.process_inbound_message("u_full", "3")
        assert _has_pending()
        assert "已取消" in await gw.process_inbound_message("u_full", "取消", msg_type="voice")
        assert not _has_pending()
        assert "`/effort` 已设置为：`medium`" in await gw.process_inbound_message("u_full", "/settings effort=medium")
        assert "`--agent` 已设置为：`planner`" in await gw.process_inbound_message("u_full", "/agent 1")
        assert "code-auditor" in await gw.process_inbound_message("u_full", "/agents")
        assert "--sandbox" in await gw.process_inbound_message("u_full", "/sandbox on")

        # 4. /usage vs /quota vs /credits, /skills, /plugin, /hooks, /permissions, /update, /install
        assert "91%" in await gw.process_inbound_message("u_full", "/usage")
        assert "91%" in await gw.process_inbound_message("u_full", "/quota")
        r_cred = await gw.process_inbound_message("u_full", "/credits")
        assert "AI Credits 积分余额" in r_cred and "42" in r_cred
        assert "/antigravity-guide" in await gw.process_inbound_message("u_full", "/skills")
        assert "swarm" in await gw.process_inbound_message("u_full", "/plugin")
        assert "SUBCMD:plugin ARGS:['disable', 'swarm']" in await gw.process_inbound_message("u_full", "disable swarm")
        assert not _has_pending()

        assert "pre-commit" in await gw.process_inbound_message("u_full", "/hooks")
        assert "project" in await gw.process_inbound_message("u_full", "/permissions")
        assert "mcp list" in await gw.process_inbound_message("u_full", "/mcp")
        assert "SUBCMD:mcp ARGS:['list']" in await gw.process_inbound_message("u_full", "list")
        assert not _has_pending()

        assert "SUBCMD:remote-control" in await gw.process_inbound_message("u_full", "/remote-control")
        assert "无效序号" in await gw.process_inbound_message("u_full", "9")
        assert _has_pending()
        assert "SUBCMD:remote-control ARGS:['status']" in await gw.process_inbound_message("u_full", "1")
        assert not _has_pending()

        # /update & /install confirm flow without exit=2
        assert "confirm" in await gw.process_inbound_message("u_full", "/update")
        assert "SUBCMD:update ARGS:[]" in await gw.process_inbound_message("u_full", "confirm")
        assert not _has_pending()
        assert "confirm" in await gw.process_inbound_message("u_full", "/install")
        assert "SUBCMD:install ARGS:['--dir', '/tmp']" in await gw.process_inbound_message("u_full", "--dir /tmp")
        assert not _has_pending()

        for info_cmd, needle in (
            ("/changelog", "SUBCMD:changelog"),
            ("/acp", "SUBCMD:acp"),
            ("/mic-serve", "SUBCMD:mic-serve"),
            ("/memory", "工作区规则与记忆"),
            ("/doctor", "环境诊断"),
            ("/diff", "health_v2"),
            ("/context", "当前会话上下文"),
            ("/statusline", "Statusline"),
            ("/tasks", "任务状态"),
            ("/voice", "微信端已原生支持语音"),
            ("/record", "直接按住发送微信语音"),
            ("/keybindings", "终端快捷键"),
            ("/logout", "宿主"),
        ):
            assert needle in await gw.process_inbound_message("u_full", info_cmd)
            assert not _has_pending()

        # /conversation (/resume alias), /branch (/fork alias), /rename, /title, /new, /feedback
        assert "可恢复的历史会话" in await gw.process_inbound_message("u_full", "/conversation")
        assert "已恢复并绑定会话线程 `c_mock_1`" in await gw.process_inbound_message("u_full", "1")
        assert "分叉开启新分支" in await gw.process_inbound_message("u_full", "/branch exp-1")
        assert "已更新为：`T1`" in await gw.process_inbound_message("u_full", "/title T1")
        assert "已归档旧线程" in await gw.process_inbound_message("u_full", "/new Fresh")
        assert "提交使用反馈" in await gw.process_inbound_message("u_full", "/feedback")
        assert "feedback.jsonl" in await gw.process_inbound_message("u_full", "great UX")
        assert not _has_pending()

        # 5. All 13 ACP AI workflow/skill slash commands + /compact clean JSON empty-response handling
        acp_workflows = (
            "/goal verify", "/schedule check", "/browser open", "/plan design",
            "/grill-me api", "/teamwork refactor", "/learn notes", "/boost perf",
            "/swarm parallel", "/agy-customizations help", "/antigravity-guide q", "/run-workflow wf.py",
        )
        for wf_cmd in acp_workflows:
            assert f"prompt={wf_cmd}" in await gw.process_inbound_message("u_full", wf_cmd)
        r_cmp = await gw.process_inbound_message("u_full", "/compact")
        assert "✅ 执行完成" in r_cmp and '"status"' not in r_cmp

        # 6. Escaping pending interaction cleanly to AI passthrough turn & /quit exit alias
        assert "请输入" in str(await gw.process_inbound_message("u_full", "/model")) or _has_pending()
        r_esc = await gw.process_inbound_message("u_full", "/plan bypass pending")
        assert "prompt=/plan bypass pending" in r_esc and not _has_pending()

        # Empty voice recognition still blocked inside engine
        assert await gw.process_inbound_message("u_full", "", msg_type="voice") == EMPTY_RECOGNITION_MSG

        r_quit = await gw.process_inbound_message("u_full", "/quit")
        assert "已退出 [AGY]" in r_quit
        assert gw.det_router.engine_router.get_session_state("u_full")["active_engine"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not resolve_binary("agy"), reason="real agy CLI not installed on host")
async def test_live_real_agy_binary_full_command_and_interactive_suite(tmp_path: Path):
    """Run real sequential end-to-end tests against installed `agy` binary across WeChat State 1 & State 2."""
    agy_bin = resolve_binary("agy")
    assert agy_bin is not None

    ws = tmp_path / "live_agy_repo"
    extra_dir = tmp_path / "live_extra_ctx"
    extra_dir.mkdir()
    (extra_dir / "ref.txt").write_text("extra ref\n", encoding="utf-8")
    _init_git_repo(ws, {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "NOTES.md": "# Live Notes\nVerified doc\n",
    })
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")

    cfg = {
        "system": {"workspace_dir": str(ws)},
        "engines": {"primary": "agy", "agy": {"binary": agy_bin, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"}},
    }
    s = Settings(_env_file=None, WORKSPACE_ROOT=ws, DB_PATH=tmp_path / "live_agy.db", AGY_BINARY=agy_bin)
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    mon = HealthMonitor(settings=s, store=store)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, health_monitor=mon, settings=s)

    def _assert_clean_reply(cmd_label: str, reply: str) -> None:
        assert reply.strip(), f"Empty reply for {cmd_label}"
        assert "is not available in print mode" not in reply, f"Print-mode error on {cmd_label}: {reply}"
        assert "exit=2" not in reply, f"exit=2 on {cmd_label}: {reply}"

    try:
        # State 1 checks
        assert "网关路由状态" in await gw.process_inbound_message("live_u", "/help")
        assert "已安装引擎" in await gw.process_inbound_message("live_u", "/status")
        assert "/agy" in await gw.process_inbound_message("live_u", "/engines")
        assert str(ws.resolve()) in await gw.process_inbound_message("live_u", "/pwd")
        assert "calc.py" in await gw.process_inbound_message("live_u", "/ls")
        assert "每引擎指令审计报告" in await gw.process_inbound_message("live_u", "/export")
        assert await gw.process_inbound_message("live_u", "语音拦截", msg_type="voice") == NON_ENGINE_VOICE_DENIED_MSG

        # Enter real agy
        r_enter = await gw.process_inbound_message("live_u", "/agy")
        assert "已进入 [AGY]" in r_enter and str(ws.resolve()) in r_enter

        # Live read-only, config, and interactive command suite
        _assert_clean_reply("/help", await gw.process_inbound_message("live_u", "/help"))
        _assert_clean_reply("/help mcp", await gw.process_inbound_message("live_u", "/help mcp"))
        assert "gemini-3.8-flash-low" in await gw.process_inbound_message("live_u", "/models")

        r_model_menu = await gw.process_inbound_message("live_u", "/model")
        _assert_clean_reply("/model", r_model_menu)
        assert "⠋" not in r_model_menu
        assert "已切换为：`gemini-3.8-flash-low`" in await gw.process_inbound_message("live_u", "gemini-3.8-flash-low")

        for cmd_seq in (
            ("/mode", "plan", "`/mode` 已设置为：`plan`"),
            ("/mode", "reset", "已恢复默认 `/mode`"),
            ("/effort", "low", "`/effort` 已设置为：`low`"),
            ("/agents", None, "self"),
            ("/agent", "/cancel", "已取消"),
            ("/sandbox", "on", "已开启"),
            ("/sandbox", "off", "已关闭"),
            ("/add-dir", str(extra_dir), str(extra_dir.resolve())),
            ("/add-dir", "clear", "已清空"),
            ("/open", "calc.py", "def add(a, b):"),
            ("/codesearch", "def add", "calc.py:1:def add"),
            ("/cs", "Verified doc", "NOTES.md:2:Verified doc"),
            ("/artifact", "1", "Verified doc"),
            ("/projects", "/cancel", "已取消"),
            ("/mcp", "list", "MCP"),
            ("/plugin", "list", "swarm"),
            ("/remote-control", "/cancel", "已取消"),
            ("/update", "/cancel", "已取消"),
            ("/install", "/cancel", "已取消"),
            ("/config", "/cancel", "已取消"),
            ("/settings", "effort=low", "`/effort` 已设置为：`low`"),
            ("/feedback", "live check ok", "feedback.jsonl"),
        ):
            cmd_in, reply_in, expected = cmd_seq
            r_menu = await gw.process_inbound_message("live_u", cmd_in)
            _assert_clean_reply(cmd_in, r_menu)
            if reply_in is not None:
                r_done = await gw.process_inbound_message("live_u", reply_in)
                _assert_clean_reply(f"{cmd_in} -> {reply_in}", r_done)
                assert expected in r_done

        for single_cmd in (
            "/diff", "/usage", "/quota", "/credits", "/skills", "/hooks", "/permissions",
            "/memory", "/doctor", "/context", "/statusline", "/tasks",
            "/changelog", "/acp", "/mic-serve", "/voice", "/record", "/keybindings", "/logout",
        ):
            _assert_clean_reply(single_cmd, await gw.process_inbound_message("live_u", single_cmd))

        # Live LLM turn using configured fast model (`gemini-3.8-flash-low`)
        r_live = await gw.process_inbound_message("live_u", "Reply with exactly: LIVE_AGY_OK")
        _assert_clean_reply("LIVE_TURN", r_live)
        assert "LIVE_AGY_OK" in r_live
        conv_id_1 = gw.det_router.engine_router.get_session_state("live_u")["meta"]["conversation_id"]
        assert conv_id_1

        # Live /btw side question must NOT overwrite main conversation_id & must work with /copy btw
        r_btw = await gw.process_inbound_message("live_u", "/btw Reply with exactly: LIVE_BTW_OK")
        _assert_clean_reply("/btw", r_btw)
        assert "LIVE_BTW_OK" in r_btw
        assert gw.det_router.engine_router.get_session_state("live_u")["meta"]["conversation_id"] == conv_id_1
        assert "LIVE_BTW_OK" in await gw.process_inbound_message("live_u", "/copy btw")
        assert "LIVE_AGY_OK" in await gw.process_inbound_message("live_u", "/copy")

        # Live /compact must succeed cleanly without dumping raw JSON
        r_compact = await gw.process_inbound_message("live_u", "/compact")
        _assert_clean_reply("/compact", r_compact)
        assert not r_compact.lstrip().startswith("{")

        # Session lifecycle: /conversation (/resume), /fork, /branch, /rename, /title, /undo (/rewind), /new, /clear, /quit
        assert conv_id_1[:12] in await gw.process_inbound_message("live_u", "/conversation")
        assert f"已恢复并绑定会话线程 `{conv_id_1}`" in await gw.process_inbound_message("live_u", "1")
        assert "分叉开启新分支" in await gw.process_inbound_message("live_u", "/fork b1")
        assert "分叉开启新分支" in await gw.process_inbound_message("live_u", "/branch b2")
        assert "已更新为：`Live Rename`" in await gw.process_inbound_message("live_u", "/rename Live Rename")
        assert "已更新为：`Live Title`" in await gw.process_inbound_message("live_u", "/title Live Title")

        # Restore original thread before testing /undo
        assert f"已恢复并绑定会话线程 `{conv_id_1}`" in await gw.process_inbound_message("live_u", f"/resume {conv_id_1}")
        r_undo = await gw.process_inbound_message("live_u", "/undo 1")
        _assert_clean_reply("/undo 1", r_undo)

        assert "已归档旧线程" in await gw.process_inbound_message("live_u", "/new")
        assert "已清空网关记录的 conversation_id" in await gw.process_inbound_message("live_u", "/clear")
        assert not (gw.det_router.engine_router.get_session_state("live_u")["meta"] or {}).get("pending_interaction")
        assert "已退出 [AGY]" in await gw.process_inbound_message("live_u", "/quit")
        assert gw.det_router.engine_router.get_session_state("live_u")["active_engine"] == ""
    finally:
        store.close()
