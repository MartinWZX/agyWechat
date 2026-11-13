"""End-to-End Verification Suite for Architecture v3 Upgrades (P0 Hardening + P1 Governed RSI + P2 SOTA).

Tests exercise real subsystems (real child processes, real SQLite WAL + FTS5, real FastAPI TestClient,
real out-of-process bubblewrap/rlimit sandbox) without self-mocking functions under test.
"""
import asyncio
import datetime
from pathlib import Path
import sys
import pytest
from starlette.testclient import TestClient

from bridge.error_classifier import FailureKind, classify_cli_failure
from bridge.multi_cli_hub import MultiCLIHub
from config import Settings
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.wechat_gateway import WeChatGateway
from guard.wind_control_governor import WindControlGovernor
from main import app, compute_wechat_signature
from memory.core_state_store import CoreStateStore
from memory.rsi_repository import RSIRepository
from rsi.eval_harness import EvalHarness
from rsi.evolution_engine import EvolutionEngine
from sandbox.approval_guard import ApprovalGuard, scrub_environment
from sandbox.runner import run_code_sync
from scheduler.code_first_cron import CodeFirstCronScheduler
from scheduler.cron_expr import cron_is_due
from skills.skill_manager import SkillManager
from telemetry.health_monitor import HealthMonitor
from tools.mcp_server import MCPToolServer
from tools.tool_registry import ToolRegistry
from verifier.hallucination_guard import LogicalConsistencyVerifier, LogicalContradictionError


def _make_real_cli_script(script_file: Path, body_lines: str) -> str:
    """Create a real executable Python CLI helper script on disk for subprocess driver tests."""
    script_file.write_text(f"#!{sys.executable}\n" + body_lines, encoding="utf-8")
    script_file.chmod(0o755)
    return str(script_file)


@pytest.mark.asyncio
async def test_s1_no_deadlock_on_sync_voice_xml_inside_running_loop(tmp_path: Path):
    """S1: Calling sync `process_incoming_message` from inside an active event loop must never deadlock."""
    store = CoreStateStore(tmp_path / "s1.db")
    gw = WeChatGateway(store=store)
    xml_payload = (
        "<xml><FromUserName><![CDATA[u1]]></FromUserName>"
        "<MsgId>m_xml_1</MsgId><MsgType><![CDATA[voice]]></MsgType>"
        "<Recognition><![CDATA[呃 查看状态]]></Recognition></xml>"
    )
    reply = await asyncio.wait_for(
        asyncio.sleep(0, result=gw.process_incoming_message("m_xml_1", "u1", "voice", xml_payload)),
        timeout=5.0,
    )
    assert "【系统运行状态】" in reply


def test_s2_anchored_429_403_classifier_blocks_substring_false_positives():
    """S2: Numbers like '4290 lines' or 'port 4030' in stdout must NOT trip 429/403 lockdown."""
    assert classify_cli_failure(0, "Processed 4290 lines on port 4030 successfully.", "") == FailureKind.NONE
    assert classify_cli_failure(1, "", "HTTP/1.1 429 Too Many Requests") == FailureKind.RATE_LIMIT_429
    assert classify_cli_failure(1, "", '{"status": 403, "error": "PERMISSION_DENIED"}') == FailureKind.LOCKDOWN_403
    assert classify_cli_failure(2, "port 4290", "generic syntax failure") == FailureKind.EXEC_ERROR


def test_s3_out_of_process_sandbox_protects_host_filesystem(tmp_path: Path):
    """S3: Untrusted code runs out-of-process and cannot mutate or delete host files."""
    victim = tmp_path / "important_host_file.txt"
    victim.write_text("DO_NOT_TOUCH", encoding="utf-8")

    malicious_delete = f"import os\ndef run():\n    os.remove({str(victim)!r})\n    return 'deleted'\n"
    res = run_code_sync(malicious_delete, entrypoint="run", timeout=5.0)
    assert not res.ok or victim.exists()
    assert victim.read_text(encoding="utf-8") == "DO_NOT_TOUCH"


