"""RSI (Recursive Self-Improvement) Evolution Engine: Governed Entropy-Reduction Lifecycle."""
import asyncio
from dataclasses import dataclass
from enum import Enum
import logging
import os
import re
from typing import Any, Dict, Optional

from memory.core_state_store import CoreStateStore
from memory.rsi_repository import RSIRepository
from rsi.eval_harness import EvalHarness
from skills.skill_manager import SkillManager
from tools.tool_registry import ToolRegistry

logger = logging.getLogger("wechat_agy.rsi")


class EvolutionStage(str, Enum):
    """Three stages of RSI entropy-reduction lifecycle."""
    STAGE_A_EXPLORATION_LLM_FIRST = "STAGE_A_EXPLORATION_LLM_FIRST"
    STAGE_B_CO_EVOLUTION_INTERACTIVE = "STAGE_B_CO_EVOLUTION_INTERACTIVE"
    STAGE_C_CONVERGENCE_CODE_FIRST = "STAGE_C_CONVERGENCE_CODE_FIRST"


@dataclass(frozen=True)
class StageRoutingPolicy:
    stage: EvolutionStage
    maturity_score: float
    skill_similarity_threshold: float
    semantic_cache_threshold: float
    daily_llm_budget: int
    auto_promote_verified: bool


class LifecyclePhaseController:
    """Calculates domain maturity score and emits stage-specific routing & budget policies."""

    def __init__(self, store: CoreStateStore, repo: Optional[RSIRepository] = None):
        self.store = store
        self.repo = repo or RSIRepository(store)

    def compute_maturity_score(self, domain_key: str = "default") -> float:
        """Compute maturity score between 0.0 (cold start) and 1.0 (converged)."""
        stats = self.store.get_stats()
        total_msgs = stats.get("total_messages", 0)
        offloaded_msgs = stats.get("offloaded_messages", 0)
        if total_msgs == 0:
            return 0.1

        offload_ratio = offloaded_msgs / max(1, total_msgs)
        rule_count = self.repo.count_rules()
        tool_count = len(ToolRegistry.list_tools())
        bonus = min(0.3, (rule_count * 0.05) + (tool_count * 0.02))
        maturity = min(1.0, (offload_ratio * 0.7) + bonus)
        return round(maturity, 2)

    def determine_stage(self, maturity_score: float) -> EvolutionStage:
        if maturity_score < 0.4:
            return EvolutionStage.STAGE_A_EXPLORATION_LLM_FIRST
        if maturity_score < 0.8:
            return EvolutionStage.STAGE_B_CO_EVOLUTION_INTERACTIVE
        return EvolutionStage.STAGE_C_CONVERGENCE_CODE_FIRST

    def get_stage_policy(self, domain_key: str = "default") -> StageRoutingPolicy:
        """Translate current maturity score into concrete router thresholds & LLM budget."""
        score = self.compute_maturity_score(domain_key)
        stage = self.determine_stage(score)
        if stage == EvolutionStage.STAGE_A_EXPLORATION_LLM_FIRST:
            return StageRoutingPolicy(
                stage=stage,
                maturity_score=score,
                skill_similarity_threshold=0.90,
                semantic_cache_threshold=0.92,
                daily_llm_budget=200,
                auto_promote_verified=True,
            )
        if stage == EvolutionStage.STAGE_B_CO_EVOLUTION_INTERACTIVE:
            return StageRoutingPolicy(
                stage=stage,
                maturity_score=score,
                skill_similarity_threshold=0.82,
                semantic_cache_threshold=0.86,
                daily_llm_budget=120,
                auto_promote_verified=True,
            )
        return StageRoutingPolicy(
            stage=stage,
            maturity_score=score,
            skill_similarity_threshold=0.75,
            semantic_cache_threshold=0.82,
            daily_llm_budget=50,
            auto_promote_verified=True,
        )


