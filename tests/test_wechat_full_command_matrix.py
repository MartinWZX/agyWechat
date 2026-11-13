"""Exhaustive WeChat End-to-End Command Matrix Tests (iLink, Webhook XML/JSON & Live AGY)."""
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Dict, List

from fastapi.testclient import TestClient
import httpx
import pytest

from bridge.engine_command_discovery import resolve_binary
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.ilink_gateway import ContextTokenStore, ILinkGatewayWorker
from gateway.wechat_gateway import WeChatGateway
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from rsi.evolution_engine import EvolutionEngine
from skills.skill_manager import SkillManager


def _make_full_agy_mock(path: Path, exec_log: Path) -> str:
    """Create a realistic executable exposing all 56 canonical AGY commands and 10 filtered aliases."""
    subcmds = "\n".join([
        "  acp             Start the Agent Client Protocol (ACP) stdio server",
        "  agent           List available agents",
        "  agents          alias for agent",
        "  changelog       Show changelog and release notes",
        "  install         Configure environment paths and shell settings",
        "  mcp             Manage MCP servers (add, remove, list, enable, disable)",
        "  mic-serve       Serve this machine's microphone to a CLI on another host",
        "  models          List available models",
        "  plugin          Manage plugins (install, uninstall, list, enable, disable)",
        "  plugins         alias for plugin",
        "  remote-control  Manage the remote-control background daemon",
        "  update          Update CLI",
        "  help            Show help for subcommands",
    ])
    acp_workflows = [
        ("agy-customizations", "Antigravity Customization System guide"),
        ("antigravity-guide", "Antigravity CLI quick reference"),
        ("boost", "Deep thinking multi-perspective workflow"),
        ("browser", "Web browsing and interaction agent"),
        ("compact", "Compact the conversation now"),
        ("goal", "Long-running goal workflow"),
        ("grill-me", "Structured design interview"),
        ("learn", "Capture reusable skills and rules"),
        ("plan", "Plan carefully before executing a task"),
        ("schedule", "Recurring cron or one-time schedule"),
        ("swarm", "Multi-agent parallel swarm"),
        ("teamwork", "Coordinate agent team on large projects"),
        ("zero-hallucination-git-gate", "Git zero-leakage and AST gate"),
    ]
    list_cmds = [
        {"name": "add-dir", "description": "Add a directory to the workspace"},
        {"name": "artifact", "description": "View and review artifacts"},
        {"name": "btw", "description": "Ask a side question without interrupting main thread"},
        {"name": "clear", "description": "Clear conversation and start a new one"},
        {"name": "codesearch", "description": "Search code in workspace"},
        {"name": "config", "description": "Open settings panel"},
        {"name": "context", "description": "Visualize current context usage"},
        {"name": "copy", "description": "Copy last planner response"},
        {"name": "credits", "description": "Show remaining credits"},
        {"name": "diff", "description": "View uncommitted changes"},
        {"name": "exit", "description": "Exit the CLI"},
        {"name": "feedback", "description": "Submit qualitative feedback"},
        {"name": "fork", "description": "Fork current conversation"},
        {"name": "hooks", "description": "Manage hook configurations"},
        {"name": "keybindings", "description": "Set custom keybindings"},
        {"name": "logout", "description": "Log out"},
        {"name": "open", "description": "Open a workspace file"},
        {"name": "permissions", "description": "Manage tool permissions"},
        {"name": "projects", "description": "Create and edit projects"},
        {"name": "record", "description": "Dictate prompt via microphone"},
        {"name": "rename", "description": "Rename current conversation"},
        {"name": "resume", "description": "Browse and resume past conversations"},
        {"name": "rewind", "description": "Rewind conversation"},
        {"name": "skills", "description": "List available skills"},
        {"name": "statusline", "description": "Toggle statusline"},
        {"name": "tasks", "description": "View background tasks"},
        {"name": "title", "description": "Toggle window title"},
        {"name": "usage", "description": "View model quota usage"},
        {"name": "voice", "description": "Voice dictation mode"},
        {"name": "new", "description": "alias for clear"},
        {"name": "cs", "description": "alias for codesearch"},
        {"name": "settings", "description": "alias for config"},
        {"name": "quit", "description": "alias for exit"},
        {"name": "branch", "description": "alias for fork"},
        {"name": "switch", "description": "alias for resume"},
        {"name": "undo", "description": "alias for rewind"},
        {"name": "quota", "description": "alias for usage"},
    ]
    body = (
        "import json, os, sys\n"
        f"LOG = {str(exec_log)!r}\n"
        f"SUB = {subcmds!r}\n"
        f"ACP = {[{'name': n, 'description': d} for n, d in acp_workflows]!r}\n"
        f"LIST = {list_cmds!r}\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        "    print('agy 1.5.8'); sys.exit(0)\n"
        "if '--help' in args and not (args and args[0] in ('acp', 'agent', 'install', 'mcp', 'mic-serve', 'plugin', 'remote-control', 'update')):\n"
        "    print('Usage: agy [flags]\\n"
        "  --model <m>               Model for session\\n"
        "  --mode <m>                Mode (default, accept-edits, auto, plan)\\n"
        "  --effort <e>              Effort (low|medium|high|xhigh|max)\\n"
        "  --agent <a>               Subagent override\\n"
        "  --add-dir <d>             Extra directory\\n"
        "  --list-commands           Print slash commands JSON\\n"
        "  --output-format <f>       Output format (text, json)\\n"
        "  --conversation <id>       Continue conversation\\n\\n"
        "Commands:\\n' + SUB); sys.exit(0)\n"
        "if '--list-commands' in args:\n"
        "    print(json.dumps(LIST)); sys.exit(0)\n"
        "if len(args) >= 2 and args[0] == 'acp' and args[1] == '--stdio':\n"
        "    for line in sys.stdin:\n"
        "        m = json.loads(line)\n"
        "        if m.get('method') == 'initialize':\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':1,'agentInfo':{'name':'MockAGY','version':'1.5.8'}}}), flush=True)\n"
        "        elif m.get('method') == 'session/new':\n"
        "            sid = 's1'\n"
        "            print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'sessionId':sid,'models':{'availableModels':[{'modelId':'gemini-3.8-flash-low','name':'Flash'}]},'modes':{'availableModes':[{'id':'plan','name':'Plan'},{'id':'auto','name':'Auto'}]}}}), flush=True)\n"
        "            print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':'available_commands_update','availableCommands':ACP}}}), flush=True)\n"
        "    sys.exit(0)\n"
        "if args and args[0] in ('acp', 'agent', 'changelog', 'install', 'mcp', 'mic-serve', 'models', 'plugin', 'remote-control', 'update'):\n"
        "    print(f'SUBCMD:{args[0]} ARGS={args[1:]}'); sys.exit(0)\n"
        "with open(LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'cwd': os.getcwd(), 'args': args}, ensure_ascii=False) + '\\n')\n"
        "p = args[args.index('-p') + 1] if '-p' in args else ''\n"
        "conv = args[args.index('--conversation') + 1] if '--conversation' in args else ''\n"
        "first = p.splitlines()[0].strip() if p else ''\n"
        "if first in ('/usage', '/credits', '/config', '/skills'):\n"
        "    print(json.dumps({'conversation_id': conv or 'c_panel', 'status': 'SUCCESS', 'response': f'PANEL_OK:{first}'})); sys.exit(0)\n"
        "if first in ('/hooks', '/permissions'):\n"
        "    print(json.dumps({'conversation_id': conv or 'c_panel', 'status': 'SUCCESS', 'response': '', 'command': {'name': first[1:], 'data': {'items': ['ok']}}})); sys.exit(0)\n"
        "if first == '/compact':\n"
        "    print(json.dumps({'conversation_id': conv or 'c_main', 'status': 'SUCCESS', 'response': '', 'num_turns': 1, 'usage': {'total_tokens': 123}})); sys.exit(0)\n"
        "if 'create calc.py' in p:\n"
        "    with open('calc.py', 'w', encoding='utf-8') as cf:\n"
        "        cf.write('def add(a, b):\\n    return a + b\\n')\n"
        "    print(json.dumps({'conversation_id': 'c_main', 'status': 'SUCCESS', 'response': 'CREATED calc.py'})); sys.exit(0)\n"
        "print(json.dumps({'conversation_id': conv or 'c_main', 'status': 'SUCCESS', 'response': f'EXEC:{first}|conv={conv}'}))\n"
    )
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return str(path)


