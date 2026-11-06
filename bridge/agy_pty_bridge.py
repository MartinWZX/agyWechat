"""Single-Host Persistent PTY Bridge & Native Subagent Orchestrator (Iron Law 1).
Features:
1. OS-level File Lock (~/.agent_core/agy_host.lock) to guarantee single resident agy host process.
2. Virtual PTY lifecycle management with ANSI cleaning and debounce buffering.
3. Central Serial Priority Queue (P0 WeChat Interactive > P1 Cron Cognitive > P2 Pattern Crystallization).
4. Real-time stream monitoring for 429/403 triggers & auto-crystallization tags.
5. Strict subagent concurrency enforcement (<=2 parallel, flash model, workspace isolation).
"""

import asyncio
import errno
import fcntl
import logging
import os
from pathlib import Path
import pty
import re
import signal
import struct
import termios
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import Settings, get_settings
from guard.wind_control_governor import WindControlGovernor, CircuitState

logger = logging.getLogger("wechat_agy.bridge")

# Stream detection regexes
RE_429 = re.compile(r"(429|Resource has been exhausted|rate limit|quota exceeded)", re.IGNORECASE)
RE_403 = re.compile(r"(403|Verify your account to continue|PERMISSION_DENIED|ToS Violation|blocked)", re.IGNORECASE)
RE_CRYSTALLIZE = re.compile(r"<crystallize_script\s+name=[\"'](.*?)[\"']>(.*?)</crystallize_script>", re.DOTALL | re.IGNORECASE)

# ANSI & Terminal escape sequences
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x1B\][^\x07\x1B]*?(?:\x07|\x1B\\)|[\x00-\x08\x0B\x0C\x0E-\x1A\x1C-\x1F]")


class HostLockError(Exception):
    """Raised when another agy host process already holds the OS lock."""
    def __init__(self, message: str = "Host lock already held"):
        super().__init__(message)


