"""Out-of-Process Sandbox Runner for untrusted / LLM-generated Python code.

The host process NEVER `exec`s untrusted code. Every dry-run, mutated tool call
and crystallized L0 script runs in a short-lived child interpreter with:
  * mandatory AST security pre-scan (sandbox.approval_guard.ApprovalGuard)
  * bubblewrap isolation when available (read-only root, private /tmp, no network,
    credential directories masked), auto-detected once per process
  * POSIX rlimits (CPU seconds, address space, file size, no core dumps)
  * scrubbed minimal environment and wall-clock timeout
Results are returned as JSON through a sentinel marker on stdout.
"""
import asyncio
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
RESULT_MARKER = "__SANDBOX_RESULT__"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MEMORY_BYTES = 1024 * 1024 * 1024
DEFAULT_FSIZE_BYTES = 16 * 1024 * 1024

_BOOTSTRAP = r'''
import inspect, json, sys
payload = json.loads(sys.stdin.read())
for p in payload.get("sys_path", []):
    if p not in sys.path:
        sys.path.insert(0, p)
scope = {"__name__": "__sandbox__", "inputs": payload.get("inputs", {})}
exec(compile(payload["code"], payload.get("filename", "<sandbox>"), "exec"), scope)
entry = payload.get("entrypoint")
result = None
if entry:
    fn = scope.get(entry)
    if not callable(fn):
        raise SystemExit("SANDBOX_ENTRYPOINT_NOT_FOUND: " + entry)
    kwargs = payload.get("kwargs", {})
    params = inspect.signature(fn).parameters.values()
    if not any(p.kind == p.VAR_KEYWORD for p in params):
        accepted = {p.name for p in inspect.signature(fn).parameters.values()}
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    result = fn(**kwargs)
sys.stdout.write("\n__SANDBOX_RESULT__" + json.dumps({"result": result}, ensure_ascii=False, default=str))
'''


class SandboxExecutionError(Exception):
    """Raised when sandboxed code fails, times out, or violates security policy."""
    def __init__(self, message: str = "Sandbox execution failed"):
        super().__init__(message)


@dataclass
class SandboxResult:
    ok: bool
    result: Any
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    def error_summary(self, limit: int = 800) -> str:
        if self.timed_out:
            return "Sandbox execution timed out."
        tail = (self.stderr or self.stdout or "").strip()
        return tail[-limit:] if tail else f"Sandbox exited with code {self.returncode}."


_BWRAP_USABLE: Optional[bool] = None