async def _send_ilink(
    worker: ILinkGatewayWorker, captured_sends: List[Dict[str, Any]], user_id: str, text: str, as_voice: bool = False
) -> str:
    """Push one WeChat text or voice message through ILinkGatewayWorker and return reassembled reply."""
    captured_sends.clear()
    msg_item = (
        {"type": 3, "voice_item": {"text": text}}
        if as_voice
        else {"type": 1, "text_item": {"text": text}}
    )
    await worker._handle_message({
        "from_user_id": user_id,
        "msg_id": f"m_{len(text)}_{os.urandom(3).hex()}",
        "context_token": "ctx_tok_wechat",
        "item_list": [msg_item],
    })
    assert captured_sends, f"No WeChat outbound message emitted for: {text}"
    for sent in captured_sends:
        chunk_txt = sent["msg"]["item_list"][0]["text_item"]["text"]
        assert 0 < len(chunk_txt) <= worker.settings.WECHAT_MAX_MSG_LEN
    return "\n".join(s["msg"]["item_list"][0]["text_item"]["text"] for s in captured_sends)


def _build_wechat_stack(
    tmp_path: Path, ws: Path, binary: str, user_id: str, sends: List[Dict[str, Any]], high_rate: bool = False
):
    wind_cfg: Dict[str, Any] = {
        "min_call_interval_seconds": 0.0,
        "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0},
    }
    if high_rate:
        wind_cfg["limits"] = {"window_5m_max_calls": 100, "window_1h_max_calls": 500}
    cfg = {
        "system": {"workspace_dir": str(ws), "snapshot_ignore_patterns": ["*.py", "*.db*", "*.log", ".git", "skills"]},
        "engines": {"max_concurrency": 2, "failover_order": ["agy"], "agy": {"binary": binary, "dangerously_skip_permissions_flag": "--dangerously-skip-permissions"}},
        "wind_control": wind_cfg,
        "crystallization": {"faq_solidify_threshold": 3, "auto_cron_enabled": False},
    }
    settings = Settings(WORKSPACE_ROOT=ws, DB_PATH=tmp_path / "matrix.db", ADMIN_USERS=[user_id], ALLOWED_USERS=[user_id], WEIXIN_TOKEN="tok_test")
    store = CoreStateStore(settings.DB_PATH)
    sm = SkillManager(workspace_root=ws, settings=settings)
    gov = WindControlGovernor(store_or_db=store, config=cfg)
    cryst = PatternCrystallizer(store=store, skill_manager=sm, settings=settings, workspace_dir=str(ws))
    rsi = EvolutionEngine(store=store, skill_manager=sm, workspace_dir=str(ws), config=cfg)
    hub = MultiCLIHub(config=cfg, governor=gov, store=store, evolution_engine=rsi, crystallizer=cryst)
    gw = WeChatGateway(config=cfg, hub=hub, store=store, governor=gov, crystallizer=cryst, skill_manager=sm, settings=settings, rsi_engine=rsi)

    def _transport_handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/ilink/bot/sendmessage"):
            sends.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404, json={"ret": -1})

    worker = ILinkGatewayWorker(settings=settings, router=gw, token_store=ContextTokenStore(tmp_path / "tokens.json"))
    worker.pusher.client = httpx.AsyncClient(transport=httpx.MockTransport(_transport_handler))
    return gw, worker, store