class EvolutionEngine:
    """Master RSI engine orchestrating tool mutation, skill precipitation, and self-healing."""

    def __init__(
        self,
        store: Optional[CoreStateStore] = None,
        skill_manager: Optional[SkillManager] = None,
        workspace_dir: str = ".",
        config: Optional[Dict[str, Any]] = None,
    ):
        self.workspace_dir = workspace_dir
        self.config = config or {}
        self.store = store or CoreStateStore()
        self.repo = RSIRepository(self.store)
        self.skill_manager = skill_manager or SkillManager(workspace_dir=workspace_dir)
        self.phase_controller = LifecyclePhaseController(self.store, self.repo)
        self.running = False
        self.current_policy: StageRoutingPolicy = self.phase_controller.get_stage_policy()

    def process_model_rsi_patches(self, raw_model_output: str) -> Dict[str, Any]:
        """Extract, sandbox-verify, and apply `<tool_mutation_patch>` and `<skill_precipitation_patch>`."""
        results: Dict[str, Any] = {"tools_mutated": [], "skills_precipitated": []}

        tool_patches = re.findall(
            r'<tool_mutation_patch\s+action="([^"]+)"\s+tool_name="([^"]+)">\s*(.*?)\s*</tool_mutation_patch>',
            raw_model_output,
            re.DOTALL,
        )
        vdir = os.path.join(self.workspace_dir, "tools", "versions") if self.workspace_dir != "." else None
        for action, tool_name, tool_code in tool_patches:
            clean_name = tool_name.strip()
            clean_action = action.strip()
            success, msg = ToolRegistry.apply_mutation_patch(
                clean_action, clean_name, tool_code.strip(), versions_dir=vdir
            )
            results["tools_mutated"].append(
                {"tool_name": clean_name, "action": clean_action, "success": success, "message": msg}
            )
            if success:
                ver = ToolRegistry._tool_versions.get(clean_name, 1)
                self.repo.log_tool_evolution(clean_name, clean_action, ver, msg)

        skill_patches = re.findall(
            r'<skill_precipitation_patch\s+skill_name="([^"]+)"\s+version="([^"]+)">(.*?)</skill_precipitation_patch>',
            raw_model_output,
            re.DOTALL,
        )
        for skill_name, version, patch_body in skill_patches:
            clean_skill = skill_name.strip()
            clean_ver = version.strip()
            script_match = re.search(r"```python\s*(.*?)\s*```", patch_body, re.DOTALL)
            script_code = script_match.group(1).strip() if script_match else None
            success, msg = self.skill_manager.apply_skill_patch(
                clean_skill, clean_ver, patch_body.strip(), script_code
            )
            results["skills_precipitated"].append(
                {"skill_name": clean_skill, "version": clean_ver, "success": success, "message": msg}
            )
            if success:
                self.repo.log_skill_evolution(clean_skill, clean_ver, msg)

        return results

    async def run_self_healing_cycle(
        self,
        failed_script_path: str,
        error_traceback: str,
        hub: Any,
        rule_name: Optional[str] = None,
    ) -> bool:
        """Self-healing loop: wake up LLM CLI once with traceback, verify via EvalHarness, and restore."""
        if not os.path.exists(failed_script_path):
            return False

        with open(failed_script_path, "r", encoding="utf-8") as f:
            broken_code = f.read()

        stem = os.path.basename(failed_script_path).replace(".py", "")
        heal_prompt = (
            f"[SELF_HEALING_TASK]\n"
            f"The deterministic script '{failed_script_path}' failed at runtime:\n"
            f"{error_traceback}\n\n"
            f"Original Code:\n```python\n{broken_code}\n```\n\n"
            f"Fix the bug, preserve 100% deterministic behaviour with a `run` entrypoint, "
            f"leave zero stubs, and wrap the fixed code in:\n"
            f'<crystallize_script name="{stem}">\n# fixed python code\n</crystallize_script>\n'
        )
        manifest = {"anchors": {"ANCHOR_1_error": {"error": error_traceback}}, "manifest_hash": "self_healing"}
        success, answer, _ = await hub.execute_task(heal_prompt, manifest, priority=1, preferred_engine="claude")
        if not success:
            if rule_name:
                self.repo.set_rule_status(rule_name, "retired")
            return False

        match = re.search(r'<crystallize_script\s+name="[^"]*">\s*(.*?)\s*</crystallize_script>', answer, re.DOTALL)
        if not match:
            return False

        fixed_code = match.group(1).strip()
        report = EvalHarness.evaluate_script(fixed_code, sample_queries=[stem])
        if not report.passed:
            logger.error("Self-healing EvalHarness rejected candidate fix: %s", report.reason)
            if rule_name:
                self.repo.set_rule_status(rule_name, "retired")
            return False

        with open(failed_script_path, "w", encoding="utf-8") as f:
            f.write(fixed_code + "\n")
        if rule_name:
            self.repo.set_rule_status(rule_name, "active")
        logger.info("Successfully self-healed script %s", failed_script_path)
        return True

    async def start_daemon(self) -> None:
        """Background maintenance loop refreshing global agent specs and stage routing policy."""
        self.running = True
        self.skill_manager.sync_global_agent_rules()
        ToolRegistry.load_persisted_tools()
        while self.running:
            self.current_policy = self.phase_controller.get_stage_policy()
            logger.debug(
                "RSI Lifecycle: score=%s stage=%s cache_theta=%s",
                self.current_policy.maturity_score,
                self.current_policy.stage.value,
                self.current_policy.semantic_cache_threshold,
            )
            await asyncio.sleep(300)

    def stop_daemon(self) -> None:
        self.running = False