def _bwrap_usable() -> bool:
    """Detect once whether bubblewrap can create user/net namespaces on this host."""
    global _BWRAP_USABLE
    if _BWRAP_USABLE is None:
        mode = os.environ.get("AGENT_SANDBOX_BWRAP", "auto").lower()
        binary = shutil.which("bwrap")
        if mode == "off" or not binary:
            _BWRAP_USABLE = False
        else:
            try:
                probe = subprocess.run(
                    [binary, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net", "true"],
                    capture_output=True, timeout=5, check=False,
                )
                _BWRAP_USABLE = probe.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _BWRAP_USABLE = False
    return _BWRAP_USABLE


SANDBOX_SCRATCH_DIR = "/tmp/agent_core/sandbox_tmp"


def _build_command(allow_network: bool) -> List[str]:
    python_cmd = [sys.executable, "-s", "-B", "-c", _BOOTSTRAP]
    if not _bwrap_usable():
        return python_cmd
    os.makedirs(SANDBOX_SCRATCH_DIR, exist_ok=True)
    cmd = [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", SANDBOX_SCRATCH_DIR, "--unshare-pid", "--die-with-parent",
        "--chdir", SANDBOX_SCRATCH_DIR,
    ]
    if not allow_network:
        cmd.append("--unshare-net")
    home = Path.home()
    for secret_dir in (home / ".ssh", home / ".aws", home / ".config" / "gcloud", home / ".gnupg"):
        if secret_dir.is_dir():
            cmd.extend(["--tmpfs", str(secret_dir)])
    dotenv_file = Path(PROJECT_ROOT) / ".env"
    if dotenv_file.is_file():
        cmd.extend(["--ro-bind", "/dev/null", str(dotenv_file)])
    return cmd + ["--"] + python_cmd


def _minimal_env() -> Dict[str, str]:
    os.makedirs(SANDBOX_SCRATCH_DIR, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": SANDBOX_SCRATCH_DIR,
        "TMPDIR": SANDBOX_SCRATCH_DIR,
    }
    return env


def _make_preexec(cpu_seconds: int, memory_bytes: int):
    def _apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (DEFAULT_FSIZE_BYTES, DEFAULT_FSIZE_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()
    return _apply_limits


def _payload(code: str, entrypoint: Optional[str], kwargs: Optional[Dict[str, Any]],
             inputs: Optional[Dict[str, Any]], filename: str) -> str:
    return json.dumps({
        "code": code,
        "entrypoint": entrypoint,
        "kwargs": kwargs or {},
        "inputs": inputs or {},
        "filename": filename,
        "sys_path": [PROJECT_ROOT],
    }, ensure_ascii=False, default=str)


def _parse(returncode: int, stdout: str, stderr: str, timed_out: bool) -> SandboxResult:
    result: Any = None
    clean_stdout = stdout
    if RESULT_MARKER in stdout:
        clean_stdout, _, raw = stdout.rpartition(RESULT_MARKER)
        try:
            result = json.loads(raw).get("result")
        except ValueError:
            result = None
    ok = (returncode == 0) and not timed_out
    return SandboxResult(ok=ok, result=result, stdout=clean_stdout, stderr=stderr, returncode=returncode, timed_out=timed_out)


def _security_gate(code: str) -> None:
    try:
        ApprovalGuard.scan_python_ast(code)
    except SandboxSecurityError as e:
        raise SandboxExecutionError(f"Security pre-scan rejected code: {e}") from e


def run_code_sync(
    code: str,
    entrypoint: Optional[str] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    inputs: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    allow_network: bool = False,
    filename: str = "<sandbox>",
) -> SandboxResult:
    """Synchronously execute code in an isolated child interpreter (never in-process)."""
    _security_gate(code)
    try:
        proc = subprocess.run(
            _build_command(allow_network),
            input=_payload(code, entrypoint, kwargs, inputs, filename),
            capture_output=True, text=True, timeout=timeout, env=_minimal_env(),
            cwd=tempfile.gettempdir(), preexec_fn=_make_preexec(int(timeout) + 1, memory_bytes), check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return SandboxResult(False, None, out, "", -9, timed_out=True)
    return _parse(proc.returncode, proc.stdout, proc.stderr, False)


async def run_code_async(
    code: str,
    entrypoint: Optional[str] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    inputs: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    allow_network: bool = False,
    filename: str = "<sandbox>",
) -> SandboxResult:
    """Asynchronously execute code in an isolated child interpreter without blocking the event loop."""
    _security_gate(code)
    proc = await asyncio.create_subprocess_exec(
        *_build_command(allow_network),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_minimal_env(), cwd=tempfile.gettempdir(),
        preexec_fn=_make_preexec(int(timeout) + 1, memory_bytes),
    )
    payload = _payload(code, entrypoint, kwargs, inputs, filename).encode("utf-8")
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return SandboxResult(False, None, "", "", -9, timed_out=True)
    return _parse(proc.returncode or 0, out_b.decode("utf-8", "ignore"), err_b.decode("utf-8", "ignore"), False)


async def run_file_async(
    script_path: str,
    entrypoint: str = "run",
    kwargs: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_network: bool = False,
) -> SandboxResult:
    """Read a script from disk and execute its entrypoint in the sandbox."""
    with open(script_path, "r", encoding="utf-8") as f:
        code = f.read()
    return await run_code_async(code, entrypoint=entrypoint, kwargs=kwargs, timeout=timeout,
                                allow_network=allow_network, filename=str(script_path))
