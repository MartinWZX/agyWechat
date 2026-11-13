"""Tests for Two-State Engine FSM, Strict /exit Switching, and Real-Time Dynamic Command Mapping."""
import json
import os
from pathlib import Path
import stat

import pytest

from bridge.engine_command_discovery import discover_engine_commands, resolve_binary
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.wechat_gateway import WeChatGateway
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from rsi.evolution_engine import EvolutionEngine
from skills.skill_manager import SkillManager
from telemetry.health_monitor import HealthMonitor


def _write_exec(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return str(path)


def _make_agy_mock_script(path: Path, version: str, acp_cmds: list, help_subcmds: list, exec_log: Path) -> str:
    """Create a real executable simulating ACP + --help + subcommands + -p prompts."""
    subcmd_lines = "\n".join(f"  {n:<15} {d}" for n, d in help_subcmds)
    body = (
        "import json, sys\n"
        f"VERSION = {version!r}\n"
        f"ACP_CMDS = {acp_cmds!r}\n"
        f"SUBCMDS_TXT = {subcmd_lines!r}\n"
        f"LOG_FILE = {str(exec_log)!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print(VERSION); sys.exit(0)\n"
        "if '--help' in args:\n"
        "    print('Usage: mock-agy [flags]\\n"
        "  --model    Model for the current CLI session\\n"
        "  --mode     Set the agent execution mode (accept-edits, auto, plan)\\n"
        "  --effort   Reasoning effort (low|medium|high|xhigh|max)\\n\\n"
        "Available subcommands:\\n' + SUBCMDS_TXT); sys.exit(0)\n"
        "if len(args) >= 2 and args[0] == 'acp' and args[1] == '--stdio':\n"
        "    for line in sys.stdin:\n"
        "        msg = json.loads(line)\n"
        "        if msg.get('method') == 'initialize':\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':1,'agentInfo':{'name':'MockAGY','version':VERSION}}}), flush=True)\n"
        "        elif msg.get('method') == 'session/new':\n"
        "            sid = 'sess_live_1'\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'sessionId':sid,'models':{'availableModels':[{'modelId':'flash-test','name':'Flash'}]},'modes':{'availableModes':[{'id':'default','name':'Default'}]}}}), flush=True)\n"
        "            print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':'available_commands_update','availableCommands':ACP_CMDS}}}), flush=True)\n"
        "    sys.exit(0)\n"
        "if args and args[0] in ('models', 'skills', 'agents', 'agent', 'mcp', 'plugin', 'changelog', 'install'):\n"
        "    print(f'LIVE_{args[0].upper()}@{VERSION} args={args[1:]}'); sys.exit(0)\n"
        "with open(LOG_FILE, 'a', encoding='utf-8') as lf:\n"
        "    lf.write(json.dumps(args, ensure_ascii=False) + '\\n')\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ' '.join(args)\n"
        "first_line = prompt.splitlines()[0] if prompt else ''\n"
        "print(f'OK[{VERSION}] first_line={first_line}')\n"
    )
    return _write_exec(path, body)


def _make_pi_list_cmd_script(path: Path) -> str:
    """Create a real executable simulating a CLI using --list-commands JSON discovery."""
    body = (
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('pi-cli 3.1.0'); sys.exit(0)\n"
        "if '--help' in args:\n"
        "    print('Usage: mock-pi [options]\\n  --list-commands   Print commands JSON'); sys.exit(0)\n"
        "if '--list-commands' in args:\n"
        "    print(json.dumps([{'name':'review','description':'Code review workflow'},{'name':'tree','description':'Inspect session tree'}])); sys.exit(0)\n"
        "print('<facts_anchor>ANCHOR_1</facts_anchor>')\n"
        "print('<causal_chain>ANCHOR_1 pi executed</causal_chain>')\n"
        "print('<execution_manifest>{\"files_created\":[],\"files_modified\":[],\"commands_executed\":[]}</execution_manifest>')\n"
        "print('<final_conclusion>PI_EXECUTED</final_conclusion>')\n"
    )
    return _write_exec(path, body)


@pytest.mark.asyncio
async def test_two_state_engine_fsm_exit_and_dynamic_version_refresh(tmp_path: Path):
    """Verify Non-Engine permissions, In-Engine /command isolation, /exit switch rule, and upgrade discovery."""
    exec_log = tmp_path / "exec.log"
    agy_path = tmp_path / "mock_agy.py"
    pi_path = tmp_path / "mock_pi.py"

    _make_agy_mock_script(
        agy_path,
        version="1.5.8",
        acp_cmds=[
            {"name": "plan", "description": "Create plan"},
            {"name": "grill-me", "description": "Interview workflow"},
            {"name": "theme", "description": "Change CLI color theme"},
        ],
        help_subcmds=[
            ("acp", "Start ACP server"),
            ("agent", "List available agents"),
            ("agents", "List available agents"),
            ("changelog", "Show changelog"),
            ("mcp", "Manage MCP servers"),
            ("plugin", "Manage plugins"),
            ("plugins", "Manage plugins"),
            ("models", "List available models"),
            ("install", "Install shell integrations"),
            ("help", "Show help"),
        ],
        exec_log=exec_log,
    )
    _make_pi_list_cmd_script(pi_path)

    cfg = {
        "system": {"workspace_dir": str(tmp_path), "snapshot_ignore_patterns": ["*.py", "*.db*", "*.log", "skills"]},
        "engines": {
            "max_concurrency": 2,
            "failover_order": ["agy", "pi"],
            "agy": {"binary": str(agy_path), "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
            "pi": {"binary": str(pi_path), "dangerously_skip_permissions_flag": "--no-confirm"},
            "claude": {"binary": str(tmp_path / "nonexistent_claude")},
            "omp": {"binary": str(tmp_path / "nonexistent_omp")},
        },
        "wind_control": {"min_call_interval_seconds": 0.0, "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0}},
        "crystallization": {"faq_solidify_threshold": 3, "auto_cron_enabled": False},
    }
    s = Settings(WORKSPACE_ROOT=tmp_path, DB_PATH=tmp_path / "fsm.db", ADMIN_USERS=["u1"])
    store = CoreStateStore(s.DB_PATH)
    sm = SkillManager(workspace_root=tmp_path, settings=s)
    gov = WindControlGovernor(store_or_db=store, config=cfg)
    cryst = PatternCrystallizer(store=store, skill_manager=sm, settings=s, workspace_dir=str(tmp_path))
    rsi = EvolutionEngine(store=store, skill_manager=sm, workspace_dir=str(tmp_path), config=cfg)
    hub = MultiCLIHub(config=cfg, governor=gov, store=store, evolution_engine=rsi, crystallizer=cryst)
    mon = HealthMonitor(governor=gov, settings=s, store=store)
    gw = WeChatGateway(
        config=cfg, hub=hub, store=store, governor=gov,
        crystallizer=cryst, skill_manager=sm, health_monitor=mon, settings=s, rsi_engine=rsi,
    )

    # 1. Non-Engine Default State: minimal view + /workspace commands work; aliases & business cmds blocked
    r_status = await gw.process_inbound_message("u1", "/status")
    assert "Google agy 微信智能体运行看板" in r_status and "非引擎默认状态" in r_status
    r_engines = await gw.process_inbound_message("u1", "/engines")
    assert "/agy" in r_engines and "/pi" in r_engines
    for old_alias in ("/cd /tmp", "/defense", "/dashboard", "/evolution", "/rsi"):
        assert "非引擎默认状态" in await gw.process_inbound_message("u1", old_alias)
    r_blocked_biz = await gw.process_inbound_message("u1", "帮我重构代码")
    assert "非引擎默认状态" in r_blocked_biz
    r_blocked_cmd = await gw.process_inbound_message("u1", "/plan 重构")
    assert "非引擎默认状态" in r_blocked_cmd
    r_blocked_arg = await gw.process_inbound_message("u1", "/agy 重构代码")
    assert "非引擎默认状态" in r_blocked_arg
    r_missing = await gw.process_inbound_message("u1", "/claude")
    assert "无法进入引擎 'claude'" in r_missing
    assert gw.det_router.engine_router.get_session_state("u1")["active_engine"] == ""

    # 2. Enter /agy -> live full CLI discovery executes immediately (zero hidden subset, zero duplicate aliases)
    r_enter = await gw.process_inbound_message("u1", "/agy")
    assert "已进入 [AGY]" in r_enter and "1.5.8" in r_enter
    for expected in ("/plan", "/grill-me", "/theme", "/changelog", "/mcp", "/plugin", "/install", "/models", "/model", "/agent", "/clear", "/exit"):
        assert expected in r_enter
    for dup_alias in ("/agents", "/plugins", "/new"):
        assert dup_alias not in r_enter

    # 3. Inside AGY: gateway /status & duplicate aliases rejected cleanly; full CLI subcommands execute
    assert "动态命令映射中不存在 '/status'" in await gw.process_inbound_message("u1", "/status")
    assert "动态命令映射中不存在 '/agents'" in await gw.process_inbound_message("u1", "/agents")
    assert "动态命令映射中不存在 '/plugins'" in await gw.process_inbound_message("u1", "/plugins")
    assert "动态命令映射中不存在 '/new'" in await gw.process_inbound_message("u1", "/new")
    r_eng_help = await gw.process_inbound_message("u1", "/help")
    assert "AGY 引擎全量命令清单" in r_eng_help and "/grill-me" in r_eng_help and "/changelog" in r_eng_help and "/mcp" in r_eng_help
    assert "LIVE_MODELS@1.5.8" in await gw.process_inbound_message("u1", "/models")
    assert "LIVE_CHANGELOG@1.5.8" in await gw.process_inbound_message("u1", "/changelog")
    assert "LIVE_MCP@1.5.8 args=['list']" in await gw.process_inbound_message("u1", "/mcp list")
    assert "LIVE_PLUGIN@1.5.8 args=['list']" in await gw.process_inbound_message("u1", "/plugin list")
    assert "LIVE_INSTALL@1.5.8" in await gw.process_inbound_message("u1", "/install")
    assert "Usage: mock-agy" in await gw.process_inbound_message("u1", "/acp")
    assert "LIVE_AGENT@1.5.8" in await gw.process_inbound_message("u1", "/agent")
    assert "已设置为：flash-agent" in await gw.process_inbound_message("u1", "/agent flash-agent")

    # Configure session overrides (/model, /mode, /effort) and verify CLI propagation
    assert "flash-test" in await gw.process_inbound_message("u1", "/model")
    assert "已设置为：flash-test" in await gw.process_inbound_message("u1", "/model flash-test")
    assert "参数无效" in await gw.process_inbound_message("u1", "/effort ultra-bad")
    assert "已设置为：high" in await gw.process_inbound_message("u1", "/effort high")

    # /plan forwards cleanly with /plan at line 1 and session --model/--effort/--agent flags
    r_plan = await gw.process_inbound_message("u1", "/plan inspect router")
    assert "OK[1.5.8] first_line=/plan inspect router" in r_plan
    log_txt = exec_log.read_text(encoding="utf-8")
    assert '"--model", "flash-test"' in log_txt and '"--effort", "high"' in log_txt and '"--agent", "flash-agent"' in log_txt

    # Direct switch /pi blocked inside AGY without /exit
    assert "必须" in await gw.process_inbound_message("u1", "/pi") or "请先发送 /exit" in await gw.process_inbound_message("u1", "/pi")

    # 4. /exit returns cleanly to Non-Engine default state
    assert "已退出 [AGY]" in await gw.process_inbound_message("u1", "/exit")
    assert gw.det_router.engine_router.get_session_state("u1")["active_engine"] == ""
    assert "Google agy 微信智能体运行看板" in await gw.process_inbound_message("u1", "/status")

    # 5. Simulate AGY binary upgrade (1.5.8 -> 2.0.0): removes /grill-me, adds /deep-audit & subcommand skills
    _make_agy_mock_script(
        agy_path,
        version="2.0.0",
        acp_cmds=[
            {"name": "plan", "description": "Create plan v2"},
            {"name": "deep-audit", "description": "Deep codebase audit"},
        ],
        help_subcmds=[
            ("acp", "Start ACP server"),
            ("models", "List available models"),
            ("skills", "List installed skills"),
            ("help", "Show help"),
        ],
        exec_log=exec_log,
    )

    r_reenter = await gw.process_inbound_message("u1", "/agy")
    assert "2.0.0" in r_reenter and "/deep-audit" in r_reenter and "/skills" in r_reenter
    assert "/grill-me" not in r_reenter
    assert "LIVE_SKILLS@2.0.0" in await gw.process_inbound_message("u1", "/skills")
    assert "不存在 '/grill-me'" in await gw.process_inbound_message("u1", "/grill-me")
    assert "OK[2.0.0] first_line=/deep-audit repo" in await gw.process_inbound_message("u1", "/deep-audit repo")

    # 6. Exit AGY and switch cleanly to /pi (--list-commands JSON probe)
    await gw.process_inbound_message("u1", "/exit")
    r_enter_pi = await gw.process_inbound_message("u1", "/pi")
    assert "已进入 [PI]" in r_enter_pi and "/review" in r_enter_pi and "/tree" in r_enter_pi
    r_review = await gw.process_inbound_message("u1", "/review main.py")
    assert "PI_EXECUTED" in r_review


@pytest.mark.asyncio
async def test_workspace_navigation_external_coding_and_native_sessions(tmp_path: Path):
    """Verify Non-Engine /workspace /pwd /ls guards, external repo coding, TUI ops, and /clear sessions."""
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
        "if '--help' in args:\n"
        "    print('Usage: mock-native-agy [flags]\\n"
        "  --model <name>            Model to use\\n"
        "  --output-format <fmt>     Output format (text, json)\\n"
        "  --conversation <id>       Continue conversation by id\\n\\n"
        "Commands:\\n  acp         Start ACP server\\n  add-dir     Add directory\\n  btw         Side question\\n  codesearch  Code search\\n  context     Context usage\\n  copy        Copy last reply\\n  diff        Show diff\\n  fork        Fork conversation\\n  open        Open file\\n  rename      Rename session\\n  resume      Switch conversation'); sys.exit(0)\n"
        "if len(args) >= 2 and args[0] == 'acp' and args[1] == '--stdio':\n"
        "    for line in sys.stdin:\n"
        "        msg = json.loads(line)\n"
        "        if msg.get('method') == 'initialize':\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':1,'agentInfo':{'name':'NativeAGY','version':'1.6.0'}}}), flush=True)\n"
        "        elif msg.get('method') == 'session/new':\n"
        "            sid = 's1'\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'sessionId':sid,'models':{'availableModels':[{'modelId':'gemini-3.8-flash-low','name':'Flash'}]},'modes':{'availableModes':[]}}}), flush=True)\n"
        "            print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':'available_commands_update','availableCommands':[{'name':'plan','description':'Create plan'},{'name':'usage','description':'View quota usage'}]}}}), flush=True)\n"
        "    sys.exit(0)\n"
        "with open(LOG_FILE, 'a', encoding='utf-8') as lf:\n"
        "    lf.write(json.dumps({'cwd': os.getcwd(), 'args': args}, ensure_ascii=False) + '\\n')\n"
        "conv = args[args.index('--conversation') + 1] if '--conversation' in args else ''\n"
        "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "if 'create calc.py' in prompt:\n"
        "    with open('calc.py', 'w', encoding='utf-8') as f:\n"
        "        f.write('def add(a, b):\\n    return a + b\\n')\n"
        "    out_conv = conv or 'conv_thread_1'\n"
        "    resp = f'CREATED_CALC in {os.getcwd()}'\n"
        "elif prompt == '/usage':\n"
        "    out_conv = ''\n"
        "    resp = 'PANEL_USAGE_OK'\n"
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
        "system": {
            "workspace_dir": str(default_ws),
            "snapshot_ignore_patterns": ["*.db*", "*.log", ".git"],
        },
        "engines": {
            "max_concurrency": 2,
            "failover_order": ["agy"],
            "agy": {"binary": str(cli_path), "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"},
        },
        "wind_control": {"min_call_interval_seconds": 0.0, "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0}},
        "crystallization": {"faq_solidify_threshold": 3, "auto_cron_enabled": False},
    }
    s = Settings(WORKSPACE_ROOT=default_ws, DB_PATH=tmp_path / "ws_sess.db", ADMIN_USERS=["u_ws"])
    store = CoreStateStore(s.DB_PATH)
    sm = SkillManager(workspace_root=default_ws, settings=s)
    gov = WindControlGovernor(store_or_db=store, config=cfg)
    cryst = PatternCrystallizer(store=store, skill_manager=sm, settings=s, workspace_dir=str(default_ws))
    rsi = EvolutionEngine(store=store, skill_manager=sm, workspace_dir=str(default_ws), config=cfg)
    hub = MultiCLIHub(config=cfg, governor=gov, store=store, evolution_engine=rsi, crystallizer=cryst)
    gw = WeChatGateway(
        config=cfg, hub=hub, store=store, governor=gov,
        crystallizer=cryst, skill_manager=sm, settings=s, rsi_engine=rsi,
    )

    # 1. Inspect default workspace, list entries, test security boundaries & zero /cd alias
    r_pwd = await gw.process_inbound_message("u_ws", "/pwd")
    assert str(default_ws.resolve()) in r_pwd
    r_ls = await gw.process_inbound_message("u_ws", "/ls")
    assert "sub_a/" in r_ls
    assert "非引擎默认状态" in await gw.process_inbound_message("u_ws", f"/cd {repo_b}")
    assert "禁止将工作目录切换至系统目录" in await gw.process_inbound_message("u_ws", "/workspace /etc")
    ssh_test = os.path.expanduser("~/.ssh")
    if Path(ssh_test).exists():
        assert "禁止访问凭据敏感目录" in await gw.process_inbound_message("u_ws", "/workspace ~/.ssh")

    # 2. Switch workspace to external git repo via /workspace, enter /agy in that workspace
    r_ws = await gw.process_inbound_message("u_ws", f"/workspace {repo_b}")
    assert str(repo_b.resolve()) in r_ws and ".git" in r_ws
    r_enter = await gw.process_inbound_message("u_ws", "/agy")
    assert str(repo_b.resolve()) in r_enter and "/clear" in r_enter and "/new" not in r_enter
    assert "原生多轮对话延续" in r_enter

    # Set model so we verify /clear preserves session config & workspace
    assert "gemini-3.8-flash-low" in await gw.process_inbound_message("u_ws", "/model gemini-3.8-flash-low")

    # 3. Turn 1: create calc.py inside repo_b -> MUST persist (no false rollback!) and show diff note
    r_t1 = await gw.process_inbound_message("u_ws", "please create calc.py")
    assert "CREATED_CALC" in r_t1 and str(repo_b.resolve()) in r_t1
    assert "本轮真实文件变更" in r_t1 and "calc.py" in r_t1
    assert (repo_b / "calc.py").is_file()

    # Verify DB persisted conversation_id and turn_count
    st1 = gw.det_router.engine_router.get_session_state("u_ws")
    assert st1["meta"]["conversation_id"] == "conv_thread_1"
    assert st1["meta"]["turn_count"] == 1

    # Verify TUI/workspace ops natively inside WeChat (/codesearch, /open, /context, /add-dir)
    assert "calc.py" in await gw.process_inbound_message("u_ws", "/codesearch def add")
    assert "return a + b" in await gw.process_inbound_message("u_ws", "/open calc.py")
    assert "conv_thread_1" in await gw.process_inbound_message("u_ws", "/context")
    assert str(default_ws.resolve()) in await gw.process_inbound_message("u_ws", f"/add-dir {default_ws}")

    # 4. Turn 2: automatically passes --conversation conv_thread_1 and --output-format json
    r_t2 = await gw.process_inbound_message("u_ws", "what was my token?")
    assert "RESUMED_MEMORY: TOKEN_Alpha_99" in r_t2
    assert "RESUMED_MEMORY: TOKEN_Alpha_99" in await gw.process_inbound_message("u_ws", "/copy")

    # Verify /btw and /usage panel run raw without altering conv_thread_1, and /rename + /fork + /resume work
    r_btw = await gw.process_inbound_message("u_ws", "/btw quick side question")
    assert "FRESH_THREAD_NO_MEMORY" in r_btw
    assert "PANEL_USAGE_OK" in await gw.process_inbound_message("u_ws", "/usage")
    assert gw.det_router.engine_router.get_session_state("u_ws")["meta"]["conversation_id"] == "conv_thread_1"
    assert "fix-calc" in await gw.process_inbound_message("u_ws", "/rename fix-calc")
    assert "已分叉开启新分支会话" in await gw.process_inbound_message("u_ws", "/fork branch-two")
    assert "conv_thread_1" in await gw.process_inbound_message("u_ws", "/resume conv_thread_1")

    # 5. Send /clear -> clears conversation_id & turn_count while keeping workspace and model
    r_clear1 = await gw.process_inbound_message("u_ws", "/clear")
    assert "已开启全新会话" in r_clear1 and str(repo_b.resolve()) in r_clear1
    st_new = gw.det_router.engine_router.get_session_state("u_ws")
    assert st_new["meta"]["conversation_id"] == "" and st_new["meta"]["turn_count"] == 0
    assert st_new["meta"]["config"]["model"] == "gemini-3.8-flash-low"

    # 6. Turn 3: fresh session (no conv_thread_1 passed), then /clear resets again cleanly
    r_t3 = await gw.process_inbound_message("u_ws", "do you remember?")
    assert "FRESH_THREAD_NO_MEMORY" in r_t3
    assert "已开启全新会话" in await gw.process_inbound_message("u_ws", "/clear")

    # Check subprocess invocation logs for exact cwd & flags (Turn1, Turn2, /btw, /usage, Turn3)
    records = [json.loads(line) for line in exec_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 5
    assert all(rec["cwd"] == str(repo_b.resolve()) for rec in records)
    assert "--conversation" not in records[0]["args"]
    assert "--add-dir" in records[1]["args"]
    assert "--conversation" in records[1]["args"] and "conv_thread_1" in records[1]["args"]
    assert "--conversation" not in records[2]["args"]  # /btw skips --conversation
    assert records[3]["args"][records[3]["args"].index("-p") + 1] == "/usage"  # raw panel slash, no wrapping
    assert "--conversation" not in records[3]["args"]
    assert "--conversation" not in records[4]["args"]  # after /clear

    # 7. /exit preserves selected external workspace and /workspace switches back
    r_exit = await gw.process_inbound_message("u_ws", "/exit")
    assert str(repo_b.resolve()) in r_exit
    assert str(repo_b.resolve()) in await gw.process_inbound_message("u_ws", "/pwd")
    r_ws_back = await gw.process_inbound_message("u_ws", f"/workspace {default_ws}")
    assert str(default_ws.resolve()) in r_ws_back


@pytest.mark.asyncio
async def test_live_installed_agy_dynamic_command_discovery():
    """Verify real installed `agy` binary exposes full CLI commands with zero duplicate aliases."""
    resolved = resolve_binary("agy")
    if not resolved:
        pytest.skip("agy binary not installed on host")
    res = await discover_engine_commands("agy", resolved, os.getcwd(), timeout=20.0)
    assert res.available
    assert res.session_capabilities.get("supports_json_output") is True
    assert res.session_capabilities.get("conversation_flag") == "--conversation"
    assert res.session_capabilities.get("supports_native_session") is True
    for cli_cmd in (
        "plan", "goal", "compact", "boost", "clear", "exit", "help",
        "models", "model", "mode", "effort", "agent", "mcp", "plugin",
        "changelog", "install", "update", "remote-control", "acp",
        "usage", "credits", "config", "hooks", "permissions", "skills",
        "add-dir", "diff", "codesearch", "context", "fork", "resume", "rename", "copy", "open", "btw",
    ):
        assert cli_cmd in res.command_map, f"Missing CLI command: {cli_cmd}"
    for dup_alias in ("new", "cs", "settings", "quit", "branch", "switch", "undo", "quota", "agents", "plugins"):
        assert dup_alias not in res.command_map, f"Duplicate alias should not exist: {dup_alias}"
        assert dup_alias in res.filtered_commands
    assert len(res.commands) >= 50
