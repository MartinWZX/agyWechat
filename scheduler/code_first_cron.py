"""Two-Stage Code-First Cron Scheduler (Stage-1 Sandboxed Pure Code + Stage-2 Cognitive Fallback)."""
import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Dict, Optional, Union

from compressor.cognitive_gate import CognitiveGate, InvocationReason
from sandbox.runner import run_file_async
from scheduler.cron_expr import cron_is_due

logger = logging.getLogger("wechat_agy.scheduler")


class RequireCognitiveCLI(Exception):
    """Raised when deterministic Stage-1 code encounters an anomaly requiring LLM reasoning."""

    def __init__(self, reason: InvocationReason, verified_payload: Dict[str, Any], preferred_engine: str = "auto"):
        self.reason = reason
        self.verified_payload = verified_payload
        self.preferred_engine = preferred_engine
        super().__init__(f"Require cognitive CLI invocation: {reason.value}")


RequireCognitiveAGY = RequireCognitiveCLI


class CodeFirstCronScheduler:
    """Two-Stage scheduler: Stage-1 runs pure code in sandbox; Stage-2 wakes cognitive CLI on anomaly."""

    def __init__(
        self,
        skill_manager: Any = None,
        bridge: Any = None,
        push_message_func: Optional[Callable[[str, str], Any]] = None,
        settings: Any = None,
        jobs_file_path: Optional[Union[str, Path]] = None,
        jobs_path: str = "scheduler/jobs.json",
        hub: Any = None,
        rsi_engine: Any = None,
        health_monitor: Any = None,
    ):
        self.skill_manager = skill_manager
        self.bridge = bridge
        self.hub = hub
        self.rsi_engine = rsi_engine
        self.health_monitor = health_monitor
        self.push_message_func = push_message_func
        self.settings = settings
        self.jobs_path = str(jobs_file_path or jobs_path)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.last_run_map: Dict[str, float] = {}
        self.stage1_success_count = 0
        self.stage2_invocations_count = 0
        self.running = False
        self.load_jobs()

    def load_jobs(self) -> Dict[str, Dict[str, Any]]:
        """Load and cache registered jobs from JSON."""
        if not os.path.exists(self.jobs_path):
            os.makedirs(os.path.dirname(os.path.abspath(self.jobs_path)), exist_ok=True)
            with open(self.jobs_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            self.jobs = {}
            return self.jobs

        try:
            with open(self.jobs_path, "r", encoding="utf-8") as f:
                jobs_list = json.load(f)
            if isinstance(jobs_list, list):
                self.jobs = {j["id"]: j for j in jobs_list if isinstance(j, dict) and "id" in j}
            else:
                self.jobs = {}
        except (OSError, ValueError) as exc:
            logger.warning("Failed to parse jobs file %s: %s", self.jobs_path, exc)
            self.jobs = {}
        return self.jobs

    async def _deliver_push(self, target_user: str, message: str) -> None:
        if not self.push_message_func:
            return
        res = self.push_message_func(target_user, message)
        if asyncio.iscoroutine(res):
            await res

    async def _execute_job_pipeline(self, job: Dict[str, Any]) -> None:
        """Execute Stage-1 pure code in sandbox and handle Stage-2 cognitive fallback or self-healing."""
        job_id = str(job.get("id", "unknown_job"))
        script_path = job.get("script_path") or job.get("script")
        target_user = str(job.get("target_user", "admin_user"))

        try:
            raw_result: Any = ""
            if script_path and os.path.exists(str(script_path)):
                if self.skill_manager:
                    raw_result = await self.skill_manager.execute_l0_script(script_path)
                else:
                    sres = await run_file_async(str(script_path), entrypoint="run", timeout=15.0)
                    if not sres.ok:
                        raise RuntimeError(sres.error_summary())
                    raw_result = sres.result
            elif script_path == "system_health_check":
                load1, _, _ = os.getloadavg()
                total_b, used_b, _ = shutil.disk_usage("/")
                disk_percent = (used_b / total_b) * 100
                if load1 > 20.0 or disk_percent > 95.0:
                    raise RequireCognitiveCLI(
                        reason=InvocationReason.INFO_REASONING,
                        verified_payload={"load_1m": load1, "disk_percent": disk_percent, "status": "SPIKE_DETECTED"},
                    )
                raw_result = f"System healthy: Load={load1:.2f}, Disk={disk_percent:.1f}% (0 Token)."
            else:
                raw_result = f"Routine Cron '{job_id}': Executed successfully (0 Token)."

            if isinstance(raw_result, dict) and raw_result.get("require_cognitive"):
                reason_name = str(raw_result.get("reason", "INFO_REASONING"))
                reason_enum = InvocationReason(reason_name) if reason_name in InvocationReason.__members__ else InvocationReason.INFO_REASONING
                raise RequireCognitiveCLI(
                    reason=reason_enum,
                    verified_payload=dict(raw_result.get("payload") or {}),
                )

            self.stage1_success_count += 1
            if self.health_monitor and hasattr(self.health_monitor, "record_cron_run"):
                self.health_monitor.record_cron_run()
            await self._deliver_push(target_user, str(raw_result))

        except RequireCognitiveCLI as req:
            self.stage2_invocations_count += 1
            gate = CognitiveGate()
            manifest = gate.build_anchor_manifest(req.verified_payload)
            job_label = str(job.get("raw_sample") or job.get("name") or job_id)
            prompt = gate.compress_context(
                f"Analyze Stage-1 cron anomaly for '{job_label}' and suggest resolution.",
                manifest,
            )
            pref = None if req.preferred_engine == "auto" else req.preferred_engine

            if self.hub:
                _, answer, _ = await self.hub.execute_task(prompt, manifest, priority=1, preferred_engine=pref)
                await self._deliver_push(target_user, f"[Stage-2 Cognitive Alert]\n{answer}")
            elif self.bridge and hasattr(self.bridge, "execute_prompt"):
                answer = await self.bridge.execute_prompt(prompt, priority=1)
                await self._deliver_push(target_user, f"[Stage-2 Cognitive Alert]\n{answer}")

        except Exception as err:
            logger.error("Cron job '%s' Stage-1 failed: %s", job_id, err)
            if self.rsi_engine and self.hub and script_path and os.path.exists(str(script_path)):
                await self.rsi_engine.run_self_healing_cycle(str(script_path), str(err), self.hub)

    def _persist_last_runs(self) -> None:
        if not self.jobs:
            return
        try:
            with open(self.jobs_path, "w", encoding="utf-8") as f:
                json.dump(list(self.jobs.values()), f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.debug("Failed to persist cron last_run metadata: %s", exc)

    async def execute_job(self, job: Dict[str, Any]) -> str:
        """Execute a single job immediately and record its run timestamp."""
        job_id = str(job.get("id", "adhoc"))
        await self._execute_job_pipeline(job)
        now_ts = time.time()
        self.last_run_map[job_id] = now_ts
        if job_id in self.jobs:
            self.jobs[job_id]["last_run"] = now_ts
            self._persist_last_runs()
        return f"Stage 1 Routine Cron '{job_id}': Executed successfully (0 Token)."

    def is_job_due(self, job: Dict[str, Any], now_ts: Optional[float] = None) -> bool:
        """Evaluate 5-field cron expression against current timestamp and job's last_run."""
        if not job.get("is_active", True):
            return False
        cron_str = str(job.get("cron") or job.get("schedule") or "").strip()
        if not cron_str:
            return False
        ts = now_ts if now_ts is not None else time.time()
        job_id = str(job.get("id", ""))
        last_ts = self.last_run_map.get(job_id, float(job.get("last_run", 0.0) or 0.0))
        try:
            return cron_is_due(cron_str, ts, last_ts)
        except ValueError as exc:
            logger.warning("Invalid cron expression '%s' on job '%s': %s", cron_str, job_id, exc)
            return False

    async def tick(self, now_ts: Optional[float] = None) -> int:
        """Run one scheduler tick, executing only jobs whose cron schedule is currently due."""
        ts = now_ts if now_ts is not None else time.time()
        self.load_jobs()
        executed = 0
        for job_id, job in list(self.jobs.items()):
            if self.is_job_due(job, now_ts=ts):
                self.last_run_map[job_id] = ts
                job["last_run"] = ts
                await self._execute_job_pipeline(job)
                executed += 1
        if executed:
            self._persist_last_runs()
        return executed

    async def start(self) -> None:
        """Start async cron loop polling every 20s with exact minute deduplication."""
        self.running = True
        while self.running:
            await self.tick()
            await asyncio.sleep(20)

    def stop(self) -> None:
        self.running = False


CodeFirstScheduler = CodeFirstCronScheduler
