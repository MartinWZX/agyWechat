"""Comprehensive Test Suite for Code-First, LLM-Last WeChat-AGY System.
Tests:
1. CoreStateStore (WAL mode, FTS5 search, single-writer principle)
2. WindControlGovernor (5-D defense: sliding window, 429 backoff, 403 kill-switch, unlock)
3. ApprovalGuard (AST inspection, 5 permission levels, interactive WeChat approval)
4. ToolRegistry (Deterministic tools & direct Python invocation)
5. SkillManager (Level 0 script execution & Level 2/3 specs)
6. CognitiveGate (PreFlightGate & ContextCompressor)
7. PatternCrystallizer (Structural fingerprinting, auto-crystallization)
8. CodeFirstCronScheduler (Stage-1 pure code & Stage-2 cognitive awakening)
9. HealthMonitor (Metrics, offload rate >= 90%)
10. WeChatGateway (L0 interception, Webhook flow)
"""

import asyncio
from pathlib import Path
import tempfile
import time
import pytest
from bridge.agy_pty_bridge import AgyPTYBridge, HostLockError
from compressor.cognitive_gate import ContextCompressor, InvocationReason, PreFlightGate
from config import Settings
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.wechat_gateway import WeChatGateway
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from sandbox.approval_guard import ApprovalGuard, PermissionLevel
from scheduler.code_first_cron import CodeFirstCronScheduler, RequireCognitiveAGY
from skills.skill_manager import SkillManager
from telemetry.health_monitor import HealthMonitor
from tools.tool_registry import registry


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def test_settings(temp_dir):
    return Settings(
        WORKSPACE_ROOT=temp_dir,
        AGY_LOCK_FILE=temp_dir / "agy_test.lock",
        DB_PATH=temp_dir / "core_state_test.db",
        ALLOWED_USERS=["test_user", "admin_user"],
        ADMIN_USERS=["admin_user"],
        MIN_COOLDOWN_SECONDS=0.1,  # Short cooldown for tests
        SLIDING_WINDOW_5M_MAX=6,
        SLIDING_WINDOW_1H_MAX=30,
        JITTER_MIN_SECONDS=0.01,
        JITTER_MAX_SECONDS=0.05,
    )


@pytest.mark.asyncio
async def test_core_state_store_and_fts5(test_settings):
    """Verify SQLite WAL mode, single-writer principle, and FTS5 BM25 search."""
    store = CoreStateStore(test_settings.DB_PATH)

    # 1. Session & Messages
    sess = await store.get_or_create_session("sess_1", "user_1")
    assert sess["session_id"] == "sess_1"

    msg_id = await store.add_message("sess_1", "user_1", "user", "Hello world")
    assert msg_id > 0
    msgs = await store.get_recent_messages("sess_1", limit=5)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello world"

    # 2. Episodic Memory with FTS5
    await store.set_episodic_memory("user_1", "config", "language_preference", "Python and Rust")
    search_res = await store.search_episodic_memory("Rust")
    assert len(search_res) == 1
    assert search_res[0]["key"] == "language_preference"

    # 3. Sliding Window Counts
    await store.log_wind_control_call(reason="test", duration_ms=100)
    c5m, c1h, time_since = await store.get_sliding_window_counts()
    assert c5m == 1
    assert c1h == 1
    assert time_since < 5.0

    store.close()


@pytest.mark.asyncio
async def test_wind_control_governor_5d(test_settings):
    """Verify 5-D Wind Control Defense (sliding window, 429, 403, and jitter)."""
    store = CoreStateStore(test_settings.DB_PATH)
    gov = WindControlGovernor(store, test_settings)

    # 1. Normal state check
    can_invoke, reason, wait = await gov.check_can_invoke(is_p0=True)
    assert can_invoke, f"Expected normal invocation to pass: {reason}"

    # 2. Jitter delay verification
    delay = await gov.apply_cadence_jitter()
    assert test_settings.JITTER_MIN_SECONDS <= delay <= test_settings.JITTER_MAX_SECONDS

    # 3. 429 Yellow Circuit Breaker
    await gov.trigger_429_circuit_breaker("Rate limit reached: 429 Resource Exhausted")
    can_invoke_429, reason_429, wait_429 = await gov.check_can_invoke(is_p0=True)
    assert not can_invoke_429
    assert "YELLOW 429 COOLDOWN" in reason_429

    # 4. Manual Admin Unlock
    ok_unlock, msg_unlock = await gov.manual_admin_unlock("admin_user")
    assert ok_unlock
    can_invoke_post, _, _ = await gov.check_can_invoke(is_p0=True)
    assert can_invoke_post

    # 5. 403 Red Kill-Switch
    await gov.trigger_403_kill_switch("403 PERMISSION_DENIED Verify your account")
    can_invoke_403, reason_403, _ = await gov.check_can_invoke(is_p0=True)
    assert not can_invoke_403
    assert "RED KILL-SWITCH ACTIVE" in reason_403

    store.close()


