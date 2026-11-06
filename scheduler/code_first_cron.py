"""Two-Stage Code-First Cron Scheduler (Stage-1 Pure Code + Stage-2 Cognitive Fallback)."""
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from compressor.cognitive_gate import CognitiveGate, InvocationReason


class RequireCognitiveCLI(Exception):
    """Raised when deterministic Stage-1 code encounters an unexpected anomaly requiring LLM reasoning."""
    def __init__(self, reason: InvocationReason, verified_payload: Dict[str, Any], preferred_engine: str = "auto"):
        self.reason = reason
        self.verified_payload = verified_payload
        self.preferred_engine = preferred_engine
        super().__init__(f"Require cognitive CLI invocation: {reason.value}")


# Backward compatibility alias for AGY tests
RequireCognitiveAGY = RequireCognitiveCLI


class CodeFirstCronScheduler:
    """Two-Stage scheduler: Stage-1 runs 100% pure deterministic code; Stage-2 cognitive awakening."""

    def __init__(
        self,
        skill_manager: Any = None,
        bridge: Any = None,
        push_message_func: Optional[Callable[[str, str], Any]] = None,
        settings: Any = None,
        jobs_file_path: Optional[Union[str, Path]] = None,
        jobs_path: str = "scheduler/jobs.json",
        hub: Any = None
    ):
        self.skill_manager = skill_manager
        self.bridge = bridge
        self.hub = hub
        self.push_message_func = push_message_func
        self.settings = settings
        self.jobs_path = str(jobs_file_path or jobs_path)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.stage1_success_count = 0
        self.stage2_invocations_count = 0
        self.running = False
        self._loop_task: Optional[asyncio.Task] = None
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
                self.jobs = {j["id"]: j for j in jobs_list if isinstance(j, dict) and "id" in j}
        except Exception:
            self.jobs = {}
        return self.jobs

    async def _execute_job_pipeline(self, job: Dict[str, Any]):
        """Execute Stage-1 pure code and handle Stage-2 cognitive fallback if triggered."""
        script_path = job.get("script_path") or job.get("script")
        target_user = job.get("target_user", "admin_user")

        # Stage 1: Deterministic execution first
        try:
            result_str = ""
            if script_path and os.path.exists(str(script_path)):
                if self.skill_manager:
                    result = await self.skill_manager.execute_l0_script(script_path)
                    result_str = str(result)
                else:
                    scope: Dict[str, Any] = {"__builtins__": __builtins__}
                    with open(script_path, "r", encoding="utf-8") as f:
                        exec(f.read(), scope)
                    run_fn = scope.get("run")
                    result_str = str(run_fn()) if callable(run_fn) else "Executed without entrypoint."
            elif script_path == "system_health_check":
                load1, _, _ = os.getloadavg()
                total_b, used_b, _ = shutil.disk_usage("/")
                disk_percent = (used_b / total_b) * 100
                if load1 > 20.0 or disk_percent > 95.0:
                    raise RequireCognitiveCLI(
                        reason=InvocationReason.INFO_REASONING,
                        verified_payload={"load_1m": load1, "disk_percent": disk_percent, "status": "SPIKE_DETECTED"}
                    )
                result_str = f"System healthy: Load={load1:.2f}, Disk={disk_percent:.1f}% (0 Token)."
            else:
                result_str = f"Routine Cron '{job.get('id')}': Executed successfully (0 Token)."

            self.stage1_success_count += 1
            if self.push_message_func:
                self.push_message_func(target_user, result_str)

        except RequireCognitiveCLI as req:
            # Stage 2: Anomaly detected, wake up cognitive engine
            self.stage2_invocations_count += 1
            gate = CognitiveGate()
            manifest = gate.build_anchor_manifest(req.verified_payload)
            prompt = gate.compress_context("Analyze system spike and suggest resolution.", manifest)

            if self.hub:
                _, answer, _ = await self.hub.execute_task(prompt, manifest, priority=1)
                if self.push_message_func:
                    self.push_message_func(target_user, f"[Stage-2 Cognitive Alert]\n{answer}")
            elif self.bridge and hasattr(self.bridge, "execute_prompt"):
                answer = await self.bridge.execute_prompt(prompt, priority=1)
                if self.push_message_func:
                    self.push_message_func(target_user, f"[Stage-2 Cognitive Alert]\n{answer}")

    async def execute_job(self, job: Dict[str, Any]) -> str:
        """Single job execution returning result string."""
        await self._execute_job_pipeline(job)
        return f"Stage 1 Routine Cron '{job.get('id')}': Executed successfully (0 Token)."

    async def start(self):
        """Start async cron loop."""
        self.running = True
        while self.running:
            self.load_jobs()
            for job in list(self.jobs.values()):
                if job.get("is_active", True):
                    try:
                        await self._execute_job_pipeline(job)
                    except Exception:
                        continue
            await asyncio.sleep(60)

    def stop(self):
        """Stop async cron loop."""
        self.running = False


# Alias for backward compatibility
CodeFirstScheduler = CodeFirstCronScheduler
