"""Abstract Base CLI Agent Driver with Full Host Environment Passthrough."""
import abc
import asyncio
import codecs
import contextlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from gateway.terminal_cleaner import ANSICleaner


def resolve_binary(binary_path: str) -> Optional[str]:
    """Resolve explicit path or search PATH and common user-local binary directories."""
    if not binary_path:
        return None
    if os.sep in binary_path or "/" in binary_path:
        p = Path(binary_path)
        return str(p) if p.is_file() else None
    extra_dirs = [
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.bun/bin"),
        "/usr/local/bin",
    ]
    current_path = os.environ.get("PATH", "")
    search_path = os.pathsep.join([current_path, *[d for d in extra_dirs if d not in current_path]])
    return shutil.which(binary_path, path=search_path)


async def run_cli_capture(
    argv: List[str],
    workspace_dir: str,
    timeout: float = 2.0,
) -> Tuple[int, str, str]:
    """Run a lightweight read-only binary check (such as `--version`) with full host env."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", f"Executable '{argv[0]}' not found."
    except OSError as exc:
        return -1, "", f"Failed to start subprocess: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout.decode("utf-8", errors="ignore"),
            stderr.decode("utf-8", errors="ignore"),
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "", f"Timed out after {timeout}s"


class BaseCLIAgentDriver(abc.ABC):
    """Unified base driver passing 100% host environment and native CLI flags directly."""

    supports_json_output: bool = False
    conversation_flag: str = ""
    supported_flags: Tuple[str, ...] = ()

    def __init__(
        self,
        engine_id: str,
        binary_path: str,
        skip_flag: str,
        default_model: str = "inherit",
        timeout: int = 0,
        max_turns: int = 0,
    ):
        self.engine_id = engine_id
        self.binary_path = binary_path
        self.skip_flag = skip_flag
        self.default_model = default_model
        self.timeout = int(timeout)
        self.max_turns = int(max_turns)

    @property
    def supports_native_session(self) -> bool:
        """Return True when this CLI driver supports structured JSON + conversation resumption."""
        return bool(self.supports_json_output and self.conversation_flag)

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
        if "timeout_seconds" in cfg and cfg["timeout_seconds"] is not None:
            kwargs["timeout"] = int(cfg["timeout_seconds"])
        if "max_turns" in cfg and cfg["max_turns"] is not None:
            kwargs["max_turns"] = int(cfg["max_turns"])

        sig_params = inspect.signature(cls.__init__).parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
        if not accepts_var_kw:
            kwargs = {k: v for k, v in kwargs.items() if k in sig_params}
        return cls(**kwargs)

    @abc.abstractmethod
    def build_one_liner_cmd(self, prompt: str) -> List[str]:
        raise NotImplementedError("Subclasses must implement build_one_liner_cmd")

    def _apply_default_model(self, cmd: List[str], extra_flags: Optional[List[str]] = None) -> List[str]:
        """Inject configured `--model` when default_model != 'inherit' and not overridden in extra_flags."""
        model = str(self.default_model or "").strip()
        if not model or model.lower() == "inherit":
            return cmd
        if "--model" in cmd or (extra_flags and any(str(f).strip() == "--model" for f in extra_flags)):
            return cmd
        insert_pos = 2 if len(cmd) > 1 and cmd[1] == "run" else 1
        return [*cmd[:insert_pos], "--model", model, *cmd[insert_pos:]]

    @staticmethod
    async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
        """Terminate an entire subprocess group and reap the child process cleanly."""
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    @staticmethod
    async def _drain_stream(stream: Optional[asyncio.StreamReader], chunks: List[str]) -> None:
        """Incrementally decode UTF-8 bytes from a subprocess pipe until EOF."""
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        while True:
            data = await stream.read(4096)
            if not data:
                tail = decoder.decode(b"", final=True)
                if tail:
                    chunks.append(tail)
                break
            text = decoder.decode(data, final=False)
            if text:
                chunks.append(text)

    async def _run_cmd(
        self,
        cmd: List[str],
        workspace_dir: str,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        progress_interval: float = 0.0,
    ) -> Tuple[int, str, str]:
        """Execute a native CLI invocation directly with optional streaming progress summaries."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
                env=os.environ.copy(),
                start_new_session=True,
            )
        except FileNotFoundError:
            return 127, "", f"Binary '{self.binary_path}' not installed on system."
        except Exception as e:
            return -1, "", f"Subprocess start failed: {e}"

        if progress_callback is None or progress_interval is None or progress_interval <= 0:
            try:
                if self.timeout is None or self.timeout <= 0:
                    stdout, stderr = await proc.communicate()
                else:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
                return (
                    proc.returncode if proc.returncode is not None else -1,
                    stdout.decode("utf-8", errors="ignore"),
                    stderr.decode("utf-8", errors="ignore"),
                )
            except asyncio.TimeoutError:
                await self._terminate_process_group(proc)
                return -1, "", f"Execution timed out after {self.timeout}s"
            except asyncio.CancelledError:
                await self._terminate_process_group(proc)
                raise

        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        stdout_task = asyncio.create_task(self._drain_stream(proc.stdout, stdout_chunks))
        stderr_task = asyncio.create_task(self._drain_stream(proc.stderr, stderr_chunks))
        wait_task = asyncio.create_task(proc.wait())
        cleaner = ANSICleaner()
        start_ts = time.monotonic()
        deadline = (start_ts + self.timeout) if (self.timeout is not None and self.timeout > 0) else None
        last_out_len = 0
        last_err_len = 0

        try:
            while not wait_task.done():
                wait_slice = float(progress_interval)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    wait_slice = min(wait_slice, remaining)

                done, _ = await asyncio.wait({wait_task}, timeout=wait_slice)
                if wait_task in done:
                    break

                full_out = "".join(stdout_chunks)
                full_err = "".join(stderr_chunks)
                delta_out = full_out[last_out_len:]
                delta_err = full_err[last_err_len:]
                last_out_len = len(full_out)
                last_err_len = len(full_err)

                raw_delta = "\n".join(part for part in (delta_out, delta_err) if part)
                cleaned_delta = cleaner.clean_text(raw_delta).strip()
                non_empty_lines = [ln.strip() for ln in cleaned_delta.splitlines() if ln.strip()]
                if non_empty_lines:
                    snippet = "\n".join(non_empty_lines[-3:])
                    if len(snippet) > 300:
                        snippet = snippet[-300:]
                else:
                    snippet = "正在执行任务，等待 CLI 输出..."

                elapsed = max(1, int(round(time.monotonic() - start_ts)))
                summary = f"⏳ [{self.engine_id.upper()} 运行中 · {elapsed}s]\n{snippet}"
                with contextlib.suppress(Exception):
                    await progress_callback(summary)

            await wait_task
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            return (
                proc.returncode if proc.returncode is not None else -1,
                "".join(stdout_chunks),
                "".join(stderr_chunks),
            )
        except asyncio.TimeoutError:
            for t in (wait_task, stdout_task, stderr_task):
                t.cancel()
            await asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True)
            await self._terminate_process_group(proc)
            return -1, "", f"Execution timed out after {self.timeout}s"
        except asyncio.CancelledError:
            for t in (wait_task, stdout_task, stderr_task):
                t.cancel()
            await asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True)
            await self._terminate_process_group(proc)
            raise

    async def execute_one_liner(
        self,
        prompt: str,
        workspace_dir: str,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        progress_interval: float = 0.0,
    ) -> Tuple[int, str, str]:
        """Execute standard non-interactive CLI prompt."""
        cmd = self._apply_default_model(self.build_one_liner_cmd(prompt))
        return await self._run_cmd(
            cmd,
            workspace_dir,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
        )

    async def execute_with_flags(
        self,
        prompt: str,
        workspace_dir: str,
        extra_flags: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        progress_interval: float = 0.0,
    ) -> Tuple[int, str, str]:
        """Execute non-interactive prompt with optional native session/output flags."""
        cmd = self._apply_default_model(self.build_one_liner_cmd(prompt), extra_flags=extra_flags)
        if extra_flags and cmd:
            clean_flags = [str(tok) for tok in extra_flags if str(tok)]
            if clean_flags:
                insert_pos = 2 if len(cmd) > 1 and cmd[1] == "run" else 1
                cmd = [*cmd[:insert_pos], *clean_flags, *cmd[insert_pos:]]
        return await self._run_cmd(
            cmd,
            workspace_dir,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
        )

    def is_installed(self) -> bool:
        """Return True iff this engine's configured binary resolves on the host system."""
        return resolve_binary(self.binary_path) is not None

    async def probe_version(self, workspace_dir: str = ".", timeout: float = 2.0) -> str:
        """Query `--version` from the installed CLI binary, returning empty string when missing/failing."""
        resolved = resolve_binary(self.binary_path)
        if not resolved:
            return ""
        rc, out, _ = await run_cli_capture([resolved, "--version"], workspace_dir, timeout=timeout)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0][:80]
        return ""


