"""Abstract Base CLI Agent Driver."""
import abc
import asyncio
import os
import fcntl
from typing import Tuple, List

class BaseCLIAgentDriver(abc.ABC):
    def __init__(self, engine_id: str, binary_path: str, skip_flag: str, default_model: str = "fast", timeout: int = 60, lock_file: str = None):
        self.engine_id = engine_id
        self.binary_path = binary_path
        self.skip_flag = skip_flag
        self.default_model = default_model
        self.timeout = timeout
        self.lock_file = lock_file or f"/tmp/agent_core/locks/{engine_id}.lock"

    @abc.abstractmethod
    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        raise NotImplementedError("Subclasses must implement build_one_liner_cmd")

    async def execute_one_liner(self, prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        cmd = self.build_one_liner_cmd(prompt)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir
            )
        except FileNotFoundError:
            return 127, "", f"Binary '{self.binary_path}' not installed on system."
        except Exception as e:
            return -1, "", f"Subprocess start failed: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            return proc.returncode, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Execution timed out after {self.timeout}s"

    async def execute_pty(self, prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        # Single-Host persistent PTY session using OS flock
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        with open(self.lock_file, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                return await self.execute_one_liner(prompt, workspace_dir)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
