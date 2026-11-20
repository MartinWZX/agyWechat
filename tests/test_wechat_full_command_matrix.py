"""End-to-End Test Matrix for Lightweight Two-State Router, Four Engines, Native Voice Boundary & Webhook."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import httpx
import pytest

from bridge.drivers.agy_driver import AgyDriver
from bridge.drivers.base_driver import BaseCLIAgentDriver, resolve_binary
from bridge.drivers.claude_driver import ClaudeDriver
from bridge.drivers.omp_driver import OmpDriver
from bridge.drivers.pi_driver import PiDriver
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings, get_engine_binary, resolve_workspace_root
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


def _make_full_agy_mock(path: Path, prompt_log: Path) -> str:
    """Create a realistic mock `agy` executable without discovery crawlers."""
    body = (
        "import json, os, sys\n"
        f"PROMPT_LOG = {str(prompt_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('agy 1.5.9'); sys.exit(0)\n"
        "if '--help' in args or '--list-commands' in args or (args and args[0] == 'acp'):\n"
        "    sys.stderr.write('unexpected discovery invocation'); sys.exit(2)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ' '.join(args)\n"
        "with open(PROMPT_LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'prompt': p, 'args': args}, ensure_ascii=False) + '\\n')\n"
        "if 'create calc.py' in p:\n"
        "    with open('calc.py', 'w', encoding='utf-8') as cf: cf.write('def add(a, b):\\n    return a + b\\n')\n"
        "    print(json.dumps({'conversation_id':'c_main','status':'SUCCESS','response':'CREATED calc.py'})); sys.exit(0)\n"
        "print(json.dumps({'conversation_id':'c_main','status':'SUCCESS','response':f'AGY_ECHO:{p}'}))\n"
    )
    return _write_exec(path, body)


def _make_simple_engine_mock(path: Path, engine_name: str, version_str: str, prompt_log: Path) -> str:
    """Create a realistic mock CLI executable for pi, claude, or omp."""
    body = (
        "import json, sys\n"
        f"ENG = {engine_name!r}\n"
        f"VER = {version_str!r}\n"
        f"LOG = {str(prompt_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print(VER); sys.exit(0)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ' '.join(args)\n"
        "with open(LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'engine': ENG, 'prompt': p, 'args': args}, ensure_ascii=False) + '\\n')\n"
        "print(json.dumps({'status':'SUCCESS','response':f'{ENG.upper()}_OK:{p}'}))\n"
    )
    return _write_exec(path, body)


async def _send_ilink(worker: ILinkGatewayWorker, sends: List[Dict[str, Any]], user_id: str, text: str) -> str:
    sends.clear()
    await worker._handle_message({
        "from_user_id": user_id,
        "message_id": f"m_{len(sends)}_{time.time_ns()}",
        "context_token": f"ctx_{user_id}",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    })
    assert len(sends) >= 1
    return "\n".join(s["text"] for s in sends)


@pytest.mark.asyncio
async def test_four_engine_matrix_voice_boundary_and_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """End-to-end verification of Non-Engine router, 4 engines, Recognition voice passthrough, and FastAPI Webhook."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    prompt_log = tmp_path / "prompts.log"
    agy_bin = _make_full_agy_mock(tmp_path / "mock_agy.py", prompt_log)
    pi_bin = _make_simple_engine_mock(tmp_path / "mock_pi.py", "pi", "pi 2.0.1", prompt_log)
    claude_bin = _make_simple_engine_mock(tmp_path / "mock_claude.py", "claude", "claude-code 1.2.3", prompt_log)
    omp_bin = _make_simple_engine_mock(tmp_path / "mock_omp.py", "omp", "omp 0.9.0", prompt_log)

    cfg = {
        "system": {"workspace_dir": str(repo)},
        "engines": {
            "primary": "agy",
            "agy": {"binary": agy_bin, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
            "pi": {"binary": pi_bin, "dangerously_skip_permissions_flag": "--no-confirm"},
            "claude": {"binary": claude_bin, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
            "omp": {"binary": omp_bin, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions --yes"},
        },
    }
    s = Settings(
        _env_file=None,
        ALLOWED_USERS=["wx_u1", "wx_wh"],
        WEIXIN_TOKEN="ilink_tok",
        WECHAT_TOKEN="wh_tok_secure",
        WEBHOOK_REQUIRE_SIGNATURE=False,
        WORKSPACE_ROOT=repo,
        DB_PATH=tmp_path / "matrix.db",
        AGY_BINARY=agy_bin,
        PI_BINARY=pi_bin,
        CLAUDE_BINARY=claude_bin,
        OMP_BINARY=omp_bin,
    )
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    mon = HealthMonitor(settings=s, store=store)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, health_monitor=mon, settings=s)

    sends: List[Dict[str, Any]] = []

    async def fake_send_message(*, token, to_user_id, text, context_token, base_url, client=None):
        sends.append({"to": to_user_id, "text": text})
        return {"ret": 0}

    monkeypatch.setattr("gateway.ilink_gateway.ILinkAPIClient.send_message", fake_send_message)
    worker = ILinkGatewayWorker(settings=s, router=gw, token_store=ContextTokenStore(tmp_path / "tok.json"))

    try:
        # 1. Outer Non-Engine State: voice blocked; /help, /status, /engines, /pwd, /workspace, /ls succeed
        r_voice_blk = await gw.handle_message("v_out", "wx_u1", "voice", "打开引擎")
        assert r_voice_blk == NON_ENGINE_VOICE_DENIED_MSG

        r_help = await _send_ilink(worker, sends, "wx_u1", "/help")
        assert "/status" in r_help and "/engines" in r_help and "/omp" in r_help

        r_status = await _send_ilink(worker, sends, "wx_u1", "/status")
        assert "CPU 负载" in r_status and "物理内存" in r_status and "工作区磁盘" in r_status
        assert "已安装引擎" in r_status
        assert "熔断" not in r_status and "COOLDOWN" not in r_status and "LOCKDOWN" not in r_status
        for e_tag in ("/agy:", "/pi:", "/claude:", "/omp:"):
            assert e_tag in r_status

        r_engines = await _send_ilink(worker, sends, "wx_u1", "/engines")
        assert "agy 1.5.9" in r_engines and "pi 2.0.1" in r_engines
        assert "claude-code 1.2.3" in r_engines and "omp 0.9.0" in r_engines
        assert "会话状态" in r_engines and "风控" not in r_engines

        assert str(repo.resolve()) in await _send_ilink(worker, sends, "wx_u1", "/pwd")
        assert "src/" in await _send_ilink(worker, sends, "wx_u1", "/ls")

        # 2. Enter /agy -> instant welcome panel; all inputs except /exit /clear /help pass verbatim to CLI
        r_enter_agy = await _send_ilink(worker, sends, "wx_u1", "/agy")
        assert "已进入 [AGY]" in r_enter_agy and "1.5.9" in r_enter_agy and str(repo.resolve()) in r_enter_agy

        for raw_slash in ("/models", "/usage", "/diff", "/open src/main.py", "/plan check", "/hooks", "/add-dir /tmp"):
            assert f"AGY_ECHO:{raw_slash}" in await _send_ilink(worker, sends, "wx_u1", raw_slash)

        assert "CREATED calc.py" in await _send_ilink(worker, sends, "wx_u1", "please create calc.py")
        assert "AGY 引擎态" in await _send_ilink(worker, sends, "wx_u1", "/help")
        assert "已清空网关记录的 conversation_id" in await _send_ilink(worker, sends, "wx_u1", "/clear")

        # Empty <Recognition> voice inside engine must return error & NOT reach engine CLI
        cnt_before_empty = len(prompt_log.read_text(encoding="utf-8").splitlines())
        empty_xml = "<xml><FromUserName><![CDATA[wx_u1]]></FromUserName><MsgType><![CDATA[voice]]></MsgType><Recognition><![CDATA[   ]]></Recognition></xml>"
        assert await gw.handle_message("v_empty", "wx_u1", "voice", empty_xml) == EMPTY_RECOGNITION_MSG
        assert len(prompt_log.read_text(encoding="utf-8").splitlines()) == cnt_before_empty

        # Valid <Recognition> voice passes verbatim WITHOUT homophone rewriting or filler stripping
        ok_xml = "<xml><FromUserName><![CDATA[wx_u1]]></FromUserName><MsgType><![CDATA[voice]]></MsgType><Recognition><![CDATA[呃 丰控看板 检查端口]]></Recognition></xml>"
        r_v_ok = await gw.handle_message("v_ok", "wx_u1", "voice", ok_xml)
        assert "AGY_ECHO:呃 丰控看板 检查端口" in r_v_ok

        # 3. Switch cleanly via /exit across /pi, /claude, and /omp
        assert "已退出 [AGY]" in await _send_ilink(worker, sends, "wx_u1", "/exit")
        for eid, exp_ver in (("pi", "PI_OK:"), ("claude", "CLAUDE_OK:"), ("omp", "OMP_OK:")):
            assert f"已进入 [{eid.upper()}]" in await _send_ilink(worker, sends, "wx_u1", f"/{eid}")
            r_eng_v = await gw.handle_message(f"v_{eid}", "wx_u1", "voice", f"用{eid}分析日志")
            assert f"{exp_ver}用{eid}分析日志" in r_eng_v
            assert f"已退出 [{eid.upper()}]" in await _send_ilink(worker, sends, "wx_u1", "/exit")

        # 4. FastAPI Webhook: JSON response, XML missing MsgType defaults to text, async push trigger
        import main

        pushed_calls: List[Dict[str, Any]] = []

        async def fake_post(self, url, *, json=None, headers=None, **kwargs):
            pushed_calls.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(200, text="ok")

        wh_settings = Settings(
            _env_file=None,
            WORKSPACE_ROOT=repo,
            DB_PATH=s.DB_PATH,
            ALLOWED_USERS=["wx_wh"],
            ADMIN_USERS=["wx_wh"],
            WECHAT_TOKEN="wh_tok_secure",
            WEBHOOK_REQUIRE_SIGNATURE=False,
            WECHAT_PUSH_URL="https://push.example.local/send",
            WECHAT_BOT_SECRET="sec_99",
        )
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr(main, "settings", wh_settings)
        monkeypatch.setattr(main, "gateway", gw)
        monkeypatch.setattr(main, "hub", hub)
        monkeypatch.setattr(main, "monitor", mon)
        monkeypatch.setattr(main, "ilink_worker", None)

        with TestClient(main.app) as client:
            r_root = client.get("/")
            assert r_root.status_code == 200 and "installed_engines" in r_root.json()
            assert "rsi_stage" not in r_root.json()
            r_health = client.get("/health")
            assert r_health.status_code == 200 and r_health.json()["status"] == "healthy"
            assert "offload_rate" not in r_health.json()

            xml_no_type = (
                "<xml><FromUserName><![CDATA[wx_wh]]></FromUserName><MsgId>wh_notype_1</MsgId>"
                "<Content><![CDATA[/pwd]]></Content></xml>"
            )
            res_wh = client.post("/wechat/webhook", content=xml_no_type, headers={"X-WeChat-Token": "wh_tok_secure"})
            assert res_wh.status_code == 200
            payload = res_wh.json()
            assert payload["status"] == "success"
            assert str(repo.resolve()) in payload["reply"]
            assert len(pushed_calls) == 1
            assert pushed_calls[0]["url"] == "https://push.example.local/send"
            assert pushed_calls[0]["headers"]["X-Bot-Secret"] == "sec_99"
    finally:
        store.close()


def test_config_precedence_and_pruned_surfaces(tmp_path: Path):
    """Verify physically deleted modules, config precedence, legacy SQLite zombie-table migration & drivers."""
    repo_root = Path(__file__).resolve().parent.parent
    for removed_rel in (
        "guard",
        "bridge/error_classifier.py",
        "router/engine_tui_ops.py",
        "router/engine_subcommand_handler.py",
        "bridge/engine_command_discovery.py",
        "bridge/acp_command_probe.py",
        "bridge/tui_command_probe.py",
        "bridge/command_curator.py",
        "sandbox",
        "scripts/render_test_report.py",
        "result",
    ):
        assert not (repo_root / removed_rel).exists(), f"Should be deleted: {removed_rel}"

    for removed_mod in (
        "guard.wind_control_governor",
        "bridge.error_classifier",
        "router.engine_tui_ops",
        "router.engine_subcommand_handler",
        "bridge.engine_command_discovery",
        "bridge.acp_command_probe",
        "bridge.tui_command_probe",
        "bridge.command_curator",
        "sandbox.approval_guard",
    ):
        with pytest.raises(ModuleNotFoundError):
            __import__(removed_mod)

    yaml_cfg = {
        "system": {"workspace_dir": str(tmp_path / "yaml_ws")},
        "engines": {
            "agy": {"binary": "/yaml/agy"},
            "pi": {"binary": "/yaml/pi"},
            "claude": {"binary": "/yaml/claude"},
            "omp": {"binary": "/yaml/omp"},
        },
    }
    default_s = Settings(_env_file=None)
    assert get_engine_binary("pi", settings=default_s, yaml_config=yaml_cfg) == "/yaml/pi"
    override_s = Settings(_env_file=None, PI_BINARY="/env/pi")
    assert get_engine_binary("pi", settings=override_s, yaml_config=yaml_cfg) == "/env/pi"
    assert resolve_workspace_root(settings=default_s, yaml_config=yaml_cfg) == str((tmp_path / "yaml_ws").resolve())
    for removed_cfg in ("MIN_COOLDOWN_SECONDS", "COOLDOWN_429_SCHEDULE", "LOCKDOWN_403_HOURS", "QUIET_HOURS_START"):
        assert not hasattr(default_s, removed_cfg)

    # Seed legacy SQLite database with zombie tables, FTS virtual table + triggers, and engine_commands_json column
    legacy_db = tmp_path / "legacy_migration.db"
    with sqlite3.connect(str(legacy_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE working_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at REAL,
                updated_at REAL,
                active_engine TEXT DEFAULT '',
                engine_commands_json TEXT DEFAULT '[]',
                engine_meta_json TEXT DEFAULT '{}',
                workspace_dir TEXT DEFAULT ''
            );
            INSERT INTO working_sessions (session_id, user_id, created_at, updated_at, active_engine, workspace_dir)
                VALUES ('session_u1', 'u1', 1.0, 1.0, 'agy', '/tmp/ws1');
            CREATE TABLE messages (
                msg_id TEXT PRIMARY KEY, session_id TEXT, user_id TEXT, msg_type TEXT,
                raw_content TEXT, normalized_content TEXT, is_voice INTEGER DEFAULT 0,
                reply_content TEXT, engine_used TEXT, offloaded INTEGER DEFAULT 0, created_at REAL
            );
            CREATE TABLE cron_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
            CREATE TABLE crystallized_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
            CREATE TABLE episodic_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT UNIQUE);
            CREATE VIRTUAL TABLE episodic_memories_fts USING fts5(content);
            CREATE TRIGGER episodic_memories_ai AFTER INSERT ON episodic_memories BEGIN
                INSERT INTO episodic_memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TABLE hallucination_audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, msg TEXT);
            CREATE TABLE interaction_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE);
            CREATE TABLE skill_evolution_history (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
            CREATE TABLE tool_evolution_history (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
            CREATE TABLE engine_health_states (engine_id TEXT PRIMARY KEY);
            CREATE TABLE wind_control_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, msg TEXT);
            """
        )

    store = CoreStateStore(legacy_db)
    with sqlite3.connect(str(legacy_db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger';").fetchall()
        ws_cols = {r[1] for r in conn.execute("PRAGMA table_info(working_sessions);").fetchall()}
        preserved_ws = conn.execute(
            "SELECT active_engine, workspace_dir FROM working_sessions WHERE session_id='session_u1';"
        ).fetchone()
    assert tables == {"working_sessions", "messages"}
    assert triggers == []
    assert "engine_commands_json" not in ws_cols
    assert preserved_ws == ("agy", "/tmp/ws1")
    assert store.close() is True

    # Driver capability & inheritance checks
    assert resolve_binary("") is None
    assert not hasattr(BaseCLIAgentDriver, "execute_pty")
    assert not hasattr(BaseCLIAgentDriver, "discover_commands")
    drv_model = AgyDriver(binary_path="agy", skip_flag="", default_model="gemini-test-model")
    assert "--model" in drv_model._apply_default_model(drv_model.build_one_liner_cmd("hi"))
    assert "--model" not in AgyDriver(binary_path="agy", skip_flag="", default_model="inherit")._apply_default_model(["agy", "-p", "hi"])
    assert AgyDriver().supports_native_session is True
    assert ClaudeDriver().conversation_flag == "--resume"
    assert isinstance(PiDriver(), BaseCLIAgentDriver)
    assert isinstance(ClaudeDriver(), BaseCLIAgentDriver)
    assert isinstance(OmpDriver(), BaseCLIAgentDriver)


@pytest.mark.asyncio
async def test_native_error_verbatim_passthrough_without_429_403_lockout(tmp_path: Path):
    """Verify non-zero CLI output (including 429/403 messages) returns ANSI-cleaned verbatim with zero lockout."""
    ws = tmp_path / "err_ws"
    ws.mkdir()
    err_bin = tmp_path / "mock_err_agy.py"
    _write_exec(
        err_bin,
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args: print('agy 1.5.9'); sys.exit(0)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "if 'trigger429' in p:\n"
        "    sys.stderr.write('\\x1b[31mHTTP 429 Too Many Requests from upstream\\x1b[0m\\n')\n"
        "    sys.exit(1)\n"
        "if 'trigger403' in p:\n"
        "    sys.stderr.write('\\x1b[31mHTTP 403 Forbidden: retry upstream\\x1b[0m\\n')\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'status': 'SUCCESS', 'response': 'RECOVERED_WITHOUT_LOCKOUT'}))\n",
    )
    cfg = {
        "system": {"workspace_dir": str(ws)},
        "engines": {"primary": "agy", "agy": {"binary": str(err_bin), "dangerously_skip_permissions_flag": ""}},
    }
    s = Settings(_env_file=None, WORKSPACE_ROOT=ws, DB_PATH=tmp_path / "err.db", AGY_BINARY=str(err_bin))
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, settings=s)

    try:
        assert "已进入 [AGY]" in await gw.process_inbound_message("u_err", "/agy")

        r_429 = await gw.process_inbound_message("u_err", "trigger429")
        assert "HTTP 429 Too Many Requests from upstream" in r_429
        assert "\x1b" not in r_429

        r_403 = await gw.process_inbound_message("u_err", "trigger403")
        assert "HTTP 403 Forbidden: retry upstream" in r_403
        assert "\x1b" not in r_403

        assert "RECOVERED_WITHOUT_LOCKOUT" in await gw.process_inbound_message("u_err", "normal turn")
        assert "已退出 [AGY]" in await gw.process_inbound_message("u_err", "/exit")
        assert "已进入 [AGY]" in await gw.process_inbound_message("u_err", "/agy")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unrestricted_parallel_engine_passthrough(tmp_path: Path):
    """Verify parallel engine calls inside session run concurrently without artificial blocking."""
    ws = tmp_path / "ws"
    slots_dir = tmp_path / "slots"
    ws.mkdir()
    slots_dir.mkdir()

    par_bin = tmp_path / "mock_par_agy.py"
    _write_exec(
        par_bin,
        "import json, os, sys, time\n"
        f"SLOTS = {str(slots_dir)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args: print('agy 1.5.9'); sys.exit(0)\n"
        "pf = os.path.join(SLOTS, f'{os.getpid()}.pid')\n"
        "open(pf, 'w').write('1')\n"
        "time.sleep(0.25)\n"
        "peak = len(os.listdir(SLOTS))\n"
        "time.sleep(0.1)\n"
        "os.remove(pf)\n"
        "print(json.dumps({'status':'SUCCESS','response':f'\\x1b[32mPEAK={peak}\\x1b[0m'}))\n",
    )
    cfg = {
        "system": {"workspace_dir": str(ws)},
        "engines": {
            "primary": "agy",
            "agy": {
                "binary": str(par_bin),
                "dangerously_skip_permissions_flag": "",
            },
        },
    }
    s = Settings(_env_file=None, WORKSPACE_ROOT=ws, DB_PATH=tmp_path / "par.db", AGY_BINARY=str(par_bin))
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, settings=s)

    try:
        assert "已进入 [AGY]" in await gw.process_inbound_message("u_par", "/agy")
        t0 = time.monotonic()
        replies = await asyncio.gather(*(gw.process_inbound_message("u_par", f"task {i}") for i in range(6)))
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0
        peaks = [int(r.split("PEAK=")[1]) for r in replies]
        assert max(peaks) >= 3
        assert all("\x1b" not in r for r in replies)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_webhook_xml_header_malformed_split_and_ansi_empty_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify XML declaration parsing, 400 on malformed XML, split webhook push, and pure-ANSI stripping."""
    from gateway.terminal_cleaner import ANSICleaner
    import main

    ws = tmp_path / "wh_ws"
    ws.mkdir()
    ansi_bin = tmp_path / "mock_ansi_agy.py"
    _write_exec(
        ansi_bin,
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args: print('agy 1.5.9'); sys.exit(0)\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "if p == 'pure_ansi':\n"
        "    print('\\x1b[32m\\x1b[0m')\n"
        "else:\n"
        "    print('X' * 35)\n",
    )

    cfg = {
        "system": {"workspace_dir": str(ws)},
        "engines": {"primary": "agy", "agy": {"binary": str(ansi_bin), "dangerously_skip_permissions_flag": ""}},
    }
    s = Settings(
        _env_file=None,
        WORKSPACE_ROOT=ws,
        DB_PATH=tmp_path / "wh_edge.db",
        ALLOWED_USERS=["wx_xml"],
        WECHAT_TOKEN="wh_tok_edge",
        WEBHOOK_REQUIRE_SIGNATURE=False,
        WECHAT_PUSH_URL="https://push.example.local/send",
        WECHAT_MAX_MSG_LEN=15,
        AGY_BINARY=str(ansi_bin),
    )
    store = CoreStateStore(s.DB_PATH)
    hub = MultiCLIHub(config=cfg, store=store, settings=s)
    mon = HealthMonitor(settings=s, store=store)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, health_monitor=mon, settings=s)

    pushed_chunks: List[str] = []

    async def fake_post(self, url, *, json=None, headers=None, **kwargs):
        pushed_chunks.append(json["content"])
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(main, "settings", s)
    monkeypatch.setattr(main, "gateway", gw)
    monkeypatch.setattr(main, "hub", hub)
    monkeypatch.setattr(main, "monitor", mon)
    monkeypatch.setattr(main, "ilink_worker", None)

    try:
        cleaner = ANSICleaner()
        assert cleaner.clean_text("Step 1\nStep 1\nDone") == "Step 1\nDone"

        assert "已进入 [AGY]" in await gw.process_inbound_message("wx_xml", "/agy")
        pure_ansi_reply = await gw.process_inbound_message("wx_xml", "pure_ansi")
        assert "\x1b" not in pure_ansi_reply
        assert pure_ansi_reply == ""

        with TestClient(main.app) as client:
            xml_with_header = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<xml><FromUserName><![CDATA[wx_xml]]></FromUserName><MsgId>hdr_1</MsgId>"
                "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[long_output]]></Content></xml>"
            )
            res_ok = client.post(
                "/wechat/webhook",
                content=xml_with_header,
                headers={"X-WeChat-Token": "wh_tok_edge"},
            )
            assert res_ok.status_code == 200
            assert res_ok.json()["reply"] == "X" * 35
            assert len(pushed_chunks) == 3
            assert "".join(pushed_chunks) == "X" * 35

            malformed_xml = "<?xml version='1.0'?><xml><FromUserName>wx_xml</unclosed>"
            res_bad = client.post(
                "/wechat/webhook",
                content=malformed_xml,
                headers={"X-WeChat-Token": "wh_tok_edge"},
            )
            assert res_bad.status_code == 400
    finally:
        store.close()