def _format_cli_command_payload(cmd_obj: Dict[str, Any]) -> str:
    """Format structured JSON `command` panel payloads when emitted by a CLI engine."""
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
    payload: Any = None
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
    if not isinstance(payload, dict) and "\n" in stripped:
        last_line = stripped.splitlines()[-1].strip()
        if last_line.startswith("{") and last_line.endswith("}"):
            try:
                candidate = json.loads(last_line)
                if isinstance(candidate, dict) and any(
                    k in candidate
                    for k in ("response", "result", "conversation_id", "session_id", "command", "error")
                ):
                    payload = candidate
            except ValueError:
                payload = None
    if not isinstance(payload, dict):
        return raw_stdout, None
    conv_id = str(payload.get("conversation_id") or payload.get("session_id") or "").strip() or None
    body = str(payload.get("response") or payload.get("result") or "")
    if not body and isinstance(payload.get("command"), dict):
        body = _format_cli_command_payload(payload["command"])
    elif not body and payload.get("error"):
        body = str(payload["error"])
    elif not body and str(payload.get("status") or "").upper() == "SUCCESS":
        info_parts = []
        dur = payload.get("duration_seconds")
        if isinstance(dur, (int, float)) and dur > 0:
            info_parts.append(f"耗时 {dur:.1f}s")
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens"):
            info_parts.append(f"tokens: {usage['total_tokens']}")
        body = f"✅ 执行完成（上下文已更新，{'，'.join(info_parts)}）" if info_parts else "✅ 执行完成。"
    return (body if body else raw_stdout), conv_id
