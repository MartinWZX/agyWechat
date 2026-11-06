"""Multi-CLI Hub with Priority Queue & Failover Router."""
import asyncio
import json
import re
from typing import Dict, Any, List, Tuple, Optional
from .drivers.base_driver import BaseCLIAgentDriver
from .drivers.agy_driver import AgyDriver
from .drivers.pi_driver import PiDriver
from .drivers.claude_driver import ClaudeDriver
from .drivers.omp_driver import OmpDriver
from guard.wind_control_governor import WindControlGovernor
from verifier.hallucination_guard import WorkspaceSnapshotDiffer, LogicalConsistencyVerifier

class MultiCLIHub:
    def __init__(self, config: Dict[str, Any] = None, governor: WindControlGovernor = None):
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

    async def execute_task(self, prompt: str, raw_manifest: Dict[str, Any], priority: int = 0, preferred_engine: str = None) -> Tuple[bool, str, str]:
        # Priority: P0 (0, highest) WeChat text/voice, P1 (1) Cron anomaly, P2 (2) Crystallization
        engines_to_try = []
        if preferred_engine and preferred_engine in self.drivers:
            engines_to_try.append(preferred_engine)
        for eng in self.failover_order:
            if eng not in engines_to_try:
                engines_to_try.append(eng)

        last_error = ""
        for engine_id in engines_to_try:
            # 1. Wind Control Check
            is_avail, reason = self.governor.check_engine_availability(engine_id, is_p0_urgent=(priority == 0))
            if not is_avail:
                last_error = f"Engine {engine_id} unavailable: {reason}"
                continue

            # 2. Human Cadence Jitter
            self.governor.apply_human_jitter()
            driver = self.drivers[engine_id]

            # 3. Pre-execution physical snapshot
            pre_snap = self.snapshot_differ.take_snapshot()

            # 4. Execute CLI
            returncode, stdout, stderr = await driver.execute_one_liner(prompt, self.workspace_dir)
            self.governor.record_call(engine_id)

            # Check 429/403 in stdout/stderr
            combined_out = stdout + "\n" + stderr
            if "429" in combined_out or "rate_limit_error" in combined_out or "Resource has been exhausted" in combined_out:
                self.governor.trigger_429_backoff(engine_id)
                last_error = f"Engine {engine_id} triggered 429 rate limit."
                continue

            if "403" in combined_out or "Verify your account" in combined_out or "PERMISSION_DENIED" in combined_out:
                self.governor.trigger_403_lockdown(engine_id)
                last_error = f"Engine {engine_id} triggered 403 RED LOCKDOWN."
                continue

            if returncode != 0:
                last_error = f"Engine {engine_id} returned code {returncode}: {stderr}"
                continue

            # 5. Anti-hallucination verification
            post_snap = self.snapshot_differ.take_snapshot()
            
            # Extract execution manifest if present
            manifest_match = re.search(r"<execution_manifest>(.*?)</execution_manifest>", stdout, re.DOTALL)
            parsed_manifest = {"files_created": [], "files_modified": [], "commands_executed": []}
            if manifest_match:
                try:
                    parsed_manifest = json.loads(manifest_match.group(1).strip())
                except Exception:
                    pass

            try:
                self.snapshot_differ.audit_manifest(pre_snap, post_snap, parsed_manifest)
                LogicalConsistencyVerifier.verify(raw_manifest, stdout)
                return True, stdout, engine_id
            except Exception as e:
                last_error = f"Anti-hallucination check failed on engine {engine_id}: {e}"
                continue

        return False, f"All engines failed. Last error: {last_error}", "NONE"
