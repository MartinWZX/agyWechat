"""Dynamic Interactive TUI Slash-Menu Prober (PTY + pyte) for CLI Engines."""
import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import select
import shlex
import struct
import subprocess
import termios
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pyte

from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError, scrub_environment

logger = logging.getLogger("wechat_agy.discovery.tui")

TUI_CMD_LINE_RE = re.compile(r"^\s*[> ]?\s*/([a-z0-9][a-z0-9_-]*)(?:\s*:\s*|\s+)(.*)$")
PAREN_ALIAS_RE = re.compile(r"^\(([a-z0-9_-]{2,24})\)\s*(.*)$", re.IGNORECASE)


def _harvest_tui_rows(
    screen: pyte.Screen,
    commands: Dict[str, str],
    aliases_seen: Dict[str, str],
) -> int:
    """Parse `/command [desc]` items currently visible on the pyte virtual screen."""
    added = 0
    for raw_line in screen.display:
        match = TUI_CMD_LINE_RE.match(raw_line)
        if not match:
            continue
        name = match.group(1).strip().lower()
        # Skip filesystem paths like /tmp/... or /home/... rendered in workspace banners
        if "/" in raw_line.split(f"/{name}", 1)[-1][:3] or name in ("tmp", "home", "usr", "var", "etc"):
            continue
        desc = match.group(2).strip()
        alias_match = PAREN_ALIAS_RE.match(desc)
        if alias_match:
            alias_name = alias_match.group(1).strip().lower()
            desc = alias_match.group(2).strip()
            if alias_name != name:
                aliases_seen[alias_name] = name
        prev = commands.get(name, "")
        if name not in commands:
            added += 1
        if len(desc) >= len(prev):
            commands[name] = desc
    return added


def _sync_probe_tui_slash(
    resolved_bin: str,
    workspace_dir: str,
    env_passthrough: Optional[Iterable[str]],
    timeout: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Spawn CLI briefly inside a virtual PTY, open `/`, and capture all interactive commands."""
    try:
        ApprovalGuard.scan_shell_command(shlex.quote(resolved_bin))
    except SandboxSecurityError as sec_err:
        logger.debug("TUI probe blocked for %s: %s", resolved_bin, sec_err)
        return [], {}

    safe_env = scrub_environment(env_passthrough or ())
    safe_env["TERM"] = "xterm-256color"
    safe_env["LINES"] = "50"
    safe_env["COLUMNS"] = "140"

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 140, 0, 0))
    proc: Optional[subprocess.Popen[bytes]] = None
    commands: Dict[str, str] = {}
    aliases_seen: Dict[str, str] = {}

    try:
        proc = subprocess.Popen(
            [resolved_bin],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workspace_dir,
            env=safe_env,
            close_fds=True,
        )
    except OSError as exc:
        logger.debug("TUI probe spawn failed for %s: %s", resolved_bin, exc)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        return [], {}
    finally:
        with contextlib.suppress(OSError):
            os.close(slave_fd)

    screen = pyte.Screen(140, 50)
    stream = pyte.ByteStream(screen)
    deadline = time.monotonic() + timeout

    def _pump(max_wait: float) -> bool:
        end = min(deadline, time.monotonic() + max_wait)
        got_bytes = False
        while time.monotonic() < end:
            if proc is not None and proc.poll() is not None:
                with contextlib.suppress(OSError):
                    chunk = os.read(master_fd, 16384)
                    if chunk:
                        stream.feed(chunk)
                        got_bytes = True
                return got_bytes
            rem = max(0.01, min(0.08, end - time.monotonic()))
            ready, _, _ = select.select([master_fd], [], [], rem)
            if not ready:
                if got_bytes:
                    return True
                continue
            try:
                chunk = os.read(master_fd, 16384)
            except OSError:
                return got_bytes
            if not chunk:
                return got_bytes
            stream.feed(chunk)
            got_bytes = True
        return got_bytes

    def _screen_text() -> str:
        return "\n".join(screen.display)

    try:
        # 1. Wait for workspace trust confirmation or main input prompt (up to 5.5s)
        trust_handled = False
        prompt_ready = False
        boot_deadline = min(deadline, time.monotonic() + 5.5)
        while time.monotonic() < boot_deadline:
            _pump(0.12)
            if proc.poll() is not None:
                return [], {}
            txt = _screen_text()
            if "trust this folder" in txt.lower() and not trust_handled:
                os.write(master_fd, b"\r")
                trust_handled = True
                _pump(0.4)
                continue
            if "for shortcuts" in txt.lower() or re.search(r"^\s*>\s*$", txt, re.MULTILINE):
                prompt_ready = True
                break

        if not prompt_ready or proc.poll() is not None:
            return [], {}

        # 2. Open `/` autocomplete popup and harvest all scrolled items
        os.write(master_fd, b"/")
        menu_deadline = min(deadline, time.monotonic() + 1.2)
        while time.monotonic() < menu_deadline:
            _pump(0.08)
            if _harvest_tui_rows(screen, commands, aliases_seen) > 0:
                break

        if not commands:
            return [], {}

        stale_steps = 0
        for _ in range(55):
            if time.monotonic() >= deadline or proc.poll() is not None:
                break
            os.write(master_fd, b"\x1b[B")
            _pump(0.06)
            added = _harvest_tui_rows(screen, commands, aliases_seen)
            stale_steps = 0 if added > 0 else stale_steps + 1
            if stale_steps >= 10:
                break
    except OSError as exc:
        logger.debug("TUI PTY read ended: %s", exc)
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                os.write(master_fd, b"\x03\x03")
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=1.5)
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
        with contextlib.suppress(OSError):
            os.close(master_fd)

    specs: List[Dict[str, Any]] = [
        {
            "name": name,
            "slash": f"/{name}",
            "kind": "agent_slash",
            "exec_mode": "prompt",
            "description": desc,
            "source": "tui",
        }
        for name, desc in sorted(commands.items())
    ]
    return specs, aliases_seen


async def probe_tui_commands(
    resolved_bin: str,
    workspace_dir: str,
    env_passthrough: Optional[Iterable[str]] = None,
    timeout: float = 8.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Asynchronously probe CLI interactive TUI `/` menu inside a virtual PTY."""
    return await asyncio.to_thread(
        _sync_probe_tui_slash, resolved_bin, workspace_dir, env_passthrough, timeout
    )
