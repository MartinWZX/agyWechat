"""Comprehensive Test Suite for Module 13: RSI Evolution Engine & Periodicity Cron Solidification."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from crystallizer.pattern_crystallizer import PatternCrystallizer
from memory.core_state_store import CoreStateStore
from rsi.evolution_engine import EvolutionEngine, EvolutionStage, LifecyclePhaseController
from skills.skill_manager import SkillManager
from tools.tool_registry import ToolRegistry


class TestRSIEvolutionEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "rsi_test.db")
        self.store = CoreStateStore(self.db_path)
        self.skill_mgr = SkillManager(workspace_dir=self.tmp_dir)
        self.cryst = PatternCrystallizer(
            store=self.store,
            skill_manager=self.skill_mgr,
            workspace_dir=self.tmp_dir
        )
        self.engine = EvolutionEngine(
            store=self.store,
            skill_manager=self.skill_mgr,
            workspace_dir=self.tmp_dir
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_lifecycle_maturity_and_stages(self):
        controller = LifecyclePhaseController(self.store)
        # Cold start maturity score
        m0 = controller.compute_maturity_score()
        self.assertEqual(m0, 0.1)
        self.assertEqual(controller.determine_stage(m0), EvolutionStage.STAGE_A_EXPLORATION_LLM_FIRST)

        # Simulate 10 messages with 9 offloaded
        for i in range(9):
            self.store.record_message(
                f"m_{i}", "s1", "u1", "text", "query", "query", False, "res", "L0_CODE", True
            )
        self.store.record_message(
            "m_9", "s1", "u1", "text", "query", "query", False, "res", "agy", False
        )

        m_high = controller.compute_maturity_score()
        self.assertGreaterEqual(m_high, 0.6)
        stage_high = controller.determine_stage(m_high)
        self.assertIn(stage_high, [EvolutionStage.STAGE_B_CO_EVOLUTION_INTERACTIVE, EvolutionStage.STAGE_C_CONVERGENCE_CODE_FIRST])

    def test_02_tool_mutation_patch_and_rollback(self):
        # 1. Apply tool mutation patch
        patch_output = """
Some reasoning here...
<tool_mutation_patch action="create" tool_name="sample_calculator">
def sample_calculator(x: int, y: int) -> int:
    return x * y + 10
</tool_mutation_patch>
"""
        res = self.engine.process_model_rsi_patches(patch_output)
        self.assertEqual(len(res["tools_mutated"]), 1)
        self.assertTrue(res["tools_mutated"][0]["success"])

        # Execute registered tool
        val = ToolRegistry.execute("sample_calculator", x=3, y=4)
        self.assertEqual(val, 22)

        # 2. Update tool with v2
        patch_v2 = """
<tool_mutation_patch action="update" tool_name="sample_calculator">
def sample_calculator(x: int, y: int) -> int:
    return x * y + 100
</tool_mutation_patch>
"""
        res_v2 = self.engine.process_model_rsi_patches(patch_v2)
        self.assertTrue(res_v2["tools_mutated"][0]["success"])
        val_v2 = ToolRegistry.execute("sample_calculator", x=3, y=4)
        self.assertEqual(val_v2, 112)

        # 3. Rollback tool to v1
        ok_rb, msg_rb = ToolRegistry.rollback("sample_calculator")
        self.assertTrue(ok_rb)
        val_rb = ToolRegistry.execute("sample_calculator", x=3, y=4)
        self.assertEqual(val_rb, 22)

    def test_03_skill_precipitation_patch(self):
        patch_output = """
<skill_precipitation_patch skill_name="disk_analyzer" version="v1.0">
---
name: disk_analyzer
description: Analyze disk space and usage trends
triggers: [disk, space, df]
---
# Disk Analyzer Skill
```python
def run():
    return "Disk healthy: 10GB free."
```
</skill_precipitation_patch>
"""
        res = self.engine.process_model_rsi_patches(patch_output)
        self.assertEqual(len(res["skills_precipitated"]), 1)
        self.assertTrue(res["skills_precipitated"][0]["success"])

        # Check skill list
        skills = self.skill_mgr.list_l2_skills()
        self.assertTrue(any(s["name"] == "disk_analyzer" for s in skills))

    def test_04_periodicity_detection_and_cron_solidification(self):
        # Record 4 recurring messages with periodic timestamps (every 600s)
        base_time = 1700000000.0
        msg = "每日早上查看服务器负载"
        fp = self.cryst.extract_fingerprint(msg)

        ts_list = [base_time, base_time + 600, base_time + 1200, base_time + 1800]
        with self.store._get_connection() as conn:
            import json
            conn.execute(
                "INSERT INTO interaction_patterns (fingerprint, raw_sample, occurrence_count, last_seen, crystallized, timestamps_json) VALUES (?, ?, 4, ?, 0, ?);",
                (fp, msg, ts_list[-1], json.dumps(ts_list))
            )
            conn.commit()

        # Detect periodicity
        p_info = self.cryst.detect_periodicity(fp)
        self.assertIsNotNone(p_info)
        self.assertLessEqual(p_info["cv"], 0.25)
        self.assertIn("*", p_info["suggested_cron"])

        # Solidify into cron
        notice = self.cryst.solidify_periodic_cron(p_info)
        self.assertIn("[⚙️ RSI 自动演进]", notice)
        self.assertIn("已自动为您固化为纯代码定时任务", notice)

        # Check jobs.json exists
        self.assertTrue(os.path.exists(self.cryst.jobs_path))


if __name__ == "__main__":
    unittest.main()
