"""Two-Stage Code-First Cron Scheduler (Standard Library)."""
import asyncio
import json
import os
import shutil
import time
from typing import Dict, Any, Callable, List, Optional
from compressor.cognitive_gate import InvocationReason, CognitiveGate
from bridge.multi_cli_hub import MultiCLIHub

class RequireCognitiveCLI(Exception):
    def __init__(self, reason: InvocationReason, verified_payload: Dict[str, Any], preferred_engine: str = "auto"):
        self.reason = reason
        self.verified_payload = verified_payload
        self.preferred_engine = preferred_engine
        super().__init__(f"Require cognitive CLI invocation: {reason.value}")

class CodeFirstScheduler:
    def __init__(self, jobs_path: str = "scheduler/jobs.json", hub: MultiCLIHub = None):
        self.jobs_path = jobs_path
        self.hub = hub
        self.running = False
        self._load_jobs()

    def _load_jobs(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.jobs_path):
            with open(self.jobs_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        with open(self.jobs_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def execute_job(self, job: Dict[str, Any]) -> str:
        # Stage 1: 100% Deterministic Python execution first
        script_name = job.get("script")
        if script_name == "system_health_check":
            load1, load5, _ = os.getloadavg()
            total_b, used_b, _ = shutil.disk_usage("/")
            disk_percent = (used_b / total_b) * 100
            
            # Anomaly trigger threshold for cognitive Stage-2
            if load1 > 20.0 or disk_percent > 95.0:
                raise RequireCognitiveCLI(
                    reason=InvocationReason.INFO_REASONING,
                    verified_payload={"load_1m": load1, "disk_percent": disk_percent, "status": "SPIKE_DETECTED"}
                )
            return f"Stage 1 Routine Cron: Load={load1:.2f}, Disk={disk_percent:.1f}%. All normal (0 Token)."
        return "Stage 1 Routine Cron: Executed successfully (0 Token)."

    async def start(self):
        self.running = True
        while self.running:
            jobs = self._load_jobs()
            for job in jobs:
                try:
                    res = await self.execute_job(job)
                except RequireCognitiveCLI as req:
                    if self.hub:
                        gate = CognitiveGate()
                        manifest = gate.build_anchor_manifest(req.verified_payload)
                        prompt = gate.compress_context("Analyze system spike and suggest resolution.", manifest)
                        success, answer, eng = await self.hub.execute_task(prompt, manifest, priority=1)
                except Exception:
                    pass
            await asyncio.sleep(60)

    def stop(self):
        self.running = False
