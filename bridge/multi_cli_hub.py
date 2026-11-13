"""Multi-CLI Hub with Bounded Concurrency (<=2), Priority Slot Queue, EWMA Routing & Anti-Hallucination Audit."""
import asyncio
import datetime
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from guard.wind_control_governor import WindControlGovernor
from verifier.hallucination_guard import LogicalConsistencyVerifier, WorkspaceSnapshotDiffer
from .drivers.agy_driver import AgyDriver
from .drivers.base_driver import BaseCLIAgentDriver
from .drivers.claude_driver import ClaudeDriver
from .drivers.omp_driver import OmpDriver
from .drivers.pi_driver import PiDriver
from .error_classifier import FailureKind, classify_cli_failure

logger = logging.getLogger("wechat_agy.multi_cli_hub")

EWMA_ALPHA = 0.3


class _PrioritySlotGate:
    """Enforces MAX_PARALLEL_SUBAGENTS (default 2) while granting slots in priority order (P0 > P1 > P2)."""

    def __init__(self, max_concurrency: int = 2):
        self.max_concurrency = max(1, int(max_concurrency))
        self._active = 0
        self._seq = 0
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []

    @property
    def active_count(self) -> int:
        return self._active

    async def acquire(self, priority: int = 0) -> None:
        if self._active < self.max_concurrency and not self._waiters:
            self._active += 1
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._seq += 1
        entry = (int(priority), self._seq, fut)
        self._waiters.append(entry)
        self._waiters.sort(key=lambda x: (x[0], x[1]))
        try:
            await fut
        except asyncio.CancelledError:
            if entry in self._waiters:
                self._waiters.remove(entry)
            elif fut.done():
                self.release()
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, next_fut = self._waiters.pop(0)
            if not next_fut.done():
                next_fut.set_result(True)
                return
        self._active = max(0, self._active - 1)


