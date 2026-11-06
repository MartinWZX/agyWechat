"""Multi-CLI Hub with Priority Queue, Smart Failover Router & Anti-Hallucination Audit."""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from guard.wind_control_governor import WindControlGovernor
from verifier.hallucination_guard import (
    LogicalConsistencyVerifier,
    WorkspaceSnapshotDiffer,
)
from .drivers.agy_driver import AgyDriver
from .drivers.base_driver import BaseCLIAgentDriver
from .drivers.claude_driver import ClaudeDriver
from .drivers.omp_driver import OmpDriver
from .drivers.pi_driver import PiDriver

logger = logging.getLogger("wechat_agy.multi_cli_hub")


class MultiCLIHub:
    """Central orchestration hub managing agy, pi, claude, and omp with smart failover."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, governor: Optional[WindControlGovernor] = None):
        self.config = config or {}
        self.governor = governor or WindControlGovernor(config=self.config)
        self.drivers: Dict[str, BaseCLIAgentDriver] = {
            "agy": AgyDriver(),
            "pi": PiDriver(),
            "claude": ClaudeDriver(),
            "omp": OmpDriver()
        }
        self.failover_order = self.config.get("engines", {}).get("failover_order", ["agy", "pi", "claude", "omp"])
        self.workspace_dir = self.config.get("system", {}).get("workspace_dir", ".")
        self.snapshot_differ = WorkspaceSnapshotDiffer(self.workspace_dir)

    async def execute_task(
        self,
        prompt: str,
        raw_manifest: Dict[str, Any],
        priority: int = 0,
        preferred_engine: Optional[str] = None
    ) -> Tuple[bool, str, str]:
        """Execute task with Priority (P0 WeChat, P1 Cron, P2 Background) and Failover."""
        engines_to_try: List[str] = []
        if preferred_engine and preferred_engine in self.drivers:
            engines_to_try.append(preferred_engine)
        for eng in self.failover_order:
            if eng not in engines_to_try:
                engines_to_try.append(eng)

        last_error = ""
        for engine_id in engines_to_try:
            # 1. Wind Control Guard
            is_avail, reason = self.governor.check_engine_availability(engine_id, is_p0_urgent=(priority == 0))
            if not is_avail:
                last_error = f"Engine {engine_id} unavailable: {reason}"
                continue

            # 2. Human Cadence Jitter
            await self.governor.apply_cadence_jitter()
            driver = self.drivers[engine_id]

            # 3. Pre-execution physical snapshot
            pre_snap = self.snapshot_differ.take_snapshot()

            # 4. Execute CLI One-Liner
            returncode, stdout, stderr = await driver.execute_one_liner(prompt, self.workspace_dir)
            self.governor.record_call(engine_id)

            combined_out = stdout + "\n" + stderr

            # 429 Rate Limit Detection
            if "429" in combined_out or "rate_limit_error" in combined_out or "Resource has been exhausted" in combined_out:
                self.governor.trigger_429_backoff(engine_id)
                last_error = f"Engine {engine_id} triggered 429 rate limit."
                continue

            # 403 Permission Denied / ToS Detection
            if "403" in combined_out or "Verify your account" in combined_out or "PERMISSION_DENIED" in combined_out:
                self.governor.trigger_403_lockdown(engine_id)
                last_error = f"Engine {engine_id} triggered 403 RED LOCKDOWN."
                continue

            if returncode != 0:
                last_error = f"Engine {engine_id} returned exit code {returncode}: {stderr}"
                continue

            # 5. Anti-Hallucination Physical Audit
            post_snap = self.snapshot_differ.take_snapshot()

            manifest_match = re.search(r"<execution_manifest>(.*?)</execution_manifest>", stdout, re.DOTALL)
            parsed_manifest = {"files_created": [], "files_modified": [], "commands_executed": []}
            if manifest_match:
                try:
                    parsed_manifest = json.loads(manifest_match.group(1).strip())
                except Exception as parse_err:
                    logger.debug("Execution manifest parse error: %s", parse_err)
                    parsed_manifest = {"files_created": [], "files_modified": [], "commands_executed": []}

            try:
                self.snapshot_differ.audit_manifest(pre_snap, post_snap, parsed_manifest)
                LogicalConsistencyVerifier.verify(raw_manifest, stdout)
                return True, stdout, engine_id
            except Exception as audit_err:
                last_error = f"Anti-hallucination check failed on engine {engine_id}: {audit_err}"
                continue

        return False, f"All engines failed. Last error: {last_error}", "NONE"