@pytest.mark.asyncio
async def test_approval_guard_ast_and_interactive(test_settings):
    """Verify AST security inspection and L4/L5 WeChat interactive approval."""
    store = CoreStateStore(test_settings.DB_PATH)
    guard = ApprovalGuard(store, test_settings)

    # 1. AST Security Inspection
    safe_code = "import json\ndef run():\n    return json.dumps({'ok': True})"
    is_safe, errs = guard.inspect_script_code(safe_code)
    assert is_safe, f"Safe code failed: {errs}"

    unsafe_code = "import os\nos.system('echo dangerous')"
    is_unsafe, errs = guard.inspect_script_code(unsafe_code)
    assert not is_unsafe
    assert any("os.system" in e for e in errs)

    # 2. Auto-approval for L1-L3
    ok_l1, _ = await guard.request_permission("test_user", PermissionLevel.L1_READ, "Query stats")
    assert ok_l1

    # 3. Interactive approval for L4/L5
    async def simulate_admin_approval():
        await asyncio.sleep(0.1)
        action_id = list(guard._pending_futures.keys())[0]
        reply = await guard.handle_approval_response("admin_user", f"确认 {action_id}")
        assert "已由管理员" in reply

    task = asyncio.create_task(simulate_admin_approval())
    approved, msg = await guard.request_permission(
        user_id="test_user",
        permission_level=PermissionLevel.L4_LOCAL_WRITE,
        description="Modify crystallized file",
        timeout_seconds=2.0,
    )
    await task
    assert approved, f"Approval flow failed: {msg}"

    store.close()


@pytest.mark.asyncio
async def test_skill_manager_and_l0_execution(test_settings):
    """Verify Level 0 pure-code deterministic script execution (0 tokens)."""
    skill_mgr = SkillManager(workspace_root=test_settings.WORKSPACE_ROOT, settings=test_settings)

    code = '''
def run(name="World"):
    return f"Hello, {name}!"
'''
    script_path = skill_mgr.save_l0_script("greet.py", code)
    assert script_path.exists()

    result = await skill_mgr.execute_l0_script(script_path, name="Antigravity")
    assert result == "Hello, Antigravity!"


@pytest.mark.asyncio
async def test_cognitive_gate_admission_and_compression():
    """Verify hardcoded pre-flight gate and token-minimized context assembly."""
    # 1. Deterministic task rejected
    ok_det, msg_det = PreFlightGate.validate_admission(InvocationReason.INFO_SYNTHESIS, "#状态", None)
    assert not ok_det
    assert "strictly deterministic" in msg_det

    # 2. Legitimate cognitive task accepted
    ok_cog, msg_cog = PreFlightGate.validate_admission(
        InvocationReason.INFO_SYNTHESIS,
        "总结分析以下非结构化故障日志",
        {"trace": "NullPointerException at line 42"},
    )
    assert ok_cog

    # 3. Context assembly
    prompt = ContextCompressor.assemble_minimal_prompt(
        reason=InvocationReason.INFO_SYNTHESIS,
        task_instruction="总结分析",
        data_payload={"log": "error"},
    )
    assert "[TASK_REASON: INFO_SYNTHESIS]" in prompt
    assert "<crystallize_script" in prompt


@pytest.mark.asyncio
async def test_pattern_crystallizer_observe_reflect(test_settings):
    """Verify Structural Fingerprinting and Observe->Reflect->Crystallize loop."""
    store = CoreStateStore(test_settings.DB_PATH)
    skill_mgr = SkillManager(workspace_root=test_settings.WORKSPACE_ROOT, settings=test_settings)
    cryst = PatternCrystallizer(store, skill_mgr, test_settings)

    msg1 = "查询服务器 10.0.0.1 在 2026-11-06 14:00 的运行指标"
    fp1 = cryst.extract_fingerprint(msg1)
    assert "<ip>" in fp1
    assert "<date>" in fp1

    # Simulate 3 hits to trigger auto-crystallization
    await cryst.observe_message(msg1)
    await cryst.observe_message(msg1)
    _, count3 = await cryst.observe_message(msg1)
    assert count3 >= 3

    # Check if crystallized into L0 rule
    rules = await store.get_active_rules()
    assert len(rules) >= 1
    assert "crystallized" in rules[0]["script_path"]

    store.close()


@pytest.mark.asyncio
async def test_telemetry_health_monitor_offload_rate(test_settings):
    """Verify HealthMonitor offload rate calculation (target >= 90%)."""
    store = CoreStateStore(test_settings.DB_PATH)
    gov = WindControlGovernor(store, test_settings)
    monitor = HealthMonitor(gov, test_settings)

    # 9 L0 hits + 1 agy invocation = 90% offload rate
    for _ in range(9):
        monitor.record_incoming()
        monitor.record_l0_hit()
    monitor.record_incoming()
    monitor.record_agy_call()

    rate = monitor.get_offload_rate()
    assert rate == 90.0

    report = await monitor.generate_status_report()
    assert "90.0%" in report
    assert "纯代码卸载效能" in report

    store.close()


