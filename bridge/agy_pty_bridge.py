"""Single-Host Persistent PTY Bridge & Serial Priority Queue for Native `agy` CLI."""
import asyncio
import errno
import fcntl
import logging
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import struct
import termios
import time
from typing import Any, Callable, List, Optional

from bridge.error_classifier import FailureKind, classify_stream_chunk
from config import Settings, get_settings
from guard.wind_control_governor import WindControlGovernor
from sandbox.approval_guard import scrub_environment

logger = logging.getLogger("wechat_agy.bridge")

RE_CRYSTALLIZE = re.compile(
    r"<crystallize_script\s+name=[\"'](.*?)[\"']>(.*?)</crystallize_script>",
    re.DOTALL | re.IGNORECASE,
)
ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x1B\][^\x07\x1B]*?(?:\x07|\x1B\\)|[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]"
)


class HostLockError(Exception):
    """Raised when another agy host process already holds the OS lock."""
    def __init__(self, message: str = "Host lock already held"):
        super().__init__(message)


class PriorityItem:
    """Item for the serial priority queue (P0 WeChat > P1 Cron > P2 Background)."""

    def __init__(self, priority: int, prompt: str, future: asyncio.Future, task_id: str = ""):
        self.priority = priority
        self.prompt = prompt
        self.future = future
        self.task_id = task_id
        self.created_at = time.time()

    def __lt__(self, other: "PriorityItem") -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