@pytest.mark.asyncio
async def test_s4_c6_driver_env_scrubbing_and_config_injection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """S4 & C6: Drivers read timeout/max_turns from config and strip host credentials before exec."""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy_openai_credential_for_scrub_test")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy_github_credential_for_scrub_test")
    monkeypatch.setenv("ALLOWED_CUSTOM_VAR", "visible_ok")

    scrubbed = scrub_environment()
    assert "OPENAI_API_KEY" not in scrubbed
    assert "GITHUB_TOKEN" not in scrubbed
    assert scrubbed.get("ALLOWED_CUSTOM_VAR") == "visible_ok"

    cli_bin = _make_real_cli_script(
        tmp_path / "fake_agy.py",
        "import os, sys\n"
        "leaked = 'OPENAI_API_KEY' in os.environ or 'GITHUB_TOKEN' in os.environ\n"
        "print('<facts_anchor>ANCHOR_1_q</facts_anchor>')\n"
        "print('<causal_chain>ANCHOR_1_q leaked=' + str(leaked) + '</causal_chain>')\n"
        "print('<execution_manifest>{\"files_created\":[],\"files_modified\":[],\"commands_executed\":[]}</execution_manifest>')\n"
        "print('<final_conclusion>ENV_LEAK=' + str(leaked) + '</final_conclusion>')\n",
    )
    cfg = {
        "system": {"workspace_dir": str(tmp_path)},
        "engines": {
            "max_concurrency": 2,
            "failover_order": ["agy"],
            "agy": {"binary": cli_bin, "dangerously_skip_permissions_flag": "", "timeout_seconds": 15, "max_turns": 2},
        },
        "wind_control": {"min_call_interval_seconds": 0.0, "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0}},
    }
    gov = WindControlGovernor(db_path=str(tmp_path / "gov.db"), config=cfg)
    hub = MultiCLIHub(config=cfg, governor=gov)
    assert hub.drivers["agy"].timeout == 15
    assert hub.drivers["agy"].max_turns == 2

    ok, out, eng = await hub.execute_task("probe", {"anchors": {"ANCHOR_1_q": {"v": 1}}}, priority=0)
    assert ok and eng == "agy"
    assert "ENV_LEAK=False" in out


def test_s5_webhook_signature_auth_idempotency_and_safe_admin_default(tmp_path: Path):
    """S5: Default ADMIN_USERS has no wildcard; invalid webhook signature returns 403; valid returns 200."""
    from main import settings as app_settings

    default_s = Settings(_env_file=None)
    assert "*" not in default_s.ADMIN_USERS

    guard = ApprovalGuard(settings=default_s)
    denied = asyncio.run(guard.handle_approval_response("random_stranger", "/approve act_123"))
    assert "权限不足" in denied

    allowed_sender = (
        "u1" if "*" in app_settings.ADMIN_USERS else app_settings.ALLOWED_USERS[0]
    )

    with TestClient(app) as client:
        bad_resp = client.post(
            "/wechat/webhook?signature=deadbeef&timestamp=1700000000&nonce=abc",
            json={"from_user": allowed_sender, "content": "/status", "msg_id": "sig_bad_1"},
        )
        assert bad_resp.status_code == 403

        token = app_settings.WECHAT_TOKEN.get_secret_value()
        sig = compute_wechat_signature(token, "1700000001", "nonce42")
        good_resp = client.post(
            f"/wechat/webhook?signature={sig}&timestamp=1700000001&nonce=nonce42",
            json={"from_user": allowed_sender, "content": "/status", "msg_id": "sig_ok_1"},
        )
        assert good_resp.status_code == 200

        xml_body = (
            f"<xml><FromUserName><![CDATA[{allowed_sender}]]></FromUserName>"
            "<MsgId>xml_webhook_01</MsgId><MsgType><![CDATA[voice]]></MsgType>"
            "<Recognition><![CDATA[风控看板]]></Recognition></xml>"
        )
        xml_resp = client.post(
            f"/wechat/webhook?signature={sig}&timestamp=1700000001&nonce=nonce42",
            content=xml_body.encode("utf-8"),
            headers={"content-type": "application/xml"},
        )
        assert xml_resp.status_code == 200


@pytest.mark.asyncio
async def test_s6_real_cron_expression_evaluation_and_deduplication(tmp_path: Path):
    """S6: '0 8 * * *' fires once at 08:00 and does NOT re-fire in the same minute or at 08:01 / 09:00."""
    ts_0800 = datetime.datetime(2026, 11, 10, 8, 0, 10).timestamp()
    ts_0800_later = datetime.datetime(2026, 11, 10, 8, 0, 45).timestamp()
    ts_0801 = datetime.datetime(2026, 11, 10, 8, 1, 5).timestamp()
    ts_0900 = datetime.datetime(2026, 11, 10, 9, 0, 0).timestamp()

    assert cron_is_due("0 8 * * *", ts_0800, last_run_ts=0.0) is True
    assert cron_is_due("0 8 * * *", ts_0800_later, last_run_ts=ts_0800) is False
    assert cron_is_due("0 8 * * *", ts_0801, last_run_ts=ts_0800) is False
    assert cron_is_due("0 8 * * *", ts_0900, last_run_ts=ts_0800) is False

    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(
        '[{"id": "daily_8am", "cron": "0 8 * * *", "script": "system_health_check", "is_active": true}]',
        encoding="utf-8",
    )
    sched = CodeFirstCronScheduler(jobs_file_path=jobs_file)
    assert await sched.tick(now_ts=ts_0800) == 1
    assert await sched.tick(now_ts=ts_0800_later) == 0
    assert await sched.tick(now_ts=ts_0801) == 0


@pytest.mark.asyncio
async def test_s7_bounded_concurrency_and_c3_contract_enforcement(tmp_path: Path):
    """S7 & C3: Hub caps parallel CLI processes at <= 2 and rejects outputs missing contract tags."""
    with pytest.raises(LogicalContradictionError):
        LogicalConsistencyVerifier.verify({"anchors": {}}, "plain unstructured output without tags")

    tracker_dir = tmp_path / "slots"
    tracker_dir.mkdir()
    cli_bin = _make_real_cli_script(
        tmp_path / "slow_cli.py",
        "import os, time\n"
        f"d = {str(tracker_dir)!r}\n"
        "pid_f = os.path.join(d, str(os.getpid()))\n"
        "open(pid_f, 'w').write('1')\n"
        "peak = len(os.listdir(d))\n"
        "time.sleep(0.15)\n"
        "os.remove(pid_f)\n"
        "print('<facts_anchor>ANCHOR_1</facts_anchor>')\n"
        "print('<causal_chain>ANCHOR_1 peak=' + str(peak) + '</causal_chain>')\n"
        "print('<execution_manifest>{\"files_created\":[],\"files_modified\":[],\"commands_executed\":[]}</execution_manifest>')\n"
        "print('<final_conclusion>PEAK=' + str(peak) + '</final_conclusion>')\n",
    )
    cfg = {
        "system": {"workspace_dir": str(tmp_path), "snapshot_ignore_patterns": ["slots", "*.py", "*.db*"]},
        "engines": {
            "max_concurrency": 2,
            "failover_order": ["agy", "pi"],
            "agy": {"binary": cli_bin, "dangerously_skip_permissions_flag": ""},
            "pi": {"binary": cli_bin, "dangerously_skip_permissions_flag": ""},
        },
        "wind_control": {"min_call_interval_seconds": 0.0, "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0}},
    }
    gov = WindControlGovernor(db_path=str(tmp_path / "conc.db"), config=cfg)
    hub = MultiCLIHub(config=cfg, governor=gov)

    tasks = [
        hub.execute_task(f"task_{i}", {"anchors": {"ANCHOR_1": {"i": i}}}, priority=0)
        for i in range(4)
    ]
    results = await asyncio.gather(*tasks)
    for ok, out, _ in results:
        assert ok
        assert "PEAK=1" in out or "PEAK=2" in out
        assert "PEAK=3" not in out


@pytest.mark.asyncio
async def test_p1_governed_rsi_closed_loop_negative_feedback_and_self_healing(tmp_path: Path):
    """P1 & P2: Full RSI loop -> 3 verified answers -> EvalHarness active skill -> 0-token T1 hit -> negative feedback degrade -> self-heal."""
    cli_calls = {"count": 0}
    counter_file = tmp_path / "cli_calls.txt"
    counter_file.write_text("0", encoding="utf-8")

    cli_bin = _make_real_cli_script(
        tmp_path / "smart_cli.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('smart-claude 2.0.0')\n"
        "    sys.exit(0)\n"
        "if '--help' in sys.argv:\n"
        "    print('Slash commands:\\n  /plan  Create plan')\n"
        "    sys.exit(0)\n"
        f"cf = {str(counter_file)!r}\n"
        "n = int(open(cf).read().strip()) + 1\n"
        "open(cf, 'w').write(str(n))\n"
        "prompt = ' '.join(sys.argv[1:])\n"
        "print('<facts_anchor>ANCHOR_1_user_query</facts_anchor>')\n"
        "print('<causal_chain>ANCHOR_1_user_query deterministic SOP lookup</causal_chain>')\n"
        "print('<execution_manifest>{\"files_created\":[],\"files_modified\":[],\"commands_executed\":[]}</execution_manifest>')\n"
        "if '[SELF_HEALING_TASK]' in prompt:\n"
        "    print('<final_conclusion>Healed broken skill</final_conclusion>')\n"
        "    print('<crystallize_script name=\"healed_skill\">\\ndef run(query=\"\", slots=None, **kwargs):\\n    return \"端口 8000 为标准服务监听端口（自愈恢复）。\"\\n</crystallize_script>')\n"
        "else:\n"
        "    print('<final_conclusion>生产环境标准监听端口为 8000，备份端口为 8001。</final_conclusion>')\n",
    )

    cfg = {
        "system": {"workspace_dir": str(tmp_path), "snapshot_ignore_patterns": ["cli_calls.txt", "skills", "*.py", "*.db*"]},
        "engines": {
            "max_concurrency": 2,
            "failover_order": ["claude"],
            "claude": {"binary": cli_bin, "dangerously_skip_permissions_flag": ""},
        },
        "wind_control": {"min_call_interval_seconds": 0.0, "human_cadence": {"min_jitter_seconds": 0.0, "max_jitter_seconds": 0.0}},
        "crystallization": {"faq_solidify_threshold": 3, "auto_cron_enabled": False},
    }
    s = Settings(WORKSPACE_ROOT=tmp_path, DB_PATH=tmp_path / "rsi_loop.db", ADMIN_USERS=["admin_user"])
    store = CoreStateStore(s.DB_PATH)
    await store.set_episodic_memory("u1", "ops", "port_spec", "Port 8000 primary")
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

    # Non-engine default state blocks business queries & direct engine+task invocation before /claude entry
    blocked = await gw.process_inbound_message("u1", "查询节点 10.0.0.1 的标准监听端口")
    assert "非引擎默认状态" in blocked
    entered = await gw.process_inbound_message("u1", "/claude")
    assert "已进入 [CLAUDE]" in entered and "/plan" in entered

    # 3 parameterized variations of the same structural question inside claude engine
    r1 = await gw.process_inbound_message("u1", "查询节点 10.0.0.1 的标准监听端口")
    r2 = await gw.process_inbound_message("u1", "查询节点 10.0.0.2 的标准监听端口")
    r3 = await gw.process_inbound_message("u1", "查询节点 10.0.0.3 的标准监听端口")
    assert r1 == "生产环境标准监听端口为 8000，备份端口为 8001。"
    assert "<facts_anchor>" not in r1  # C1: raw XML tags stripped on async path!
    cli_calls["count"] = int(counter_file.read_text(encoding="utf-8"))
    assert cli_calls["count"] == 3

    repo = RSIRepository(store)
    active_rules = repo.list_rules(statuses=["active"])
    assert len(active_rules) == 1
    rule_name = active_rules[0]["rule_name"]
    script_path = Path(active_rules[0]["script_path"])

    # 4th parameterized query hits Tier-1 Active RSI Skill with ZERO new CLI invocations!
    r4 = await gw.process_inbound_message("u1", "查询节点 10.0.0.99 的标准监听端口")
    assert "8000" in r4
    assert int(counter_file.read_text(encoding="utf-8")) == 3

    # Corrupt active script to verify automatic degradation + self-healing cycle!
    script_path.write_text("def run(query='', slots=None, **kwargs):\n    return 1 / 0\n", encoding="utf-8")
    r5 = await gw.process_inbound_message("u1", "查询节点 10.0.0.88 的标准监听端口")
    assert "8000" in r5
    # Self-healing restored rule back to active with valid code!
    healed_rule = repo.get_rule(rule_name)
    assert healed_rule is not None and healed_rule["status"] == "active"
    healed_report = EvalHarness.evaluate_script(script_path.read_text(encoding="utf-8"), sample_queries=["test"])
    assert healed_report.passed

    # Serve healed skill once, then send negative user feedback ("不对") -> degrades rule immediately!
    r6 = await gw.process_inbound_message("u1", "查询节点 10.0.0.77 的标准监听端口")
    assert "8000" in r6
    r7 = await gw.process_inbound_message("u1", "不对")
    assert "8000" in r7
    degraded_rule = repo.get_rule(rule_name)
    assert degraded_rule is not None and degraded_rule["status"] == "degraded"


def test_r5_tool_persistence_and_p2_mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """R5 & P2: Dynamically mutated tools persist in manifest.json, reload on startup, and serve over MCP."""
    versions_dir = tmp_path / "versions"
    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setattr(ToolRegistry, "_versions_dir", str(versions_dir))
    monkeypatch.setattr(ToolRegistry, "_trace_path", str(trace_file))

    ok, msg = ToolRegistry.apply_mutation_patch(
        "create",
        "unit_converter_kb",
        "def unit_converter_kb(megabytes: int) -> int:\n"
        '    """Convert MB to KB deterministically."""\n'
        "    return megabytes * 1024\n",
    )
    assert ok, msg
    assert (versions_dir / "manifest.json").is_file()

    # Simulate process restart by wiping in-memory entry and reloading from disk
    ToolRegistry._tools.pop("unit_converter_kb", None)
    assert ToolRegistry.get_tool("unit_converter_kb") is None
    reloaded = ToolRegistry.load_persisted_tools()
    assert reloaded >= 1
    assert ToolRegistry.execute("unit_converter_kb", megabytes=4) == 4096

    # Verify MCP JSON-RPC 2.0 protocol server
    init_resp = MCPToolServer.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_resp is not None and "tools" in init_resp["result"]["capabilities"]

    list_resp = MCPToolServer.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert list_resp is not None
    names = {t["name"] for t in list_resp["result"]["tools"]}
    assert "unit_converter_kb" in names

    call_resp = MCPToolServer.handle_request({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "unit_converter_kb", "arguments": {"megabytes": 2}},
    })
    assert call_resp is not None
    assert call_resp["result"]["isError"] is False
    assert call_resp["result"]["content"][0]["text"] == "2048"

    ToolRegistry.apply_mutation_patch("deprecate", "unit_converter_kb", "")