class PriorityItem:
    """Item for the PriorityEventQueue."""
    def __init__(self, priority: int, prompt: str, future: asyncio.Future, task_id: str = ""):
        self.priority = priority  # 0 = P0, 1 = P1, 2 = P2
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

    # ==========================================
    # OS File Lock & Lifecycle
    # ==========================================
    def acquire_host_lock(self) -> None:
        """Acquire exclusive OS file lock on ~/.agent_core/agy_host.lock."""
        lock_path = self.settings.AGY_LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file_fd = fd
            # Write current PID
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(fd)
            logger.info("Acquired agy host OS lock on %s (PID: %d)", lock_path, os.getpid())
        except (IOError, OSError) as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise HostLockError(
                    f"Fatal: Another agy host instance is already running (locked {lock_path}). Single-host iron law violated!"
                )
            raise

    def release_host_lock(self) -> None:
        """Release OS file lock."""
        if self._lock_file_fd is not None:
            try:
                fcntl.flock(self._lock_file_fd, fcntl.LOCK_UN)
                os.close(self._lock_file_fd)
                logger.info("Released agy host OS lock.")
            except Exception as e:
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
        logger.info("AgyPTYBridge started successfully.")

    async def _spawn_agy_pty(self) -> None:
        """Spawn native agy binary inside a virtual PTY."""
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._slave_fd = slave_fd

        # Set window size
        cols = self.settings.PTY_COLS
        rows = self.settings.PTY_ROWS
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # Set non-blocking mode on master fd
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        binary_path = self.settings.AGY_BINARY
        # Check if binary exists or search PATH
        if not Path(binary_path).exists():
            import shutil
            found = shutil.which(binary_path)
            if found:
                binary_path = found
            else:
                logger.warning("AGY binary %s not found on disk, running in mock/fallback mode", binary_path)

        cmd = [binary_path]
        logger.info("Spawning native agy process: %s in %s", cmd, self.settings.WORKSPACE_ROOT)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.settings.WORKSPACE_ROOT),
                env=env,
                preexec_fn=os.setsid,
            )
            # Close slave fd in parent process
            os.close(slave_fd)
            self._slave_fd = None
            logger.info("Agy process spawned successfully with PID: %d", self._process.pid)
        except Exception as e:
            logger.error("Failed to spawn agy process: %s", e)
            # Don't crash immediately, allow graceful fallback
            self._process = None

    # ==========================================
    # Priority Queue & Dispatcher
    # ==========================================
    async def enqueue_task(self, prompt: str, priority: int = 0, task_id: str = "") -> str:
        """Enqueue a cognitive task into the serial priority queue.
        priority: 0 (P0: Realtime WeChat), 1 (P1: Cron Cognitive), 2 (P2: Crystallization)
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = PriorityItem(priority=priority, prompt=prompt, future=future, task_id=task_id)
        await self._queue.put(item)
        logger.info("Enqueued task [P%d, %s]. Queue depth: %d", priority, task_id or "adhoc", self._queue.qsize())
        return await future

    async def _queue_worker_loop(self) -> None:
        """Single worker coroutine serializing all agy invocations."""
        while self._is_running:
            try:
                item = await self._queue.get()
                self._current_item = item
                self._current_buffer.clear()

                # 1. Pre-flight check via Wind Control Governor
                is_p0 = (item.priority == 0)
                can_invoke, reason, wait_time = await self.governor.check_can_invoke(is_p0=is_p0)

                if not can_invoke:
                    if wait_time and wait_time > 0 and wait_time <= 30.0 and is_p0:
                        # Smooth wait for P0 realtime request
                        logger.info("P0 request waiting %.1fs for rate window smoothing...", wait_time)
                        await asyncio.sleep(wait_time)
                        can_invoke, reason, _ = await self.governor.check_can_invoke(is_p0=True)

                if not can_invoke:
                    fallback_reply = f"🛡️ [风控拦截/本地兜底] Google 账号安全保护已介入：{reason}\n当前请求已被系统安全调度至本地代码兜底。"
                    item.future.set_result(fallback_reply)
                    self._queue.task_done()
                    self._current_item = None
                    continue

                # 2. Human Cadence Jitter (1.5s ~ 4.8s delay)
                await self.governor.apply_cadence_jitter()

                # 3. Write command to PTY
                start_time = time.time()
                await self._write_to_pty(item.prompt)

                # 4. Wait for response with debounce
                response = await self._wait_for_response()
                duration_ms = int((time.time() - start_time) * 1000)

                # 5. Record successful call in wind control logs
                await self.governor.record_invocation(
                    reason=f"P{item.priority}_task",
                    duration_ms=duration_ms,
                    subagents_count=1 if "invoke_subagent" in item.prompt else 0,
                    http_status=200,
                )

                self._turn_count += 1
                # Periodically trigger /compact to prevent context bloat
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
        """Write text followed by newline to master PTY."""
        if self._master_fd is None:
            logger.warning("No master PTY fd available, falling back to mock response.")
            self._current_buffer.append(f"[Mock Response] Executed: {text[:60]}")
            return

        clean_text = text.strip() + "\n"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self._master_fd, clean_text.encode("utf-8"))
        logger.debug("Written %d bytes to PTY", len(clean_text))

    async def _wait_for_response(self, timeout: float = 60.0) -> str:
        """Wait for agy terminal output to settle (debounce pattern)."""
        debounce_interval = self.settings.DEBOUNCE_SECONDS
        start_wait = time.time()

        # Wait until we see at least some output or timeout
        while time.time() - start_wait < timeout:
            await asyncio.sleep(0.3)
            now = time.time()
            if self._current_buffer and (now - self._last_output_time >= debounce_interval):
                break

        full_output = "".join(self._current_buffer)
        cleaned = self._clean_terminal_text(full_output)
        return cleaned if cleaned else "任务执行完毕，无输出。"

    def _clean_terminal_text(self, text: str) -> str:
        """Clean terminal ANSI codes, carriage returns, and control characters."""
        if not text:
            return ""
        stripped = ANSI_ESCAPE_RE.sub("", text)
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        return "\n".join(lines)

    async def _compact_session(self) -> None:
        """Send /compact to the persistent agy session to free up context."""
        logger.info("Executing periodic /compact on agy PTY...")
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b"/compact\n")
                self._turn_count = 0
            except Exception as e:
                logger.error("Failed to write /compact to PTY: %s", e)

    # ==========================================
    # PTY Stream Reader & Stream Pattern Monitor
    # ==========================================
    async def _pty_reader_loop(self) -> None:
        """Read output asynchronously from master PTY and inspect for 429/403/scripts."""
        loop = asyncio.get_running_loop()
        while self._is_running:
            if self._master_fd is None:
                await asyncio.sleep(1.0)
                continue

            try:
                # Use run_in_executor with select/read
                data = await loop.run_in_executor(None, self._read_pty_chunk)
                if not data:
                    await asyncio.sleep(0.1)
                    continue

                text = data.decode("utf-8", errors="replace")
                self._last_output_time = time.time()
                self._current_buffer.append(text)

                if self.on_output_chunk:
                    try:
                        res = self.on_output_chunk(text)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as err:
                        logger.debug("Crystallize callback error: %s", err)

                # Scan stream for 429 Rate Limit
                if RE_429.search(text):
                    logger.critical("429 Rate Limit detected in PTY stream!")
                    await self.governor.trigger_429_circuit_breaker(trigger_text=text)

                # Scan stream for 403 / Account Verification
                if RE_403.search(text):
                    logger.critical("403 / Verification detected in PTY stream!")
                    await self.governor.trigger_403_kill_switch(trigger_text=text)

                # Scan stream for auto-crystallization script tags
                script_matches = RE_CRYSTALLIZE.findall(text)
                for script_name, script_code in script_matches:
                    logger.info("Detected auto-crystallization script in stream: %s", script_name)
                    if self.on_crystallize_script:
                        try:
                            res = self.on_crystallize_script(script_name, script_code)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.error("Failed to handle crystallized script: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error reading from PTY: %s", e)
                await asyncio.sleep(0.5)

    def _read_pty_chunk(self) -> bytes:
        """Synchronously read available bytes from non-blocking master FD."""
        if self._master_fd is None:
            return b""
        try:
            return os.read(self._master_fd, 4096)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError as e:
            if e.errno == errno.EIO:  # EIO indicates child process has terminated
                return b""
            raise

    # ==========================================
    # Teardown
    # ==========================================
    async def stop(self) -> None:
        """Gracefully stop reader, worker, agy process, and release lock."""
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
            except Exception as err:
                logger.debug("Process kill error: %s", err)
            self._process = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except Exception as err:
                logger.debug("Master fd close error: %s", err)
            self._master_fd = None

        self.release_host_lock()
        logger.info("AgyPTYBridge stopped cleanly.")