class MultiCLIHub:
    """Central orchestration hub managing agy, pi, claude, and omp with smart failover & RSI hooks."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        governor: Optional[WindControlGovernor] = None,
        store: Any = None,
        evolution_engine: Any = None,
        crystallizer: Any = None,
    ):
        self.config = config or {}
        self.governor = governor or WindControlGovernor(config=self.config)
        self.store = store
        self.evolution_engine = evolution_engine
        self.crystallizer = crystallizer

        eng_cfg = self.config.get("engines", {})
        self.drivers: Dict[str, BaseCLIAgentDriver] = {
            "agy": AgyDriver.from_engine_config(eng_cfg.get("agy")),
            "pi": PiDriver.from_engine_config(eng_cfg.get("pi")),
            "claude": ClaudeDriver.from_engine_config(eng_cfg.get("claude")),
            "omp": OmpDriver.from_engine_config(eng_cfg.get("omp")),
        }
        self.failover_order: List[str] = list(eng_cfg.get("failover_order", ["agy", "pi", "claude", "omp"]))
        max_parallel = int(eng_cfg.get("max_concurrency", 2))
        self.slot_gate = _PrioritySlotGate(max_concurrency=max_parallel)
        self._audit_lock = asyncio.Lock()

        sys_cfg = self.config.get("system", {})
        self.workspace_dir = sys_cfg.get("workspace_dir", ".")
        ignore_pats = sys_cfg.get("snapshot_ignore_patterns")
        self.snapshot_differ = WorkspaceSnapshotDiffer(self.workspace_dir, ignore_patterns=ignore_pats)

        self._ewma_success: Dict[str, float] = {eid: 1.0 for eid in self.drivers}
        self._ewma_latency_ms: Dict[str, float] = {eid: 1000.0 for eid in self.drivers}

    def _record_ewma(self, engine_id: str, ok: bool, latency_ms: float) -> None:
        prev_s = self._ewma_success.get(engine_id, 1.0)
        prev_l = self._ewma_latency_ms.get(engine_id, 1000.0)
        self._ewma_success[engine_id] = round(EWMA_ALPHA * (1.0 if ok else 0.0) + (1 - EWMA_ALPHA) * prev_s, 4)
        self._ewma_latency_ms[engine_id] = round(EWMA_ALPHA * latency_ms + (1 - EWMA_ALPHA) * prev_l, 2)

    def get_engine_metrics(self) -> Dict[str, Dict[str, float]]:
        return {
            eid: {
                "ewma_success": self._ewma_success.get(eid, 1.0),
                "ewma_latency_ms": self._ewma_latency_ms.get(eid, 1000.0),
            }
            for eid in self.drivers
        }

    def _rank_candidates(self, preferred_engine: Optional[str]) -> List[str]:
        ordered: List[str] = []
        if preferred_engine and preferred_engine in self.drivers:
            ordered.append(preferred_engine)
        adaptive = self.config.get("engines", {}).get("adaptive_routing", False)
        remaining = [e for e in self.failover_order if e in self.drivers and e not in ordered]
        if adaptive:
            remaining.sort(
                key=lambda eid: (
                    -self._ewma_success.get(eid, 1.0),
                    self._ewma_latency_ms.get(eid, 1000.0),
                    self.failover_order.index(eid),
                )
            )
        ordered.extend(remaining)
        return ordered

    async def execute_task(
        self,
        prompt: str,
        raw_manifest: Dict[str, Any],
        priority: int = 0,
        preferred_engine: Optional[str] = None,
        allowed_scope: Optional[List[str]] = None,
        auto_apply_rsi_hooks: bool = True,
    ) -> Tuple[bool, str, str]:
        """Execute task with bounded concurrency (<=2), priority slot scheduling, and physical audit."""
        await self.slot_gate.acquire(priority=priority)
        try:
            return await self._execute_with_failover(
                prompt,
                raw_manifest,
                priority,
                preferred_engine,
                allowed_scope,
                auto_apply_rsi_hooks=auto_apply_rsi_hooks,
            )
        finally:
            self.slot_gate.release()

    def _is_daily_budget_exhausted(self) -> Tuple[bool, int, str]:
        """Check current RSI stage daily_llm_budget against today's recorded CLI calls."""
        if not self.evolution_engine or not hasattr(self.evolution_engine, "phase_controller"):
            return False, 0, ""
        if not self.store or not hasattr(self.store, "_get_connection"):
            return False, 0, ""
        policy = self.evolution_engine.phase_controller.get_stage_policy()
        today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT COUNT(*) FROM wind_control_logs WHERE event_type = 'CALL' AND created_at >= ?;",
                (today_start,),
            )
            calls_today = int(c.fetchone()[0])
        if calls_today >= policy.daily_llm_budget:
            return True, policy.daily_llm_budget, policy.stage.value
        return False, policy.daily_llm_budget, policy.stage.value

    async def _run_driver_once(
        self,
        driver: BaseCLIAgentDriver,
        mode: str,
        prompt: str,
    ) -> Tuple[int, str, str, float]:
        t0 = time.monotonic()
        if mode == "pty":
            returncode, stdout, stderr = await driver.execute_pty(prompt, self.workspace_dir)
        else:
            returncode, stdout, stderr = await driver.execute_one_liner(prompt, self.workspace_dir)
        return returncode, stdout, stderr, (time.monotonic() - t0) * 1000.0

    async def _execute_with_failover(
        self,
        prompt: str,
        raw_manifest: Dict[str, Any],
        priority: int,
        preferred_engine: Optional[str],
        allowed_scope: Optional[List[str]],
        auto_apply_rsi_hooks: bool = True,
    ) -> Tuple[bool, str, str]:
        budget_exceeded, budget_limit, stage_name = await asyncio.to_thread(self._is_daily_budget_exhausted)
        if budget_exceeded:
            return False, f"BUDGET_EXCEEDED: Daily LLM budget ({budget_limit}) reached for stage {stage_name}.", "NONE"

        engines_to_try = self._rank_candidates(preferred_engine)
        last_error = ""
        strict_manifest = bool(self.config.get("anti_hallucination", {}).get("strict_manifest_check", True))

        for engine_id in engines_to_try:
            is_avail, reason = self.governor.check_engine_availability(engine_id, is_p0_urgent=(priority == 0))
            if not is_avail:
                last_error = f"Engine {engine_id} unavailable: {reason}"
                continue

            await self.governor.apply_cadence_jitter()
            driver = self.drivers[engine_id]
            mode = self.config.get("engines", {}).get(engine_id, {}).get("mode", "one_liner")

            local_differ = WorkspaceSnapshotDiffer(
                self.workspace_dir, ignore_patterns=self.snapshot_differ.ignore_patterns
            )
            if strict_manifest:
                async with self._audit_lock:
                    pre_snap = await asyncio.to_thread(local_differ.take_snapshot)
                    returncode, stdout, stderr, elapsed_ms = await self._run_driver_once(driver, mode, prompt)
                    await asyncio.to_thread(self.governor.record_call, engine_id)
            else:
                pre_snap = await asyncio.to_thread(local_differ.take_snapshot)
                returncode, stdout, stderr, elapsed_ms = await self._run_driver_once(driver, mode, prompt)
                await asyncio.to_thread(self.governor.record_call, engine_id)

            failure = classify_cli_failure(returncode, stdout, stderr)
            if failure == FailureKind.RATE_LIMIT_429:
                self._record_ewma(engine_id, False, elapsed_ms)
                self.governor.trigger_429_backoff(engine_id)
                last_error = f"Engine {engine_id} triggered anchored 429 rate limit."
                continue

            if failure == FailureKind.LOCKDOWN_403:
                self._record_ewma(engine_id, False, elapsed_ms)
                self.governor.trigger_403_lockdown(engine_id)
                last_error = f"Engine {engine_id} triggered anchored 403 RED LOCKDOWN."
                continue

            if failure == FailureKind.EXEC_ERROR:
                self._record_ewma(engine_id, False, elapsed_ms)
                last_error = f"Engine {engine_id} returned exit code {returncode}: {stderr}"
                continue

            manifest_match = re.search(r"<execution_manifest>(.*?)</execution_manifest>", stdout, re.DOTALL)
            parsed_manifest: Dict[str, Any] = {"files_created": [], "files_modified": [], "commands_executed": []}
            if manifest_match:
                try:
                    parsed_manifest = json.loads(manifest_match.group(1).strip())
                except ValueError as parse_err:
                    logger.debug("Execution manifest parse error: %s", parse_err)

            try:
                if strict_manifest:
                    post_snap = await asyncio.to_thread(local_differ.take_snapshot)
                    await asyncio.to_thread(
                        local_differ.audit_manifest,
                        pre_snap,
                        post_snap,
                        parsed_manifest,
                        allowed_scope=allowed_scope,
                    )
                ok_logic, reason_logic = LogicalConsistencyVerifier.verify(raw_manifest, stdout)
                if not ok_logic:
                    raise ValueError(reason_logic)
            except Exception as audit_err:
                self._record_ewma(engine_id, False, elapsed_ms)
                if self.store and hasattr(self.store, "log_hallucination"):
                    self.store.log_hallucination(
                        str(raw_manifest.get("manifest_hash", "task"))[:16],
                        engine_id,
                        type(audit_err).__name__,
                        str(audit_err),
                        reverted=True,
                    )
                last_error = f"Anti-hallucination check failed on engine {engine_id}: {audit_err}"
                continue

            self._record_ewma(engine_id, True, elapsed_ms)
            if auto_apply_rsi_hooks:
                self._apply_post_execution_rsi_hooks(stdout)
            return True, stdout, engine_id

        return False, f"All engines failed. Last error: {last_error}", "NONE"

    def _apply_post_execution_rsi_hooks(self, stdout: str) -> None:
        """Automatically harvest tool mutations, skill precipitations, and crystallized scripts."""
        auto_promote = True
        if self.evolution_engine and hasattr(self.evolution_engine, "phase_controller"):
            auto_promote = self.evolution_engine.phase_controller.get_stage_policy().auto_promote_verified
        if self.evolution_engine and hasattr(self.evolution_engine, "process_model_rsi_patches"):
            try:
                self.evolution_engine.process_model_rsi_patches(stdout)
            except Exception as exc:
                logger.warning("RSI patch harvesting failed: %s", exc)
        if self.crystallizer and hasattr(self.crystallizer, "crystallize_from_response"):
            try:
                self.crystallizer.crystallize_from_response(stdout, auto_promote=auto_promote)
            except Exception as exc:
                logger.warning("Crystallize-from-response hook failed: %s", exc)