@pytest.mark.asyncio
async def test_wechat_full_command_matrix_ilink_and_webhook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise all Non-Engine commands, 56 AGY commands, removed aliases, Webhook XML & iLink chunking."""
    default_ws = tmp_path / "ws_default"
    repo = tmp_path / "ws_project"
    extra_dir = tmp_path / "extra_ctx"
    (default_ws / "sub").mkdir(parents=True)
    repo.mkdir(parents=True)
    extra_dir.mkdir(parents=True)
    (repo / "README.md").write_text("# Matrix Project\n", encoding="utf-8")
    (repo / "plan.md").write_text("# Artifact Doc\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "ci@test.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    exec_log = tmp_path / "agy_matrix.log"
    mock_bin = _make_full_agy_mock(tmp_path / "mock_full_agy.py", exec_log)
    sends: List[Dict[str, Any]] = []
    gw, worker, store = _build_wechat_stack(tmp_path, default_ws, mock_bin, "wx_user_1", sends, high_rate=True)

    try:
        # --- 1. Non-Engine Default State: all 9 base cmds + /exit + guards + 5 removed aliases ---
        assert str(default_ws.resolve()) in await _send_ilink(worker, sends, "wx_user_1", "/pwd")
        assert "sub/" in await _send_ilink(worker, sends, "wx_user_1", "/ls")
        assert "Google agy" in await _send_ilink(worker, sends, "wx_user_1", "/status")
        assert "/agy" in await _send_ilink(worker, sends, "wx_user_1", "/engines")
        assert "五维防御看板" in await _send_ilink(worker, sends, "wx_user_1", "/security")
        assert await _send_ilink(worker, sends, "wx_user_1", "/audit")
        assert "演进" in await _send_ilink(worker, sends, "wx_user_1", "/rules")
        assert "/workspace <路径>" in await _send_ilink(worker, sends, "wx_user_1", "/help")
        assert "非引擎默认状态" in await _send_ilink(worker, sends, "wx_user_1", "/exit")
        for alias in ("/cd /tmp", "/defense", "/dashboard", "/evolution", "/rsi"):
            assert "非引擎默认状态" in await _send_ilink(worker, sends, "wx_user_1", alias)
        assert "禁止将工作目录切换至系统目录" in await _send_ilink(worker, sends, "wx_user_1", "/workspace /etc")
        assert "非引擎默认状态" in await _send_ilink(worker, sends, "wx_user_1", "/plan refactor")
        assert "非引擎默认状态" in await _send_ilink(worker, sends, "wx_user_1", "/agy hello")
        assert "无法进入引擎" in await _send_ilink(worker, sends, "wx_user_1", "/claude")
        assert str(repo.resolve()) in await _send_ilink(worker, sends, "wx_user_1", f"/workspace {repo}")

        # --- 2. Enter /agy via iLink voice_item and verify full 56-command catalog & zero aliases ---
        r_enter = await _send_ilink(worker, sends, "wx_user_1", "/agy", as_voice=True)
        assert "已进入 [AGY]" in r_enter and "56 个唯一命令" in r_enter
        r_help = await _send_ilink(worker, sends, "wx_user_1", "/help")
        assert len(sends) >= 1 and "AGY 引擎全量命令清单" in r_help
        assert "已在 [AGY] 引擎会话中" in await _send_ilink(worker, sends, "wx_user_1", "/agy")
        assert "请先发送 /exit" in await _send_ilink(worker, sends, "wx_user_1", "/pi")
        for blocked in ("/status", "/new", "/agents", "/plugins", "/cs", "/settings", "/quit", "/branch", "/switch", "/undo", "/quota"):
            assert f"不存在 '{blocked}'" in await _send_ilink(worker, sends, "wx_user_1", blocked)

        # --- 3. Session config (/model, /mode, /effort) + CLI subcommands + Panels ---
        assert "可选值" in await _send_ilink(worker, sends, "wx_user_1", "/model")
        assert "已设置为：gemini-3.8-flash-low" in await _send_ilink(worker, sends, "wx_user_1", "/model gemini-3.8-flash-low")
        assert "已设置为：plan" in await _send_ilink(worker, sends, "wx_user_1", "/mode plan")
        assert "已设置为：high" in await _send_ilink(worker, sends, "wx_user_1", "/effort high")
        assert "已设置为：code-reviewer" in await _send_ilink(worker, sends, "wx_user_1", "/agent code-reviewer")
        assert "已恢复默认 /agent" in await _send_ilink(worker, sends, "wx_user_1", "/agent reset")

        for sub_cmd, token in [
            ("/acp", "SUBCMD:acp"), ("/mic-serve", "SUBCMD:mic-serve"), ("/models", "SUBCMD:models"),
            ("/agent", "SUBCMD:agent"), ("/changelog", "SUBCMD:changelog"), ("/install --help", "SUBCMD:install"),
            ("/mcp list", "SUBCMD:mcp"), ("/plugin list", "SUBCMD:plugin"),
            ("/remote-control status", "SUBCMD:remote-control"), ("/update --help", "SUBCMD:update"),
        ]:
            assert token in await _send_ilink(worker, sends, "wx_user_1", sub_cmd)

        for panel_cmd, expect in [
            ("/usage", "PANEL_OK:/usage"), ("/credits", "PANEL_OK:/credits"),
            ("/config", "PANEL_OK:/config"), ("/skills", "PANEL_OK:/skills"),
            ("/hooks", "[hooks] items:"), ("/permissions", "[permissions] items:"),
        ]:
            assert expect in await _send_ilink(worker, sends, "wx_user_1", panel_cmd)

        # --- 4. Coding turn -> copy -> workspace diff -> all TUI/session tools -> 13 AI workflows ---
        r_turn = await _send_ilink(worker, sends, "wx_user_1", "please create calc.py")
        assert "CREATED calc.py" in r_turn and "calc.py" in r_turn
        assert "CREATED calc.py" in await _send_ilink(worker, sends, "wx_user_1", "/copy")
        assert "c_main" in await _send_ilink(worker, sends, "wx_user_1", "/context")
        assert "calc.py" in await _send_ilink(worker, sends, "wx_user_1", "/diff")
        assert "def add" in await _send_ilink(worker, sends, "wx_user_1", "/codesearch def add")
        assert "def add" in await _send_ilink(worker, sends, "wx_user_1", "/open calc.py")
        assert "plan.md" in await _send_ilink(worker, sends, "wx_user_1", "/artifact")
        assert repo.name in await _send_ilink(worker, sends, "wx_user_1", "/projects")
        assert "后台调度任务" in await _send_ilink(worker, sends, "wx_user_1", "/tasks")
        assert str(extra_dir.resolve()) in await _send_ilink(worker, sends, "wx_user_1", f"/add-dir {extra_dir}")
        assert "已清空当前会话的所有 `--add-dir` 附加目录" in await _send_ilink(worker, sends, "wx_user_1", "/add-dir clear")
        assert "exec-title" in await _send_ilink(worker, sends, "wx_user_1", "/rename exec-title")
        assert "已分叉" in await _send_ilink(worker, sends, "wx_user_1", "/fork fork-branch")
        assert "c_main" in await _send_ilink(worker, sends, "wx_user_1", "/resume c_main")
        assert "EXEC:side?|conv=" in await _send_ilink(worker, sends, "wx_user_1", "/btw side?")
        assert "tokens=123" in await _send_ilink(worker, sends, "wx_user_1", "/compact")
        assert "无头会话不支持按单条文本回退文件" in await _send_ilink(worker, sends, "wx_user_1", "/rewind")
        for note_cmd in ("/feedback", "/keybindings", "/logout", "/record", "/statusline", "/title", "/voice"):
            assert len(await _send_ilink(worker, sends, "wx_user_1", note_cmd)) > 5

        for wf in (
            "plan", "goal", "boost", "grill-me", "swarm", "teamwork", "browser",
            "schedule", "learn", "agy-customizations", "antigravity-guide", "zero-hallucination-git-gate",
        ):
            assert f"EXEC:/{wf} test_arg|conv=c_main" in await _send_ilink(worker, sends, "wx_user_1", f"/{wf} test_arg")

        assert "已开启全新会话" in await _send_ilink(worker, sends, "wx_user_1", "/clear")
        assert "已退出 [AGY]" in await _send_ilink(worker, sends, "wx_user_1", "/exit")
        assert gw.det_router.engine_router.get_session_state("wx_user_1")["active_engine"] == ""

        # --- 5. FastAPI POST /wechat/webhook XML voice + JSON dispatch parity ---
        import main
        xml_voice = (
            "<xml><ToUserName><![CDATA[bot]]></ToUserName><FromUserName><![CDATA[wx_wh_user]]></FromUserName>"
            "<MsgType><![CDATA[voice]]></MsgType><MsgId>v_matrix_1</MsgId>"
            "<Recognition><![CDATA[风控看板]]></Recognition></xml>"
        )
        wh_settings = Settings(
            WORKSPACE_ROOT=repo,
            DB_PATH=tmp_path / "matrix.db",
            ALLOWED_USERS=["wx_wh_user"],
            ADMIN_USERS=["wx_wh_user"],
            WECHAT_TOKEN="dev_token_123456",
            WEBHOOK_REQUIRE_SIGNATURE=False,
        )
        monkeypatch.setattr(main, "settings", wh_settings)
        monkeypatch.setattr(main, "gateway", gw)
        monkeypatch.setattr(main, "ilink_worker", None)
        with TestClient(main.app) as client:
            assert client.post("/wechat/webhook", content=xml_voice).status_code == 200
            for idx, body_txt in enumerate(("/status", "/workspace " + str(repo), "/agy", "/usage", "/clear", "/exit")):
                res_wh = client.post(
                    "/wechat/webhook",
                    json={"from_user": "wx_wh_user", "content": body_txt, "msg_id": f"wh_{idx}"},
                )
                assert res_wh.status_code == 200 and res_wh.text == "success"
    finally:
        await worker.pusher.client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_live_wechat_full_command_matrix_against_real_agy(tmp_path: Path):
    """Run live end-to-end WeChat iLink command matrix against real installed host `agy` binary."""
    resolved = resolve_binary("agy")
    if not resolved:
        pytest.skip("agy binary not installed on host")

    repo = tmp_path / "live_repo"
    extra_dir = tmp_path / "live_extra"
    (repo / "src").mkdir(parents=True)
    extra_dir.mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (repo / "notes.md").write_text("# Notes\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "live@test.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "LiveTest"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    sends: List[Dict[str, Any]] = []
    _, worker, store = _build_wechat_stack(tmp_path, repo, resolved, "wx_live_user", sends)
    try:
        # Non-Engine checks
        assert str(repo.resolve()) in await _send_ilink(worker, sends, "wx_live_user", f"/workspace {repo}")
        for cmd in ("/pwd", "/ls", "/ls src", "/status", "/engines", "/security", "/audit", "/rules", "/help", "/exit"):
            assert len(await _send_ilink(worker, sends, "wx_live_user", cmd)) > 0
        for old_alias in ("/cd .", "/defense", "/dashboard", "/evolution", "/rsi"):
            assert "非引擎默认状态" in await _send_ilink(worker, sends, "wx_live_user", old_alias)

        # Enter real /agy and test /help + alias rejections
        r_enter = await _send_ilink(worker, sends, "wx_live_user", "/agy")
        assert "已进入 [AGY]" in r_enter and "1.5.8" in r_enter
        r_help = await _send_ilink(worker, sends, "wx_live_user", "/help")
        assert len(sends) >= 2 and "/usage" in r_help and "/plan" in r_help
        for bad in ("/new", "/agents", "/plugins", "/cs", "/settings", "/quit", "/branch", "/switch", "/undo", "/quota", "/status"):
            assert f"不存在 '{bad}'" in await _send_ilink(worker, sends, "wx_live_user", bad)

        # Config + all live subcommands
        assert "已设置为：gemini-3.8-flash-low" in await _send_ilink(worker, sends, "wx_live_user", "/model gemini-3.8-flash-low")
        assert "已设置为：auto" in await _send_ilink(worker, sends, "wx_live_user", "/mode auto")
        assert "已设置为：low" in await _send_ilink(worker, sends, "wx_live_user", "/effort low")
        for sub in ("/models", "/agent", "/mcp list", "/plugin list", "/changelog", "/install --help", "/update --help", "/remote-control status", "/acp", "/mic-serve"):
            assert len(await _send_ilink(worker, sends, "wx_live_user", sub)) > 10

        # Live 6 panels
        for panel in ("/usage", "/credits", "/config", "/hooks", "/permissions", "/skills"):
            res_p = await _send_ilink(worker, sends, "wx_live_user", panel)
            assert "抱歉" not in res_p and "returned exit code" not in res_p, f"{panel} failed: {res_p}"

        # Real Turn 1 + Turn 2 + /compact + /btw + all TUI tools
        r_t1 = await _send_ilink(worker, sends, "wx_live_user", "Create math_ok.py defining def add(a,b): return a+b and reply ONLY with CREATED_OK")
        assert "CREATED_OK" in r_t1 or (repo / "math_ok.py").is_file()
        assert "math_ok.py" in await _send_ilink(worker, sends, "wx_live_user", "Which python filename did you just create? Reply with ONLY that filename.")
        r_cmp = await _send_ilink(worker, sends, "wx_live_user", "/compact")
        assert r_cmp.strip() and "抱歉" not in r_cmp
        assert "PONG" in await _send_ilink(worker, sends, "wx_live_user", "/btw Reply with only PONG")

        for tool_cmd in (
            f"/add-dir {extra_dir}", "/diff", "/codesearch greet", "/open src/app.py",
            "/context", "/copy", "/artifact", "/projects", "/tasks",
            "/rename live-session", "/fork branch-a", "/resume",
            "/rewind", "/feedback", "/keybindings", "/logout", "/record", "/statusline", "/title", "/voice",
        ):
            assert len(await _send_ilink(worker, sends, "wx_live_user", tool_cmd)) > 5

        assert "已开启全新会话" in await _send_ilink(worker, sends, "wx_live_user", "/clear")
        assert "已退出 [AGY]" in await _send_ilink(worker, sends, "wx_live_user", "/exit")
    finally:
        await worker.pusher.client.aclose()
        store.close()
