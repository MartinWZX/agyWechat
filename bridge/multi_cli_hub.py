"""Multi-CLI Hub with Bounded Concurrency (<=2), Priority Slot Queue, EWMA Routing & Anti-Hallucination Audit."""
import asyncio
import datetime
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from bridge.engine_command_discovery import EngineDiscoveryResult, resolve_binary
from guard.wind_control_governor import WindControlGovernor
from verifier.hallucination_guard import LogicalConsistencyVerifier, WorkspaceSnapshotDiffer
from .drivers.agy_driver import AgyDriver
from .drivers.base_driver import BaseCLIAgentDriver, extract_engine_json_result, format_workspace_diff_note
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

    def _rank_candidates(
        self, preferred_engine: Optional[str], strict_engine_pin: bool = False
    ) -> List[str]:
        if strict_engine_pin and preferred_engine and preferred_engine in self.drivers:
            return [preferred_engine]
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

    async def inspect_engine(
        self, engine_id: str, workspace_dir: Optional[str] = None
    ) -> EngineDiscoveryResult:
        """Probe a configured engine live on entry inside `workspace_dir` and build its command catalog."""
        if engine_id not in self.drivers:
            return EngineDiscoveryResult(
                engine_id=engine_id, available=False, error=f"未配置引擎 '{engine_id}'。"
            )
        target_ws = workspace_dir or self.workspace_dir
        return await self.drivers[engine_id].discover_commands(target_ws)

    async def execute_engine_subcommand(
        self, engine_id: str, args: List[str], workspace_dir: Optional[str] = None
    ) -> Tuple[int, str, str]:
        """Execute a safe read-only CLI subcommand on an engine in `workspace_dir`."""
        if engine_id not in self.drivers:
            return 127, "", f"Unknown engine '{engine_id}'"
        target_ws = workspace_dir or self.workspace_dir
        return await self.drivers[engine_id].execute_subcommand(args, target_ws)

    async def execute_engine_panel(
        self, engine_id: str, slash_cmd: str, workspace_dir: Optional[str] = None, extra_flags: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """Execute a zero-token CLI panel command without consuming LLM wind-control quota."""
        if engine_id not in self.drivers:
            return False, f"Unknown engine '{engine_id}'"
        rc, out, err = await self.drivers[engine_id].execute_with_flags(
            slash_cmd, workspace_dir or self.workspace_dir, extra_flags=extra_flags
        )
        if rc != 0:
            return False, (err or out or f"exit={rc}").strip()
        parsed, _ = extract_engine_json_result(out)
        return True, (parsed or out or err or "✅ 已执行完成。").strip()

    def list_configured_engines(self) -> List[Dict[str, Any]]:
        """Summarize binary readiness and governor circuit state for all configured engines."""
        rows: List[Dict[str, Any]] = []
        for eid, drv in self.drivers.items():
            resolved = resolve_binary(drv.binary_path)
            avail, reason = self.governor.check_engine_availability(eid, is_p0_urgent=True)
            rows.append({
                "engine_id": eid,
                "binary": drv.binary_path,
                "resolved": resolved or "",
                "installed": bool(resolved),
                "healthy": bool(avail),
                "reason": reason,
            })
        return rows

    async def execute_task(
        self,
        prompt: str,
        raw_manifest: Dict[str, Any],
        priority: int = 0,
        preferred_engine: Optional[str] = None,
        allowed_scope: Optional[List[str]] = None,
        auto_apply_rsi_hooks: bool = True,
        strict_engine_pin: bool = False,
        require_contract_tags: bool = True,
        cli_flags: Optional[List[str]] = None,
        workspace_dir: Optional[str] = None,
        coding_mode: bool = False,
        session_meta_out: Optional[Dict[str, Any]] = None,
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
                strict_engine_pin=strict_engine_pin,
                require_contract_tags=require_contract_tags,
                cli_flags=cli_flags,
                workspace_dir=workspace_dir,
                coding_mode=coding_mode,
                session_meta_out=session_meta_out,
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
        workspace_dir: str,
        cli_flags: Optional[List[str]] = None,
    ) -> Tuple[int, str, str, float]:
        t0 = time.monotonic()
        if mode == "pty":
            returncode, stdout, stderr = await driver.execute_pty(prompt, workspace_dir)
        elif cli_flags and hasattr(driver, "execute_with_flags"):
            returncode, stdout, stderr = await driver.execute_with_flags(
                prompt, workspace_dir, extra_flags=cli_flags
            )
        else:
            returncode, stdout, stderr = await driver.execute_one_liner(prompt, workspace_dir)
        return returncode, stdout, stderr, (time.monotonic() - t0) * 1000.0

    async def _execute_with_failover(
        self,
        prompt: str,
        raw_manifest: Dict[str, Any],
        priority: int,
        preferred_engine: Optional[str],
        allowed_scope: Optional[List[str]],
        auto_apply_rsi_hooks: bool = True,
        strict_engine_pin: bool = False,
        require_contract_tags: bool = True,
        cli_flags: Optional[List[str]] = None,
        workspace_dir: Optional[str] = None,
        coding_mode: bool = False,
        session_meta_out: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, str]:
        budget_exceeded, budget_limit, stage_name = await asyncio.to_thread(self._is_daily_budget_exhausted)
        if budget_exceeded:
            return False, f"BUDGET_EXCEEDED: Daily LLM budget ({budget_limit}) reached for stage {stage_name}.", "NONE"

        engines_to_try = self._rank_candidates(preferred_engine, strict_engine_pin=strict_engine_pin)
        last_error = ""
        target_ws = workspace_dir or self.workspace_dir
        strict_manifest = bool(self.config.get("anti_hallucination", {}).get("strict_manifest_check", True)) and not coding_mode

        for engine_id in engines_to_try:
            is_avail, reason = self.governor.check_engine_availability(engine_id, is_p0_urgent=(priority == 0))
            if not is_avail:
                last_error = f"Engine {engine_id} unavailable: {reason}"
                continue

            await self.governor.apply_cadence_jitter()
            driver = self.drivers[engine_id]
            mode = self.config.get("engines", {}).get(engine_id, {}).get("mode", "one_liner")

            local_differ = WorkspaceSnapshotDiffer(
                target_ws, ignore_patterns=self.snapshot_differ.ignore_patterns
            )
            if strict_manifest:
                async with self._audit_lock:
                    pre_snap = await asyncio.to_thread(local_differ.take_snapshot)
                    returncode, raw_stdout, stderr, elapsed_ms = await self._run_driver_once(
                        driver, mode, prompt, target_ws, cli_flags=cli_flags
                    )
                    await asyncio.to_thread(self.governor.record_call, engine_id)
            else:
                pre_snap = await asyncio.to_thread(local_differ.take_snapshot)
                returncode, raw_stdout, stderr, elapsed_ms = await self._run_driver_once(
                    driver, mode, prompt, target_ws, cli_flags=cli_flags
                )
                await asyncio.to_thread(self.governor.record_call, engine_id)

            stdout, detected_conv_id = extract_engine_json_result(raw_stdout)
            if session_meta_out is not None and detected_conv_id:
                session_meta_out["conversation_id"] = detected_conv_id

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

            diff_note = ""
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
                elif coding_mode:
                    post_snap = await asyncio.to_thread(local_differ.take_snapshot)
                    diff = local_differ.compute_diff(pre_snap, post_snap)
                    diff_note = format_workspace_diff_note(diff)
                    if session_meta_out is not None:
                        session_meta_out["diff"] = {k: sorted(v) for k, v in diff.items()}

                if (require_contract_tags and not coding_mode) or "<facts_anchor>" in stdout:
                    ok_logic, reason_logic = LogicalConsistencyVerifier.verify(raw_manifest, stdout)
                    if not ok_logic:
                        raise ValueError(reason_logic)
            except Exception as audit_err:
                self._record_ewma(engine_id, False, elapsed_ms)
                if self.store and hasattr(self.store, "log_hallucination"):
                    self.store.log_hallucination(
                        str(raw_manifest.get("manifest_hash", "task"))[:16],
                        engine_id, type(audit_err).__name__, str(audit_err), reverted=True,
                    )
                last_error = f"Anti-hallucination check failed on engine {engine_id}: {audit_err}"
                continue

            self._record_ewma(engine_id, True, elapsed_ms)
            if auto_apply_rsi_hooks:
                self._apply_post_execution_rsi_hooks(stdout)
            return True, stdout + diff_note, engine_id

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