class AgyPTYBridge:
    """Single persistent agy process manager with virtual PTY and serial priority queue."""

    def __init__(
        self,
        governor: WindControlGovernor,
        settings: Optional[Settings] = None,
        on_crystallize_script: Optional[Callable[[str, str], Any]] = None,
        on_output_chunk: Optional[Callable[[str], Any]] = None,
    ):
        self.governor = governor
        self.settings = settings or get_settings()
        self.on_crystallize_script = on_crystallize_script
        self.on_output_chunk = on_output_chunk

        self._lock_file_fd: Optional[int] = None
        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None
        self._process: Optional[asyncio.subprocess.Process] = None

        self._queue: asyncio.PriorityQueue[PriorityItem] = asyncio.PriorityQueue()
        self._worker_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None

        self._current_buffer: List[str] = []
        self._current_item: Optional[PriorityItem] = None
        self._is_running = False
        self._last_output_time = 0.0
        self._turn_count = 0

    def acquire_host_lock(self) -> None:
        """Acquire exclusive OS file lock on ~/.agent_core/agy_host.lock."""
        lock_path = self.settings.AGY_LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file_fd = fd
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(fd)
            logger.info("Acquired agy host OS lock on %s (PID: %d)", lock_path, os.getpid())
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise HostLockError(
                    f"Fatal: Another agy host instance is already running (locked {lock_path})."
                )
            raise

    def release_host_lock(self) -> None:
        """Release OS file lock."""
        if self._lock_file_fd is not None:
            try:
                fcntl.flock(self._lock_file_fd, fcntl.LOCK_UN)
                os.close(self._lock_file_fd)
            except OSError as e:
                logger.error("Error releasing host lock: %s", e)
            self._lock_file_fd = None

    async def start(self) -> None:
        """Initialize PTY and start background worker and reader coroutines."""
        if self._is_running:
            return
        self.acquire_host_lock()
        await self._spawn_agy_pty()
        self._is_running = True
        self._reader_task = asyncio.create_task(self._pty_reader_loop(), name="agy_pty_reader")
        self._worker_task = asyncio.create_task(self._queue_worker_loop(), name="agy_queue_worker")

    async def _spawn_agy_pty(self) -> None:
        """Spawn native agy binary inside a virtual PTY with scrubbed credentials."""
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._slave_fd = slave_fd

        winsize = struct.pack("HHHH", self.settings.PTY_ROWS, self.settings.PTY_COLS, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        binary_path = self.settings.AGY_BINARY
        if not Path(binary_path).exists():
            found = shutil.which(binary_path)
            if found:
                binary_path = found
            else:
                logger.warning("AGY binary %s not found on disk, running in fallback mode", binary_path)

        env = scrub_environment()
        env["TERM"] = "xterm-256color"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self._process = await asyncio.create_subprocess_exec(
                binary_path,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.settings.WORKSPACE_ROOT),
                env=env,
                preexec_fn=os.setsid,
            )
            os.close(slave_fd)
            self._slave_fd = None
        except OSError as e:
            logger.error("Failed to spawn agy process: %s", e)
            self._process = None

    async def enqueue_task(self, prompt: str, priority: int = 0, task_id: str = "") -> str:
        """Enqueue a cognitive task into the serial priority queue."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = PriorityItem(priority=priority, prompt=prompt, future=future, task_id=task_id)
        await self._queue.put(item)
        return await future

    async def execute_prompt(self, prompt: str, priority: int = 0) -> str:
        """Compatibility alias for enqueue_task."""
        return await self.enqueue_task(prompt=prompt, priority=priority)

    async def _queue_worker_loop(self) -> None:
        while self._is_running:
            try:
                item = await self._queue.get()
                self._current_item = item
                self._current_buffer.clear()

                is_p0 = (item.priority == 0)
                can_invoke, reason, wait_time = await self.governor.check_can_invoke(is_p0=is_p0)
                if not can_invoke and wait_time and 0 < wait_time <= 30.0 and is_p0:
                    await asyncio.sleep(wait_time)
                    can_invoke, reason, _ = await self.governor.check_can_invoke(is_p0=True)

                if not can_invoke:
                    fallback = f"🛡️ [风控拦截/本地兜底] 账号安全保护已介入：{reason}"
                    item.future.set_result(fallback)
                    self._queue.task_done()
                    self._current_item = None
                    continue

                await self.governor.apply_cadence_jitter()
                await self._write_to_pty(item.prompt)
                response = await self._wait_for_response()
                self.governor.record_call("agy")

                self._turn_count += 1
                if self._turn_count >= 15:
                    await self._compact_session()

                item.future.set_result(response)
                self._queue.task_done()
                self._current_item = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in agy queue worker: %s", e)
                if self._current_item and not self._current_item.future.done():
                    self._current_item.future.set_exception(e)
                self._current_item = None

    async def _write_to_pty(self, text: str) -> None:
        if self._master_fd is None:
            self._current_buffer.append(f"[Mock Response] Executed: {text[:60]}")
            return
        clean_text = text.strip() + "\n"
        await asyncio.to_thread(os.write, self._master_fd, clean_text.encode("utf-8"))

    async def _wait_for_response(self, timeout: float = 60.0) -> str:
        debounce_interval = self.settings.DEBOUNCE_SECONDS
        start_wait = time.time()
        while time.time() - start_wait < timeout:
            await asyncio.sleep(0.3)
            if self._current_buffer and (time.time() - self._last_output_time >= debounce_interval):
                break
        cleaned = self._clean_terminal_text("".join(self._current_buffer))
        return cleaned if cleaned else "任务执行完毕，无输出。"

    def _clean_terminal_text(self, text: str) -> str:
        if not text:
            return ""
        stripped = ANSI_ESCAPE_RE.sub("", text)
        return "\n".join(line.strip() for line in stripped.splitlines() if line.strip())

    async def _compact_session(self) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b"/compact\n")
                self._turn_count = 0
            except OSError as e:
                logger.error("Failed to write /compact to PTY: %s", e)

    async def _pty_reader_loop(self) -> None:
        while self._is_running:
            if self._master_fd is None:
                await asyncio.sleep(1.0)
                continue
            try:
                data = await asyncio.to_thread(self._read_pty_chunk)
                if not data:
                    await asyncio.sleep(0.1)
                    continue

                text = data.decode("utf-8", errors="replace")
                self._last_output_time = time.time()
                self._current_buffer.append(text)

                if self.on_output_chunk:
                    res = self.on_output_chunk(text)
                    if asyncio.iscoroutine(res):
                        await res

                failure = classify_stream_chunk(text)
                if failure == FailureKind.RATE_LIMIT_429:
                    await self.governor.trigger_429_circuit_breaker(reason=text[:200], engine_id="agy")
                elif failure == FailureKind.LOCKDOWN_403:
                    await self.governor.trigger_403_kill_switch(reason=text[:200], engine_id="agy")

                for script_name, script_code in RE_CRYSTALLIZE.findall(text):
                    if self.on_crystallize_script:
                        res = self.on_crystallize_script(script_name, script_code)
                        if asyncio.iscoroutine(res):
                            await res
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error reading from PTY: %s", e)
                await asyncio.sleep(0.5)

    def _read_pty_chunk(self) -> bytes:
        if self._master_fd is None:
            return b""
        try:
            return os.read(self._master_fd, 4096)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError as e:
            if e.errno == errno.EIO:
                return b""
            raise

    async def stop(self) -> None:
        self._is_running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self._reader_task:
            self._reader_task.cancel()
        if self._process:
            try:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._process.kill()
            except OSError as err:
                logger.debug("Process kill error: %s", err)
            self._process = None
        for fd_attr in ("_master_fd", "_slave_fd"):
            fd_val = getattr(self, fd_attr)
            if fd_val is not None:
                try:
                    os.close(fd_val)
                except OSError as err:
                    logger.debug("FD close error: %s", err)
                setattr(self, fd_attr, None)
        self.release_host_lock()
