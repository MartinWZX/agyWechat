"""Abstract Base CLI Agent Driver with Environment Scrubbing & Sandbox Command Audit."""
import abc
import asyncio
import fcntl
import os
import shlex
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError, scrub_environment


class BaseCLIAgentDriver(abc.ABC):
    """Unified base driver enforcing scrubbed credentials, command safety, and config injection."""

    def __init__(
        self,
        engine_id: str,
        binary_path: str,
        skip_flag: str,
        default_model: str = "fast",
        timeout: int = 60,
        max_turns: int = 3,
        lock_file: Optional[str] = None,
        env_passthrough: Optional[Iterable[str]] = None,
    ):
        self.engine_id = engine_id
        self.binary_path = binary_path
        self.skip_flag = skip_flag
        self.default_model = default_model
        self.timeout = int(timeout)
        self.max_turns = int(max_turns)
        self.lock_file = lock_file or f"/tmp/agent_core/locks/{engine_id}.lock"
        self.env_passthrough: List[str] = list(env_passthrough or ())

    @classmethod
    def from_engine_config(cls, engine_cfg: Optional[Dict[str, Any]] = None) -> "BaseCLIAgentDriver":
        cfg = engine_cfg or {}
        kwargs: Dict[str, Any] = {}
        if cfg.get("binary"):
            kwargs["binary_path"] = str(cfg["binary"])
        if "dangerously_skip_permissions_flag" in cfg:
            kwargs["skip_flag"] = str(cfg["dangerously_skip_permissions_flag"])
        if cfg.get("default_model"):
            kwargs["default_model"] = str(cfg["default_model"])
        if cfg.get("timeout_seconds"):
            kwargs["timeout"] = int(cfg["timeout_seconds"])
        if cfg.get("max_turns"):
            kwargs["max_turns"] = int(cfg["max_turns"])
        if cfg.get("pty_lock_file"):
            kwargs["lock_file"] = str(cfg["pty_lock_file"])
        if cfg.get("env_passthrough"):
            kwargs["env_passthrough"] = list(cfg["env_passthrough"])
        return cls(**kwargs)

    @abc.abstractmethod
    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        raise NotImplementedError("Subclasses must implement build_one_liner_cmd")

    def _audit_invocation_prefix(self, cmd: List[str], prompt: str) -> None:
        """Scan driver binary & flag arguments (excluding literal prompt payload) against blacklist."""
        non_prompt_tokens = [tok for tok in cmd if tok != prompt]
        ApprovalGuard.scan_shell_command(" ".join(shlex.quote(t) for t in non_prompt_tokens))

    async def execute_one_liner(self, prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        cmd = self.build_one_liner_cmd(prompt)
        try:
            self._audit_invocation_prefix(cmd, prompt)
        except SandboxSecurityError as sec_err:
            return 126, "", f"Sandbox command guard blocked invocation: {sec_err}"

        safe_env = scrub_environment(self.env_passthrough)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
                env=safe_env,
            )
        except FileNotFoundError:
            return 127, "", f"Binary '{self.binary_path}' not installed on system."
        except Exception as e:
            return -1, "", f"Subprocess start failed: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            return (
                proc.returncode if proc.returncode is not None else -1,
                stdout.decode("utf-8", errors="ignore"),
                stderr.decode("utf-8", errors="ignore"),
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"Execution timed out after {self.timeout}s"

    async def execute_pty(self, prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        """Single-host serialized execution protected by non-blocking OS flock."""
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        lock_f = open(self.lock_file, "w")
        try:
            await asyncio.to_thread(fcntl.flock, lock_f.fileno(), fcntl.LOCK_EX)
            try:
                return await self.execute_one_liner(prompt, workspace_dir)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        finally:
            lock_f.close()
