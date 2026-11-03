"""Singleton AGY process manager with PTY support, 3-layer cleanup, and dual-state machine."""

import asyncio
import errno
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, List, Optional
import psutil

from app.config import Settings, get_settings
from app.terminal_cleaner import DebounceBuffer

logger = logging.getLogger("wechat_agy.singleton")


class SystemState(str, Enum):
    """Dual-state enum for the system."""
    HOST_MODE = "HOST_MODE"
    AGY_MODE = "AGY_MODE"


class SingletonAGYManager:
    """Manages the lifecycle of a strictly unique agy process attached to a virtual PTY."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        push_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.settings = settings or get_settings()
        self.push_callback = push_callback
        self.GLOBAL_CWD: Path = self.settings.WORKSPACE_ROOT
        self._state: SystemState = SystemState.HOST_MODE

        self._lock = asyncio.Lock()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._master_fd: Optional[int] = None
        self._slave_fd: Optional[int] = None
        self._debounce_buffer: Optional[DebounceBuffer] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._is_terminating: bool = False

    @property
    def state(self) -> SystemState:
        """Current state: HOST_MODE or AGY_MODE."""
        return self._state

    @property
    def process(self) -> Optional[asyncio.subprocess.Process]:
        """Current active process instance, if any."""
        return self._process

    @property
    def pid(self) -> Optional[int]:
        """PID of the active agy process, if running."""
        return self._process.pid if self._process else None

    def is_running(self) -> bool:
        """Check if agy process is alive."""
        return self._process is not None and self._process.returncode is None

    async def spawn(
        self,
        cwd: Optional[Path] = None,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        """Spawn a single agy process inside a POSIX PTY and enter AGY_MODE."""
        async with self._lock:
            if self.is_running():
                raise RuntimeError("AGY process is already running. Use restart() or terminate() first.")

            # Ensure previous state is cleanly reset
            await self._cleanup_internal()

            target_cwd = (cwd or self.GLOBAL_CWD).resolve()
            if not target_cwd.exists():
                target_cwd.mkdir(parents=True, exist_ok=True)
            self.GLOBAL_CWD = target_cwd

            # 1. Allocate POSIX PTY
            master_fd, slave_fd = pty.openpty()
            self._master_fd = master_fd
            self._slave_fd = slave_fd

            # 2. Configure PTY window size (rows, cols)
            try:
                winsize = struct.pack(
                    "HHHH",
                    self.settings.PTY_ROWS,
                    self.settings.PTY_COLS,
                    0,
                    0,
                )
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except Exception as e:
                logger.warning(f"Failed to set TIOCSWINSZ: {e}")

            # 3. Environment configuration
            sub_env = os.environ.copy()
            sub_env["TERM"] = "xterm-256color"
            sub_env["COLUMNS"] = str(self.settings.PTY_COLS)
            sub_env["LINES"] = str(self.settings.PTY_ROWS)

            # Build command args
            cmd = [self.settings.AGY_BINARY]
            if self.settings.AGY_DEFAULT_ARGS:
                cmd.extend(self.settings.AGY_DEFAULT_ARGS)
            if extra_args:
                cmd.extend(extra_args)

            logger.info(f"Spawning agy command: {cmd} in {target_cwd}")

            # 4. Spawn child in independent process group (os.setsid)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(target_cwd),
                    env=sub_env,
                    preexec_fn=os.setsid,
                )
            except Exception as e:
                os.close(slave_fd)
                os.close(master_fd)
                self._master_fd = None
                self._slave_fd = None
                raise RuntimeError(f"Failed to spawn agy process: {e}") from e

            # Close slave end in parent process immediately
            try:
                os.close(slave_fd)
            except OSError:
                pass
            self._slave_fd = None

            # 5. Initialize debounce buffer with output callback
            self._debounce_buffer = DebounceBuffer(
                debounce_seconds=self.settings.DEBOUNCE_SECONDS,
                max_chunk_chars=self.settings.MAX_CHUNK_CHARS,
                wechat_max_len=self.settings.WECHAT_MAX_MSG_LEN,
                on_flush=self._on_cleaned_output,
            )

            self._is_terminating = False
            self._state = SystemState.AGY_MODE

            # 6. Launch background reader and process monitor tasks
            self._reader_task = asyncio.create_task(self._pty_reader_loop())
            self._monitor_task = asyncio.create_task(self._monitor_process_exit())

            logger.info(f"agy process spawned successfully with PID: {self._process.pid}")

    async def terminate(self) -> None:
        """Terminate the agy process tree via 3-layer defense and return to HOST_MODE."""
        async with self._lock:
            self._is_terminating = True
            await self._cleanup_internal()
            self._state = SystemState.HOST_MODE
            logger.info("agy process terminated. Reverted to HOST_MODE.")

    async def restart(self, extra_args: Optional[List[str]] = None) -> None:
        """Terminate existing agy process and spawn a fresh one (/new implementation)."""
        await self.terminate()
        await self.spawn(cwd=self.GLOBAL_CWD, extra_args=extra_args)

    async def send_input(self, text: str) -> None:
        """Write input text plus carriage return to the PTY master fd."""
        if not self.is_running() or self._master_fd is None:
            raise RuntimeError("Cannot send input: agy process is not active.")

        data = text if text.endswith(("\r", "\n")) else text + "\r"
        payload = data.encode("utf-8")
        try:
            await asyncio.to_thread(os.write, self._master_fd, payload)
        except OSError as e:
            logger.error(f"Error writing to PTY master: {e}")
            raise

    async def send_ctrl_c(self) -> None:
        """Send SIGINT (Ctrl+C = 0x03) to the PTY master fd."""
        if not self.is_running() or self._master_fd is None:
            raise RuntimeError("Cannot send Ctrl+C: agy process is not active.")

        try:
            await asyncio.to_thread(os.write, self._master_fd, b"\x03")
            logger.info("Sent Ctrl+C to agy PTY.")
        except OSError as e:
            logger.error(f"Error writing Ctrl+C to PTY master: {e}")
            raise

    async def _on_cleaned_output(self, text: str) -> None:
        """Forward cleaned text chunks to push callback if configured."""
        if self.push_callback:
            try:
                await self.push_callback(text)
            except Exception as e:
                logger.error(f"Failed to push cleaned terminal output: {e}")

    async def _pty_reader_loop(self) -> None:
        """Continuous background coroutine reading raw bytes from PTY master."""
        fd = self._master_fd
        if fd is None:
            return

        try:
            while not self._is_terminating:
                try:
                    data = await asyncio.to_thread(os.read, fd, 4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    if self._debounce_buffer:
                        await self._debounce_buffer.feed(text)
                except OSError as e:
                    # EIO indicates child closed all slave fds (normal termination on Linux)
                    if e.errno in (errno.EIO, errno.EBADF):
                        break
                    logger.warning(f"PTY read error: {e}")
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if self._debounce_buffer:
                await self._debounce_buffer.flush()

    async def _monitor_process_exit(self) -> None:
        """Monitor agy process termination, flush output, and transition back to HOST_MODE."""
        proc = self._process
        if proc is None:
            return

        try:
            return_code = await proc.wait()
            logger.info(f"agy process exited with code {return_code}")
        except asyncio.CancelledError:
            return

        if self._is_terminating:
            return

        async with self._lock:
            if not self._is_terminating:
                await self._cleanup_internal()
                self._state = SystemState.HOST_MODE
                if self.push_callback:
                    await self.push_callback(f"ℹ️ AGY 进程已退出 (退出码 {return_code})，系统已自动返回 HOST_MODE。")

    async def _cleanup_internal(self) -> None:
        """Execute 3-layer process destruction and close PTY file descriptors."""
        # Layer 1 & 2: Process group killpg and direct handle kill
        if self._process:
            pid = self._process.pid
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.sleep(0.1)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except (ProcessLookupError, OSError):
                pass

            try:
                self._process.kill()
            except (ProcessLookupError, OSError):
                pass

            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.5)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                pass

            self._process = None

        # Close file descriptors first so PTY read loop immediately unblocks
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self._slave_fd is not None:
            try:
                os.close(self._slave_fd)
            except OSError:
                pass
            self._slave_fd = None

        # Cancel background tasks safely
        curr = asyncio.current_task()
        for task in (self._reader_task, self._monitor_task):
            if task and not task.done() and task is not curr:
                task.cancel()

        self._reader_task = None
        self._monitor_task = None

        # Close debounce buffer
        if self._debounce_buffer:
            try:
                await self._debounce_buffer.close()
            except Exception:
                pass
            self._debounce_buffer = None

        # Layer 3: psutil orphan scavenger
        self._psutil_cleanup()

    def _psutil_cleanup(self) -> None:
        """Scan and kill any remaining orphan agy processes."""
        current_pid = os.getpid()
        parent_pid = os.getppid()
        target_name = Path(self.settings.AGY_BINARY).name

        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                p_pid = p.info["pid"]
                if p_pid in (current_pid, parent_pid):
                    continue

                p_name = p.info["name"] or ""
                cmdline = p.info["cmdline"] or []

                is_target = False
                if p_name == target_name or (target_name and p_name.startswith(target_name)):
                    is_target = True
                elif cmdline and (
                    cmdline[0] == self.settings.AGY_BINARY
                    or cmdline[0].endswith(f"/{target_name}")
                ):
                    is_target = True

                if is_target:
                    logger.warning(f"Layer 3 psutil: killing orphan agy process PID {p_pid}")
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


_global_manager: Optional[SingletonAGYManager] = None


def get_agy_manager(
    settings: Optional[Settings] = None,
    push_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> SingletonAGYManager:
    """Retrieve global singleton AGY manager."""
    global _global_manager
    if _global_manager is None:
        _global_manager = SingletonAGYManager(
            settings=settings,
            push_callback=push_callback,
        )
    return _global_manager
