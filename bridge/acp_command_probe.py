"""ACP stdio capability prober and local markdown command/skill directory scanner."""
import asyncio
import contextlib
import json
import logging
from pathlib import Path
import shlex
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError, scrub_environment

logger = logging.getLogger("wechat_agy.discovery.acp")


async def probe_acp_commands(
    resolved_bin: str,
    workspace_dir: str,
    env_passthrough: Optional[Iterable[str]] = None,
    timeout: float = 12.0,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Query ACP `initialize` + `session/new` to obtain live `available_commands_update`."""
    safe_env = scrub_environment(env_passthrough or ())
    try:
        ApprovalGuard.scan_shell_command(f"{shlex.quote(resolved_bin)} acp --stdio")
        proc = await asyncio.create_subprocess_exec(
            resolved_bin,
            "acp",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=safe_env,
            limit=1024 * 1024,
        )
    except (SandboxSecurityError, OSError, ValueError) as exc:
        logger.debug("ACP probe spawn failed for %s: %s", resolved_bin, exc)
        return [], {}

    commands: List[Dict[str, str]] = []
    meta: Dict[str, Any] = {}
    try:
        if proc.stdin is None or proc.stdout is None:
            return [], {}
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "agyWechat", "version": "1.0"},
            },
        }
        proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        sess_sent = False
        got_commands = False

        while loop.time() < deadline and not got_commands:
            rem = max(0.05, deadline - loop.time())
            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(rem, 1.5))
            except asyncio.TimeoutError:
                if sess_sent and commands:
                    break
                continue
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue

            if msg.get("id") == 1 and not sess_sent:
                meta["agent_info"] = (msg.get("result") or {}).get("agentInfo", {})
                new_req = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {"cwd": workspace_dir, "mcpServers": []},
                }
                proc.stdin.write((json.dumps(new_req) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                sess_sent = True
            elif msg.get("id") == 2:
                res = msg.get("result") or {}
                models = (res.get("models") or {}).get("availableModels") or []
                modes = (res.get("modes") or {}).get("availableModes") or []
                meta["models"] = [str(m.get("modelId") or m.get("name")) for m in models if isinstance(m, dict)]
                meta["modes"] = [str(m.get("id") or m.get("name")) for m in modes if isinstance(m, dict)]
            elif msg.get("method") == "session/update":
                upd = ((msg.get("params") or {}).get("update")) or {}
                if upd.get("sessionUpdate") == "available_commands_update":
                    for item in upd.get("availableCommands") or []:
                        if isinstance(item, dict) and item.get("name"):
                            cname = str(item["name"]).strip().lstrip("/").lower()
                            commands.append({
                                "name": cname,
                                "slash": f"/{cname}",
                                "kind": "agent_slash",
                                "exec_mode": "prompt",
                                "description": str(item.get("description") or "").strip(),
                                "source": "acp",
                            })
                    got_commands = True
    finally:
        with contextlib.suppress(OSError, ValueError):
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(OSError):
                await proc.wait()

    return commands, meta


def _extract_md_summary(md_path: Path) -> str:
    """Extract concise one-line description from command or skill markdown frontmatter."""
    try:
        lines = md_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:25]
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if s.lower().startswith("description:"):
            return s.split(":", 1)[1].strip().strip("\"'")
    for line in lines:
        s = line.strip().lstrip("#").strip()
        if s and s != "---" and ":" not in s[:15]:
            return s[:100]
    return ""


def scan_local_command_files(
    engine_id: str, workspace_dir: str, skip_skills: bool = False
) -> List[Dict[str, str]]:
    """Discover workspace & user custom markdown commands/skills if not covered by ACP."""
    specs: List[Dict[str, str]] = []
    ws = Path(workspace_dir)
    home = Path.home()
    md_dirs: List[Path] = []
    eid = engine_id.lower()
    if eid == "claude":
        md_dirs = [ws / ".claude" / "commands", home / ".claude" / "commands"]
    elif eid == "pi":
        md_dirs = [ws / ".pi" / "commands", ws / ".pi" / "prompts", home / ".pi" / "agent" / "commands"]
    elif eid == "omp":
        md_dirs = [ws / ".omp" / "commands", home / ".omp" / "commands"]

    for d in md_dirs:
        if d.is_dir():
            for md_file in sorted(d.glob("*.md")):
                cname = md_file.stem.strip().lower()
                if cname:
                    specs.append({
                        "name": cname,
                        "slash": f"/{cname}",
                        "kind": "agent_slash",
                        "exec_mode": "prompt",
                        "description": _extract_md_summary(md_file),
                        "source": "skill_dir",
                    })

    if eid == "agy" and not skip_skills:
        skills_dir = ws / ".agents" / "skills"
        if skills_dir.is_dir():
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                cname = skill_md.parent.name.strip().lower()
                if cname:
                    specs.append({
                        "name": cname,
                        "slash": f"/{cname}",
                        "kind": "agent_slash",
                        "exec_mode": "prompt",
                        "description": _extract_md_summary(skill_md),
                        "source": "skill_dir",
                    })
    return specs
