"""Comprehensive End-to-End Test Suite for All 12 Modules."""
import unittest
import yaml
from gateway.voice_normalizer import VoiceTextNormalizer
from gateway.wechat_gateway import WeChatGateway
from verifier.hallucination_guard import (
    WorkspaceSnapshotDiffer,
    LogicalConsistencyVerifier,
    ShadowDryRunValidator,
    UnderExecutionHallucinationError,
    OverExecutionHallucinationError,
    LogicalContradictionError
)
from compressor.cognitive_gate import PreInferenceDataValidator
from guard.wind_control_governor import WindControlGovernor
from memory.core_state_store import CoreStateStore
from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError
from crystallizer.pattern_crystallizer import PatternCrystallizer

class TestAutonomousMultiCLISystem(unittest.TestCase):
    def setUp(self):
        with open("config.yaml", "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.store = CoreStateStore()
        # Reset health state for clean test isolation
        with self.store._get_connection() as conn:
            conn.execute("UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0, last_call_timestamp = 0;")
            conn.commit()

    def test_01_voice_normalizer(self):
        vn = VoiceTextNormalizer(self.config)
        # Test filler removal & homophone correction
        raw = "呃 那个 请问 帮我 丰控看板"
        norm = vn.normalize(raw)
        self.assertEqual(norm, "#风控看板")

        raw_pi = "爱特派 执行系统健康检查"
        norm_pi = vn.normalize(raw_pi)
        self.assertTrue(norm_pi.startswith("@pi"))

    def test_02_voice_pre_inference_sanity(self):
        valid, msg = PreInferenceDataValidator.validate_wechat_voice("啊 呃 嗯")
        self.assertFalse(valid)
        valid2, msg2 = PreInferenceDataValidator.validate_wechat_voice("查看系统状态")
        self.assertTrue(valid2)

    def test_03_l0_zero_token_interceptor(self):
        gw = WeChatGateway(self.config)
        # Status command L0 test
        res = gw.process_incoming_message("msg_test_01", "user_01", "text", "#状态")
        self.assertIn("【系统运行状态】", res)

        # Risk dashboard L0 test via voice
        xml = "<xml><Recognition><![CDATA[呃 丰控看板]]></Recognition><MediaId><![CDATA[m123]]></MediaId><Format><![CDATA[amr]]></Format></xml>"
        res_voice = gw.process_incoming_message("msg_test_02", "user_01", "voice", xml)
        self.assertIn("#风控看板", res_voice)
        self.assertIn("【🛡️ 多引擎风控与运行看板】", res_voice)

    def test_04_anti_hallucination_under_execution(self):
        differ = WorkspaceSnapshotDiffer(".")
        pre = differ.take_snapshot()
        post = pre.copy()
        manifest = {"files_created": ["non_existent_file.py"], "files_modified": []}
        with self.assertRaises(UnderExecutionHallucinationError):
            differ.audit_manifest(pre, post, manifest)

    def test_05_anti_hallucination_over_execution(self):
        differ = WorkspaceSnapshotDiffer(".")
        pre = differ.take_snapshot()
        post = pre.copy()
        post["unauthorized_secret.txt"] = "hash123"
        manifest = {"files_created": [], "files_modified": []}
        with self.assertRaises(OverExecutionHallucinationError):
            differ.audit_manifest(pre, post, manifest)

    def test_06_anti_hallucination_logical_consistency(self):
        raw_manifest = {
            "anchors": {
                "ANCHOR_1_cpu": {"value": 12, "status": "NORMAL"}
            }
        }
        contradictory_output = """
<facts_anchor>
ANCHOR_1_cpu: 12% (NORMAL)
</facts_anchor>
<causal_chain>
ANCHOR_1_cpu is 12%, thus system has CRITICAL_FAILURE.
</causal_chain>
<execution_manifest>
{"files_created": [], "files_modified": [], "commands_executed": []}
</execution_manifest>
<final_conclusion>
CRITICAL_FAILURE occurred due to overload.
</final_conclusion>
"""
        with self.assertRaises(LogicalContradictionError):
            LogicalConsistencyVerifier.verify(raw_manifest, contradictory_output)

    def test_07_shadow_dry_run_anti_stub(self):
        stub_code = "def bad_func():\n    pass\n"
        with self.assertRaises(Exception):
            ShadowDryRunValidator.validate_script(stub_code)

        valid_code = "def good_func():\n    return 42\ngood_func()\n"
        ok, msg = ShadowDryRunValidator.validate_script(valid_code)
        self.assertTrue(ok)

    def test_08_wind_control_rate_limit(self):
        gov = WindControlGovernor(config=self.config)
        ok, msg = gov.check_engine_availability("agy", is_p0_urgent=True)
        self.assertTrue(ok)

    def test_09_sandbox_ast_and_blacklist(self):
        with self.assertRaises(SandboxSecurityError):
            ApprovalGuard.scan_shell_command("rm -rf /")

        with self.assertRaises(SandboxSecurityError):
            ApprovalGuard.scan_python_ast("eval('x + 1')")

    def test_10_crystallizer_pattern(self):
        cryst = PatternCrystallizer()
        fp = cryst.extract_fingerprint("请问今天天气怎么样")
        self.assertTrue(len(fp) > 0)

if __name__ == "__main__":
    unittest.main()
