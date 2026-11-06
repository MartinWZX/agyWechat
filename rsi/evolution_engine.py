"""RSI (Recursive Self-Improvement) Evolution Engine: LLM-First -> Code-First Entropy Reduction."""
import asyncio
import logging
import os
import re
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from memory.core_state_store import CoreStateStore
from skills.skill_manager import SkillManager
from tools.tool_registry import ToolRegistry
from verifier.hallucination_guard import ShadowDryRunValidator, ShadowDryRunError

logger = logging.getLogger("wechat_agy.rsi")


class EvolutionStage(str, Enum):
    """Three stages of RSI entropy-reduction lifecycle."""
    STAGE_A_EXPLORATION_LLM_FIRST = "STAGE_A_EXPLORATION_LLM_FIRST"
    STAGE_B_CO_EVOLUTION_INTERACTIVE = "STAGE_B_CO_EVOLUTION_INTERACTIVE"
    STAGE_C_CONVERGENCE_CODE_FIRST = "STAGE_C_CONVERGENCE_CODE_FIRST"


class LifecyclePhaseController:
    """Calculates domain maturity score and determines whether to invoke LLM or use deterministic code."""

    def __init__(self, store: CoreStateStore):
        self.store = store

    def compute_maturity_score(self, domain_key: str = "default") -> float:
        """Compute maturity score between 0.0 (Cold start) and 1.0 (Fully code converged)."""
        stats = self.store.get_stats()
        total_msgs = stats.get("total_messages", 0)
        offloaded_msgs = stats.get("offloaded_messages", 0)

        if total_msgs == 0:
            return 0.1

        offload_ratio = offloaded_msgs / max(1, total_msgs)
        # Weight by tool count and crystallized rules
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM crystallized_rules;")
            rule_count = c.fetchone()[0]

        tool_count = len(ToolRegistry.list_tools())
        bonus = min(0.3, (rule_count * 0.05) + (tool_count * 0.02))
        maturity = min(1.0, (offload_ratio * 0.7) + bonus)
        return round(maturity, 2)

    def determine_stage(self, maturity_score: float) -> EvolutionStage:
        if maturity_score < 0.4:
            return EvolutionStage.STAGE_A_EXPLORATION_LLM_FIRST
        elif maturity_score < 0.8:
            return EvolutionStage.STAGE_B_CO_EVOLUTION_INTERACTIVE
        else:
            return EvolutionStage.STAGE_C_CONVERGENCE_CODE_FIRST


class EvolutionEngine:
    """The master RSI engine orchestrating tool mutation, skill precipitation, and self-healing."""

    def __init__(
        self,
        store: Optional[CoreStateStore] = None,
        skill_manager: Optional[SkillManager] = None,
        workspace_dir: str = ".",
        config: Optional[Dict[str, Any]] = None
    ):
        self.workspace_dir = workspace_dir
        self.config = config or {}
        self.store = store or CoreStateStore()
        self.skill_manager = skill_manager or SkillManager(workspace_dir=workspace_dir)
        self.phase_controller = LifecyclePhaseController(self.store)
        self.running = False

    def process_model_rsi_patches(self, raw_model_output: str) -> Dict[str, Any]:
        """Extract and apply <tool_mutation_patch> and <skill_precipitation_patch>."""
        results: Dict[str, Any] = {"tools_mutated": [], "skills_precipitated": []}

        # 1. Tool Mutation Patches: <tool_mutation_patch action="create|update|merge|deprecate" tool_name="...">
        tool_patches = re.findall(
            r'<tool_mutation_patch\s+action="([^"]+)"\s+tool_name="([^"]+)">\s*(.*?)\s*</tool_mutation_patch>',
            raw_model_output,
            re.DOTALL
        )
        for action, tool_name, tool_code in tool_patches:
            success, msg = ToolRegistry.apply_mutation_patch(action.strip(), tool_name.strip(), tool_code.strip())
            results["tools_mutated"].append({"tool_name": tool_name, "action": action, "success": success, "message": msg})
            if success:
                with self.store._get_connection() as conn:
                    conn.execute("""
                    INSERT INTO tool_evolution_history (tool_name, action, version, diff_summary, created_at)
                    VALUES (?, ?, 1, ?, ?);
                    """, (tool_name, action, msg, time.time()))
                    conn.commit()

        # 2. Skill Precipitation Patches: <skill_precipitation_patch skill_name="..." version="...">
        skill_patches = re.findall(
            r'<skill_precipitation_patch\s+skill_name="([^"]+)"\s+version="([^"]+)">(.*?)</skill_precipitation_patch>',
            raw_model_output,
            re.DOTALL
        )
        for skill_name, version, patch_body in skill_patches:
            # Extract optional inner script block if present
            script_match = re.search(r"```python\s*(.*?)\s*```", patch_body, re.DOTALL)
            script_code = script_match.group(1).strip() if script_match else None
            success, msg = self.skill_manager.apply_skill_patch(
                skill_name.strip(), version.strip(), patch_body.strip(), script_code
            )
            results["skills_precipitated"].append({"skill_name": skill_name, "version": version, "success": success, "message": msg})
            if success:
                with self.store._get_connection() as conn:
                    conn.execute("""
                    INSERT INTO skill_evolution_history (skill_name, version, patch_summary, created_at)
                    VALUES (?, ?, ?, ?);
                    """, (skill_name, version, msg, time.time()))
                    conn.commit()

        return results

    async def run_self_healing_cycle(self, failed_script_path: str, error_traceback: str, hub: Any) -> bool:
        """Self-healing loop: wake up LLM CLI once with traceback to patch the broken script."""
        if not os.path.exists(failed_script_path):
            return False

        with open(failed_script_path, "r", encoding="utf-8") as f:
            broken_code = f.read()

        heal_prompt = f"""[SELF_HEALING_TASK]
The following deterministic script encountered an unexpected runtime exception:
File: {failed_script_path}
Error:
{error_traceback}

Original Code:
```python
{broken_code}
```

Fix the bug, preserve 100% deterministic behaviour, ensure zero stubs or mock placeholders, and return the fixed code wrapped in:
<crystallize_script name="{os.path.basename(failed_script_path).replace('.py', '')}">
# fixed python code
</crystallize_script>
"""
        manifest = {"anchors": {"ANCHOR_1_error": error_traceback}, "manifest_hash": "self_healing"}
        success, answer, _ = await hub.execute_task(heal_prompt, manifest, priority=1, preferred_engine="claude")
        if not success:
            return False

        match = re.search(r'<crystallize_script\s+name="[^"]*">\s*(.*?)\s*</crystallize_script>', answer, re.DOTALL)
        if not match:
            return False

        fixed_code = match.group(1).strip()
        try:
            ShadowDryRunValidator.validate_script(fixed_code)
            with open(failed_script_path, "w", encoding="utf-8") as f:
                f.write(fixed_code + "\n")
            logger.info("Successfully self-healed script %s", failed_script_path)
            return True
        except ShadowDryRunError as e:
            logger.error("Self-healing shadow dry-run failed: %s", e)
            return False

    async def start_daemon(self):
        """Background maintenance loop: synchronizes global rules and checks health."""
        self.running = True
        self.skill_manager.sync_global_agent_rules()
        while self.running:
            score = self.phase_controller.compute_maturity_score()
            stage = self.phase_controller.determine_stage(score)
            logger.debug("RSI Lifecycle: Maturity Score = %s, Stage = %s", score, stage.value)
            await asyncio.sleep(300)

    def stop_daemon(self):
        self.running = False
