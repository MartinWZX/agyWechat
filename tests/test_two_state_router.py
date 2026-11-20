"""Tests for Pure Two-State Engine FSM, Instant Entry, Host Env Passthrough, and Native Conversations."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import time

import pytest

from bridge.drivers.agy_driver import AgyDriver
from bridge.drivers.base_driver import resolve_binary
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings
from gateway.wechat_gateway import NON_ENGINE_VOICE_DENIED_MSG, WeChatGateway
from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
from router.workspace_navigator import WorkspaceNavigator
from telemetry.health_monitor import HealthMonitor


def _write_exec(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return str(path)


def _make_agy_mock_script(path: Path, version: str, exec_log: Path) -> str:
    """Mock `agy` that asserts NO discovery crawlers (--help/--list-commands/acp) are invoked."""
    body = (
        "import json, sys\n"
        f"VERSION = {version!r}\n"
        f"LOG_FILE = {str(exec_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print(VERSION); sys.exit(0)\n"
        "if '--help' in args or '--list-commands' in args or (args and args[0] == 'acp'):\n"
        "    sys.stderr.write('ERROR: crawler flag should never be invoked'); sys.exit(2)\n"
        "with open(LOG_FILE, 'a', encoding='utf-8') as lf:\n"
        "    lf.write(json.dumps(args, ensure_ascii=False) + '\\n')\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ' '.join(args)\n"
        "first_line = prompt.splitlines()[0] if prompt else ''\n"
        "print(f'OK[{VERSION}] first_line={first_line}')\n"
    )
    return _write_exec(path, body)


def _make_pi_mock_script(path: Path) -> str:
    body = (
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('pi-cli 3.1.0'); sys.exit(0)\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ' '.join(args)\n"
        "print(f'PI_EXECUTED:{prompt}')\n"
    )
    return _write_exec(path, body)


@pytest.mark.asyncio
async def test_two_state_engine_fsm_exit_and_instant_entry(tmp_path: Path):
    """Verify State-1 command gateway, instant State-2 entry, only 3 in-engine controls, and 100% passthrough."""
    exec_log = tmp_path / "exec.log"
    agy_path = tmp_path / "mock_agy.py"
    pi_path = tmp_path / "mock_pi.py"

    _make_agy_mock_script(agy_path, version="1.5.8", exec_log=exec_log)
    _make_pi_mock_script(pi_path)

    cfg = {
        "system": {"workspace_dir": str(tmp_path)},
        "engines": {
            "primary": "agy",
            "agy": {"binary": str(agy_path), "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
            "pi": {"binary": str(pi_path), "dangerously_skip_permissions_flag": "--no-confirm"},
            "claude": {"binary": str(tmp_path / "nonexistent_claude")},
            "omp": {"binary": str(tmp_path / "nonexistent_omp")},
        },
    }
    s = Settings(
        _env_file=None,
        WORKSPACE_ROOT=tmp_path,
        DB_PATH=tmp_path / "fsm.db",
        ADMIN_USERS=["u1"],
        AGY_BINARY=str(agy_path),
        PI_BINARY=str(pi_path),
        CLAUDE_BINARY=str(tmp_path / "nonexistent_claude"),
        OMP_BINARY=str(tmp_path / "nonexistent_omp"),
    )
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    mon = HealthMonitor(settings=s, store=store)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, health_monitor=mon, settings=s)

    try:
        # 1. Outer State-1: /help, /status, /engines, /pwd allowlisted; voice & non-gateway commands blocked
        r_help = await gw.process_inbound_message("u1", "/help")
        assert "/status" in r_help and "/engines" in r_help and "/agy" in r_help

        r_status = await gw.process_inbound_message("u1", "/status")
        assert "CPU 负载" in r_status and "物理内存" in r_status and "工作区磁盘" in r_status
        assert "已安装引擎" in r_status and "/agy:" in r_status
        assert "熔断" not in r_status and "COOLDOWN" not in r_status and "LOCKDOWN" not in r_status

        r_engines = await gw.process_inbound_message("u1", "/engines")
        assert "1.5.8" in r_engines and "pi-cli 3.1.0" in r_engines and "未安装" in r_engines
        assert "会话状态" in r_engines and "风控" not in r_engines

        r_pwd = await gw.process_inbound_message("u1", "/pwd")
        assert "当前工作目录" in r_pwd

        r_voice_outer = await gw.process_inbound_message("u1", "打开引擎", msg_type="voice")
        assert r_voice_outer == NON_ENGINE_VOICE_DENIED_MSG

        for blocked_outer in ("/security", "/audit", "/rules", "/exit", "/cd /tmp", "/plan 重构", "帮我重构代码", "/agy 重构代码"):
            assert "网关路由状态" in await gw.process_inbound_message("u1", blocked_outer)

        r_missing = await gw.process_inbound_message("u1", "/claude")
        assert "claude" in r_missing and "PATH" in r_missing
        assert gw.det_router.engine_router.get_session_state("u1")["active_engine"] == ""

        # 2. Enter /agy -> instant welcome panel (NO discovery crawlers), displays engine, version, workspace, conv_id status
        t_start = time.monotonic()
        r_enter = await gw.process_inbound_message("u1", "/agy")
        assert (time.monotonic() - t_start) < 1.5
        assert "已进入 [AGY]" in r_enter and "1.5.8" in r_enter
        assert str(tmp_path.resolve()) in r_enter
        assert "conversation_id" in r_enter
        assert "/exit" in r_enter and "/clear" in r_enter and "/help" in r_enter

        # 3. Inside AGY: ONLY /help, /clear, /exit intercepted by gateway; ALL other slash commands pass straight to CLI!
        r_eng_help = await gw.process_inbound_message("u1", "/help")
        assert "AGY 引擎态" in r_eng_help and str(tmp_path.resolve()) in r_eng_help
        assert "/exit" in r_eng_help and "/clear" in r_eng_help and "/help" in r_eng_help

        for passthrough_input in (
            "/plan inspect router",
            "/grill-me architecture",
            "/diff --stat",
            "/open main.py",
            "/models",
            "/model flash-test",
            "/add-dir /tmp",
            "/status",
            "/pi",
            "/unknown_native_slash arg1",
        ):
            r_pt = await gw.process_inbound_message("u1", passthrough_input)
            assert f"OK[1.5.8] first_line={passthrough_input}" in r_pt

        # Verify /clear resets conversation_id cleanly
        r_clear = await gw.process_inbound_message("u1", "/clear")
        assert "已清空网关记录的 conversation_id" in r_clear

        # 4. /exit returns cleanly to Non-Engine default state
        assert "已退出 [AGY]" in await gw.process_inbound_message("u1", "/exit")
        assert gw.det_router.engine_router.get_session_state("u1")["active_engine"] == ""

        # 5. Simulate AGY binary upgrade (1.5.8 -> 2.0.0): reflected immediately in /engines and /agy
        _make_agy_mock_script(agy_path, version="2.0.0", exec_log=exec_log)
        assert "2.0.0" in await gw.process_inbound_message("u1", "/engines")
        r_reenter = await gw.process_inbound_message("u1", "/agy")
        assert "2.0.0" in r_reenter
        assert "OK[2.0.0] first_line=/deep-audit repo" in await gw.process_inbound_message("u1", "/deep-audit repo")

        # 6. Exit AGY and enter /pi
        await gw.process_inbound_message("u1", "/exit")
        r_enter_pi = await gw.process_inbound_message("u1", "/pi")
        assert "已进入 [PI]" in r_enter_pi and "pi-cli 3.1.0" in r_enter_pi
        assert "PI_EXECUTED:/review main.py" in await gw.process_inbound_message("u1", "/review main.py")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workspace_navigation_external_coding_and_native_sessions(tmp_path: Path):
    """Verify Non-Engine /workspace /pwd /ls guards, external repo coding, native conversation_id & /clear."""
    default_ws = tmp_path / "default_ws"
    repo_b = tmp_path / "projects" / "repo_beta"
    (default_ws / "sub_a").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    (repo_b / "README.md").write_text("# Repo Beta\n", encoding="utf-8")

    exec_log = tmp_path / "session_exec.log"
    cli_path = tmp_path / "mock_native_agy.py"
    body = (
        "import json, os, sys\n"
        f"LOG_FILE = {str(exec_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('agy-native 1.6.0'); sys.exit(0)\n"
        "if '--help' in args or '--list-commands' in args or (args and args[0] == 'acp'):\n"
        "    sys.exit(2)\n"
        "with open(LOG_FILE, 'a', encoding='utf-8') as lf:\n"
        "    lf.write(json.dumps({'cwd': os.getcwd(), 'args': args}, ensure_ascii=False) + '\\n')\n"
        "conv = args[args.index('--conversation') + 1] if '--conversation' in args else ''\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "if 'create calc.py' in prompt:\n"
        "    with open('calc.py', 'w', encoding='utf-8') as f:\n"
        "        f.write('def add(a, b):\\n    return a + b\\n')\n"
        "    out_conv = conv or 'conv_thread_1'\n"
        "    resp = f'CREATED_CALC in {os.getcwd()}'\n"
        "elif prompt.startswith('/diff'):\n"
        "    out_conv = conv or 'conv_thread_1'\n"
        "    resp = f'NATIVE_PASSTHROUGH:{prompt}'\n"
        "elif conv == 'conv_thread_1':\n"
        "    out_conv = 'conv_thread_1'\n"
        "    resp = 'RESUMED_MEMORY: TOKEN_Alpha_99'\n"
        "else:\n"
        "    out_conv = 'conv_thread_2'\n"
        "    resp = 'FRESH_THREAD_NO_MEMORY'\n"
        "print(json.dumps({'conversation_id': out_conv, 'status': 'SUCCESS', 'response': resp}))\n"
    )
    _write_exec(cli_path, body)

    cfg = {
        "system": {"workspace_dir": str(default_ws)},
        "engines": {
            "primary": "agy",
            "agy": {"binary": str(cli_path), "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
        },
    }
    s = Settings(
        _env_file=None,
        WORKSPACE_ROOT=default_ws,
        DB_PATH=tmp_path / "ws_sess.db",
        ADMIN_USERS=["u_ws"],
        AGY_BINARY=str(cli_path),
    )
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, settings=s)

    try:
        # 1. Inspect default workspace, list entries, test security boundaries
        assert str(default_ws.resolve()) in await gw.process_inbound_message("u_ws", "/pwd")
        assert "sub_a/" in await gw.process_inbound_message("u_ws", "/ls")
        assert "网关路由状态" in await gw.process_inbound_message("u_ws", f"/cd {repo_b}")
        assert "禁止将工作目录切换至系统目录" in await gw.process_inbound_message("u_ws", "/workspace /etc")
        ssh_test = os.path.expanduser("~/.ssh")
        if Path(ssh_test).exists():
            assert "禁止访问凭据敏感目录" in await gw.process_inbound_message("u_ws", "/workspace ~/.ssh")

        # 2. Switch workspace to external git repo via /workspace, enter /agy in that workspace
        r_ws = await gw.process_inbound_message("u_ws", f"/workspace {repo_b}")
        assert str(repo_b.resolve()) in r_ws and ".git" in r_ws
        r_enter = await gw.process_inbound_message("u_ws", "/agy")
        assert str(repo_b.resolve()) in r_enter and "agy-native 1.6.0" in r_enter

        # 3. Turn 1: create calc.py inside repo_b natively
        r_t1 = await gw.process_inbound_message("u_ws", "please create calc.py")
        assert "CREATED_CALC" in r_t1 and str(repo_b.resolve()) in r_t1
        assert (repo_b / "calc.py").is_file()

        st1 = gw.det_router.engine_router.get_session_state("u_ws")
        assert st1["meta"]["conversation_id"] == "conv_thread_1"
        assert st1["meta"]["turn_count"] == 1

        # 4. Turn 2: automatically passes --output-format json --conversation conv_thread_1
        r_t2 = await gw.process_inbound_message("u_ws", "what was my token?")
        assert "RESUMED_MEMORY: TOKEN_Alpha_99" in r_t2

        # 5. Native slash command /diff --stat passes straight through to CLI (zero gateway interception)
        r_diff = await gw.process_inbound_message("u_ws", "/diff --stat")
        assert "NATIVE_PASSTHROUGH:/diff --stat" in r_diff

        # 6. /clear resets conversation_id & turn_count while keeping workspace
        r_clear = await gw.process_inbound_message("u_ws", "/clear")
        assert "已清空网关记录的 conversation_id" in r_clear and str(repo_b.resolve()) in r_clear
        st_new = gw.det_router.engine_router.get_session_state("u_ws")
        assert st_new["meta"]["conversation_id"] == "" and st_new["meta"]["turn_count"] == 0

        # 7. Turn 4: fresh session without --conversation
        r_t3 = await gw.process_inbound_message("u_ws", "do you remember?")
        assert "FRESH_THREAD_NO_MEMORY" in r_t3

        records = [json.loads(line) for line in exec_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 4
        assert all(rec["cwd"] == str(repo_b.resolve()) for rec in records)
        assert "--output-format" in records[0]["args"] and "json" in records[0]["args"]
        assert "--conversation" not in records[0]["args"]
        assert "--conversation" in records[1]["args"] and "conv_thread_1" in records[1]["args"]
        assert "--conversation" in records[2]["args"] and "conv_thread_1" in records[2]["args"]
        assert "--conversation" not in records[3]["args"]

        # 8. /exit preserves selected external workspace and /workspace switches back
        r_exit = await gw.process_inbound_message("u_ws", "/exit")
        assert str(repo_b.resolve()) in r_exit
        assert str(repo_b.resolve()) in await gw.process_inbound_message("u_ws", "/pwd")
        r_ws_back = await gw.process_inbound_message("u_ws", f"/workspace {default_ws}")
        assert str(default_ws.resolve()) in r_ws_back
    finally:
        store.close()


@pytest.mark.asyncio
async def test_live_installed_agy_fast_check_and_full_host_env_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify full host environment passthrough without scrubbing/command blocking, plus live agy check."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk_host_live_passthrough")
    monkeypatch.setenv("WECHAT_BOT_SECRET", "host_token_preserved")
    monkeypatch.setenv("CUSTOM_ENGINE_TOKEN", "custom_xyz_999")

    probe_cli = tmp_path / "env_echo_agy.py"
    _write_exec(
        probe_cli,
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('agy-env 1.0'); sys.exit(0)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "payload = {\n"
        "    'anthropic': os.environ.get('ANTHROPIC_API_KEY', ''),\n"
        "    'wechat_sec': os.environ.get('WECHAT_BOT_SECRET', ''),\n"
        "    'custom': os.environ.get('CUSTOM_ENGINE_TOKEN', ''),\n"
        "    'prompt': p,\n"
        "}\n"
        "print(json.dumps({'status': 'SUCCESS', 'response': json.dumps(payload)}))\n",
    )

    drv = AgyDriver(binary_path=str(probe_cli), skip_flag="")
    tricky_prompt = "Explain why `rm -rf /tmp/foo && git push --force origin main` is dangerous"
    rc, out, _ = await drv.execute_one_liner(tricky_prompt, str(tmp_path))
    assert rc == 0
    outer_json = json.loads(out)
    env_seen = json.loads(outer_json["response"])
    assert env_seen["anthropic"] == "sk_host_live_passthrough"
    assert env_seen["wechat_sec"] == "host_token_preserved"
    assert env_seen["custom"] == "custom_xyz_999"
    assert env_seen["prompt"] == tricky_prompt

    resolved = resolve_binary("agy")
    if resolved:
        live_drv = AgyDriver(binary_path=resolved)
        assert live_drv.is_installed() is True
        ver = await live_drv.probe_version(os.getcwd(), timeout=2.0)
        assert len(ver) > 0


def test_workspace_navigator_blocks_gcloud_and_concurrent_session_updates(tmp_path: Path):
    """Verify .config/gcloud path blocking and thread-safe concurrent turn_count updates."""
    ws = tmp_path / "ws"
    gcloud_dir = ws / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)

    store = CoreStateStore(tmp_path / "conc.db")
    try:
        repo = EngineSessionRepository(store)
        nav = WorkspaceNavigator(repo, default_workspace=str(ws))

        ok, resolved, err = nav.validate_path("u_sec", str(gcloud_dir))
        assert ok is False
        assert resolved is None
        assert "凭据敏感目录" in err

        repo.enter_engine("u_conc", "agy", meta={"workspace_dir": str(ws)})
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(repo.update_conversation_state, "u_conc", f"conv_{i}" if i == 0 else None)
                for i in range(16)
            ]
            for fut in futures:
                fut.result()

        state = repo.get_session_state("u_conc")
        assert state["meta"]["turn_count"] == 16
        assert state["meta"]["conversation_id"] == "conv_0"
    finally:
        store.close()
