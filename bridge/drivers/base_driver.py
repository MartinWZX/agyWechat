"""Abstract Base CLI Agent Driver with Environment Scrubbing & Sandbox Command Audit."""
import abc
import asyncio
import fcntl
import json
import os
import shlex
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bridge.engine_command_discovery import (
    EngineDiscoveryResult,
    discover_engine_commands,
    resolve_binary,
    run_cli_capture,
)
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

    async def _run_cmd(self, cmd: List[str], prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        """Execute a single scrubbed non-interactive CLI invocation after blacklist audit."""
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

    async def execute_one_liner(self, prompt: str, workspace_dir: str) -> Tuple[int, str, str]:
        """Execute standard non-interactive CLI prompt."""
        return await self._run_cmd(self.build_one_liner_cmd(prompt), prompt, workspace_dir)

    async def execute_with_flags(
        self, prompt: str, workspace_dir: str, extra_flags: Optional[List[str]] = None
    ) -> Tuple[int, str, str]:
        """Execute non-interactive prompt with session-configured CLI overrides and resume flags."""
        cmd = self.build_one_liner_cmd(prompt)
        allowed_flags = {
            "--model", "--mode", "--effort", "--agent", "--add-dir",
            "--output-format", "--conversation", "--resume",
        }
        if extra_flags and cmd:
            validated: List[str] = []
            idx = 0
            while idx + 1 < len(extra_flags):
                flag_name = str(extra_flags[idx]).strip()
                flag_val = str(extra_flags[idx + 1]).strip()
                if flag_name in allowed_flags and flag_val:
                    validated.extend([flag_name, flag_val])
                idx += 2
            if validated:
                cmd = [cmd[0], *validated, *cmd[1:]]
        return await self._run_cmd(cmd, prompt, workspace_dir)

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

    async def discover_commands(self, workspace_dir: str, timeout: float = 20.0) -> EngineDiscoveryResult:
        """Query available commands & metadata dynamically from the installed CLI binary."""
        return await discover_engine_commands(
            engine_id=self.engine_id,
            binary_path=self.binary_path,
            workspace_dir=workspace_dir,
            env_passthrough=self.env_passthrough,
            timeout=timeout,
        )

    async def execute_subcommand(
        self, args: List[str], workspace_dir: str, timeout: float = 20.0
    ) -> Tuple[int, str, str]:
        """Run a verified CLI subcommand (e.g., models/agents/changelog) without LLM wrapping."""
        resolved = resolve_binary(self.binary_path)
        if not resolved:
            return 127, "", f"Binary '{self.binary_path}' not installed on system."
        return await run_cli_capture(
            [resolved, *args], workspace_dir, timeout=timeout, env_passthrough=self.env_passthrough
        )


def _format_cli_command_payload(cmd_obj: Dict[str, Any]) -> str:
    """Format structured JSON `command` panel payloads (e.g. /hooks, /permissions, /agents)."""
    cname = str(cmd_obj.get("name") or "command").strip()
    data = cmd_obj.get("data")
    if isinstance(data, dict):
        if len(data) == 1:
            key, val = next(iter(data.items()))
            if isinstance(val, list):
                if not val:
                    return f"[{cname}] {key}: (空)"
                rows = [
                    json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                    for item in val
                ]
                return f"[{cname}] {key}:\n" + "\n".join(f"• {row}" for row in rows)
        return json.dumps(data, ensure_ascii=False, indent=2)
    return json.dumps(cmd_obj, ensure_ascii=False, indent=2)


def extract_engine_json_result(raw_stdout: str) -> Tuple[str, Optional[str]]:
    """Extract clean text response and native conversation_id from CLI JSON output when present."""
    stripped = (raw_stdout or "").strip()
    if not stripped.startswith("{"):
        return raw_stdout, None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return raw_stdout, None
    if not isinstance(payload, dict):
        return raw_stdout, None
    conv_id = str(payload.get("conversation_id") or payload.get("session_id") or "").strip() or None
    body = str(payload.get("response") or payload.get("result") or "")
    if not body and isinstance(payload.get("command"), dict):
        body = _format_cli_command_payload(payload["command"])
    if not body and payload.get("status") in ("SUCCESS", "success"):
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        turns = payload.get("num_turns")
        tokens = usage.get("total_tokens")
        meta_parts = [p for p in (f"turns={turns}" if turns is not None else "", f"tokens={tokens}" if tokens else "") if p]
        suffix = f"（{', '.join(meta_parts)}）" if meta_parts else ""
        body = f"✅ CLI 指令已执行完成{suffix}。"
    elif not body and payload.get("status") not in (None, "SUCCESS", "success"):
        body = str(payload.get("error") or raw_stdout)
    if "response" in payload or "result" in payload or isinstance(payload.get("command"), dict):
        return body, conv_id
    return raw_stdout, conv_id


def format_workspace_diff_note(diff: Dict[str, Any]) -> str:
    """Summarize physical file changes made during a coding turn without modifying workspace files."""
    parts: List[str] = []
    for label, key in (("新建", "created"), ("修改", "modified"), ("删除", "deleted")):
        items = sorted(diff.get(key) or ())
        if items:
            preview = ", ".join(items[:6])
            if len(items) > 6:
                preview += f" 等共{len(items)}个"
            parts.append(f"{label}: {preview}")
    if not parts:
        return ""
    return "\n\n📁 本轮真实文件变更：" + " | ".join(parts)