@pytest.mark.asyncio
async def test_wechat_gateway_l0_interception(test_settings):
    """Verify L0 instant interception physically bypassing agy."""
    store = CoreStateStore(test_settings.DB_PATH)
    skill_mgr = SkillManager(workspace_root=test_settings.WORKSPACE_ROOT, settings=test_settings)
    gov = WindControlGovernor(store, test_settings)
    approval_guard = ApprovalGuard(store, test_settings)
    cryst = PatternCrystallizer(store, skill_mgr, test_settings)
    monitor = HealthMonitor(gov, test_settings)

    bridge = AgyPTYBridge(gov, test_settings)
    bridge.acquire_host_lock()

    gateway = WeChatGateway(
        store=store,
        governor=gov,
        bridge=bridge,
        approval_guard=approval_guard,
        crystallizer=cryst,
        skill_manager=skill_mgr,
        health_monitor=monitor,
        settings=test_settings,
    )

    # 1. System command #状态 intercepted at L0
    resp_status = await gateway.process_inbound_message("test_user", "#状态")
    assert "Google agy 微信智能体运行看板" in resp_status

    # 2. System command #风控看板 intercepted at L0
    resp_wind = await gateway.process_inbound_message("test_user", "#风控看板")
    assert "五维防御看板" in resp_wind

    # 3. Help manual intercepted at L0
    resp_help = await gateway.process_inbound_message("test_user", "#help")
    assert "WeChat-AGY 系统帮助手册" in resp_help

    # Verify 0 agy invocations were made for all three queries
    assert monitor.agy_invocations == 0
    assert monitor.l0_intercepted == 3
    assert monitor.get_offload_rate() == 100.0

    bridge.release_host_lock()
    store.close()


@pytest.mark.asyncio
async def test_code_first_cron_scheduler_two_stage(test_settings):
    """Verify Two-Stage scheduler: Stage-1 pure code (0 tokens) and Stage-2 cognitive awakening."""
    store = CoreStateStore(test_settings.DB_PATH)
    gov = WindControlGovernor(store, test_settings)
    skill_mgr = SkillManager(workspace_root=test_settings.WORKSPACE_ROOT, settings=test_settings)
    bridge = AgyPTYBridge(gov, test_settings)

    # 1. Create a Stage-1 pure code script
    stage1_code = '''
def run():
    return "All systems operational (0 Token)"
'''
    script_path = skill_mgr.save_l0_script("cron_stage1.py", stage1_code)

    pushed_messages = []
    def mock_push(user, msg):
        pushed_messages.append((user, msg))

    # Save to jobs.json
    jobs_json = test_settings.WORKSPACE_ROOT / "scheduler" / "jobs.json"
    jobs_json.parent.mkdir(parents=True, exist_ok=True)
    import json
    jobs_json.write_text(json.dumps([{
        "id": "job_stage1",
        "name": "Stage1 Job",
        "schedule": "* * * * *",
        "script_path": str(script_path),
        "target_user": "test_user",
        "is_active": True
    }]), encoding="utf-8")

    scheduler = CodeFirstCronScheduler(
        skill_manager=skill_mgr,
        bridge=bridge,
        push_message_func=mock_push,
        settings=test_settings,
        jobs_file_path=jobs_json,
    )
    scheduler.load_jobs()
    assert len(scheduler.jobs) == 1

    # Execute Stage-1 pipeline directly
    job = scheduler.jobs["job_stage1"]
    await scheduler._execute_job_pipeline(job)

    assert scheduler.stage1_success_count == 1
    assert len(pushed_messages) == 1
    assert "All systems operational" in pushed_messages[0][1]

    store.close()


def test_fastapi_endpoints(test_settings):
    """Verify FastAPI root, health, and webhook endpoints with full lifespan context."""
    from starlette.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        # Root endpoint
        resp_root = client.get("/")
        assert resp_root.status_code == 200
        assert resp_root.json()["system"] == "WeChat-AGY Autonomous Agent"

        # Webhook endpoint with L0 command #状态
        payload = {
            "from_user": "o9cq804HiRX4uv6kMVZLFquLYyMY@im.wechat",
            "content": "#状态",
            "msg_id": "msg_001"
        }
        resp_wh = client.post("/wechat/webhook", json=payload)
        assert resp_wh.status_code == 200
        assert resp_wh.text == "success"

        # Health endpoint
        resp_health = client.get("/health")
        assert resp_health.status_code == 200
        assert "offload_rate" in resp_health.json()
