"""Real-time Multi-Channel Dynamic Command Discovery for CLI Engines (ACP, --help, --list-commands, Skills)."""
import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bridge.acp_command_probe import probe_acp_commands, scan_local_command_files
from bridge.command_curator import (
    classify_and_curate,
    extract_session_capabilities,
    extract_session_config_specs,
)
from bridge.tui_command_probe import probe_tui_commands
from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError, scrub_environment

logger = logging.getLogger("wechat_agy.discovery")

DAEMON_HELP_ONLY_SUBCOMMANDS = {"acp", "mic-serve"}


@dataclass
class EngineCommandSpec:
    """Normalized command descriptor mapped dynamically into a user's engine session."""

    name: str
    slash: str
    kind: str  # "agent_slash" | "cli_subcommand" | "session_config" | "engine_help"
    exec_mode: str  # "prompt" | "subcommand_readonly" | "subcommand_readonly_list" | "subcommand_help" | "session_config" | "engine_help" | "unsupported_in_chat"
    description: str = ""
    source: str = "cli_help"
    category: str = "ai_core"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "slash": self.slash,
            "kind": self.kind,
            "exec_mode": self.exec_mode,
            "description": self.description,
            "source": self.source,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EngineCommandSpec":
        name = str(data.get("name", "")).lstrip("/").strip().lower()
        return cls(
            name=name,
            slash=str(data.get("slash") or f"/{name}"),
            kind=str(data.get("kind") or "agent_slash"),
            exec_mode=str(data.get("exec_mode") or "prompt"),
            description=str(data.get("description") or ""),
            source=str(data.get("source") or "cli_help"),
            category=str(data.get("category") or "ai_core"),
        )


@dataclass
class EngineDiscoveryResult:
    """Result returned by real-time probing when entering a CLI engine."""

    engine_id: str
    available: bool
    binary_resolved: str = ""
    version: str = ""
    commands: List[EngineCommandSpec] = field(default_factory=list)
    command_map: Dict[str, EngineCommandSpec] = field(default_factory=dict)
    models: List[str] = field(default_factory=list)
    modes: List[str] = field(default_factory=list)
    config_options: Dict[str, List[str]] = field(default_factory=dict)
    filtered_commands: Dict[str, str] = field(default_factory=dict)
    session_capabilities: Dict[str, Any] = field(default_factory=dict)
    discovery_sources: List[str] = field(default_factory=list)
    error: str = ""


def resolve_binary(binary_path: str) -> Optional[str]:
    """Resolve explicit path or search standard & user-local binary directories."""
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
    timeout: float = 5.0,
    env_passthrough: Optional[Iterable[str]] = None,
) -> Tuple[int, str, str]:
    """Run a non-interactive CLI probe or read-only subcommand with scrubbed env."""
    try:
        ApprovalGuard.scan_shell_command(" ".join(shlex.quote(tok) for tok in argv))
    except SandboxSecurityError as sec_err:
        return 126, "", f"Blocked unsafe command: {sec_err}"

    safe_env = scrub_environment(env_passthrough or ())
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir,
            env=safe_env,
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
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "", f"Timed out after {timeout}s"


def _classify_cli_subcommand(name: str, _desc: str = "") -> str:
    low = name.lower()
    if low == "help":
        return "engine_help"
    if low in DAEMON_HELP_ONLY_SUBCOMMANDS:
        return "subcommand_help"
    if low == "agent":
        return "agent_command"
    return "subcommand_passthrough"


def _parse_help_output(help_text: str) -> Tuple[List[EngineCommandSpec], bool, bool]:
    """Parse subcommands and advertised slash commands from generic CLI --help output."""
    specs: List[EngineCommandSpec] = []
    supports_acp = False
    supports_list_commands = "--list-commands" in help_text
    section_mode = ""

    for raw_line in help_text.splitlines():
        stripped = raw_line.strip()
        low = stripped.lower()
        if re.match(r"^(?:available\s+subcommands|subcommands|core\s+commands|commands)\s*:\s*$", low):
            section_mode = "subcommand"
            continue
        if re.match(r"^(?:slash\s+commands|built-in\s+commands|interactive\s+commands)\s*:\s*$", low):
            section_mode = "slash"
            continue
        if not stripped:
            section_mode = ""
            continue
        if not raw_line.startswith((" ", "\t")) and stripped.endswith(":"):
            section_mode = ""
            continue

        if section_mode == "subcommand":
            m = re.match(r"^\s+([a-zA-Z0-9][a-zA-Z0-9_-]*)(?:\s{2,}(.+)|\s*)$", raw_line)
            if m:
                cmd_name = m.group(1).lower()
                desc = (m.group(2) or "").strip()
                if cmd_name == "acp":
                    supports_acp = True
                kind = "engine_help" if cmd_name == "help" else "cli_subcommand"
                specs.append(EngineCommandSpec(
                    name=cmd_name,
                    slash=f"/{cmd_name}",
                    kind=kind,
                    exec_mode=_classify_cli_subcommand(cmd_name, desc),
                    description=desc,
                    source="cli_help",
                ))
        elif section_mode == "slash":
            m = re.match(r"^\s+/([a-zA-Z0-9][a-zA-Z0-9_-]*)(?:\s{2,}(.+)|\s*)$", raw_line)
            if m:
                cmd_name = m.group(1).lower()
                desc = (m.group(2) or "").strip()
                specs.append(EngineCommandSpec(
                    name=cmd_name,
                    slash=f"/{cmd_name}",
                    kind="agent_slash",
                    exec_mode="engine_help" if cmd_name == "help" else "prompt",
                    description=desc,
                    source="cli_help",
                ))

    return specs, supports_acp, supports_list_commands


async def _probe_list_commands_flag(
    resolved_bin: str,
    workspace_dir: str,
    env_passthrough: Optional[Iterable[str]] = None,
    timeout: float = 4.0,
) -> List[EngineCommandSpec]:
    """Query `--list-commands` JSON if advertised by the CLI binary."""
    rc, stdout, _ = await run_cli_capture(
        [resolved_bin, "--list-commands"], workspace_dir, timeout=timeout, env_passthrough=env_passthrough
    )
    if rc != 0 or not stdout.strip():
        return []
    try:
        payload = json.loads(stdout.strip())
    except ValueError:
        return []
    items = payload.get("commands", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []

    out: List[EngineCommandSpec] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            name = item.strip().lstrip("/").lower()
            out.append(EngineCommandSpec(
                name=name, slash=f"/{name}", kind="agent_slash", exec_mode="prompt", source="list_commands"
            ))
        elif isinstance(item, dict) and item.get("name"):
            name = str(item["name"]).strip().lstrip("/").lower()
            kind = str(item.get("kind") or "agent_slash")
            exec_mode = str(item.get("exec_mode") or ("engine_help" if name == "help" else "prompt"))
            out.append(EngineCommandSpec(
                name=name,
                slash=f"/{name}",
                kind=kind,
                exec_mode=exec_mode,
                description=str(item.get("description") or "").strip(),
                source="list_commands",
            ))
    return out


async def discover_engine_commands(
    engine_id: str,
    binary_path: str,
    workspace_dir: str,
    env_passthrough: Optional[Iterable[str]] = None,
    timeout: float = 15.0,
) -> EngineDiscoveryResult:
    """Live-probe CLI engine binary on entry and build its dynamic session command mapping."""
    resolved = resolve_binary(binary_path)
    if not resolved:
        return EngineDiscoveryResult(
            engine_id=engine_id,
            available=False,
            error=f"未检测到引擎 '{engine_id}' 的可执行文件 '{binary_path}'。",
        )

    (_, ver_out, _), (help_rc, help_out, help_err) = await asyncio.gather(
        run_cli_capture([resolved, "--version"], workspace_dir, timeout=2.5, env_passthrough=env_passthrough),
        run_cli_capture([resolved, "--help"], workspace_dir, timeout=3.0, env_passthrough=env_passthrough),
    )
    version_str = (ver_out or "").strip().splitlines()[0][:80] if ver_out.strip() else ""
    combined_help = (help_out or "") + ("\n" + help_err if help_err else "")
    help_specs, supports_acp, supports_list_cmds = _parse_help_output(combined_help)
    flag_specs_raw, config_options = extract_session_config_specs(combined_help)
    session_caps = extract_session_capabilities(combined_help)

    merged: Dict[str, EngineCommandSpec] = {}
    sources: List[str] = []
    if help_rc == 0 or help_specs or flag_specs_raw:
        sources.append("cli_help")
        for sp in help_specs:
            merged.setdefault(sp.name, sp)
        for raw_sp in flag_specs_raw:
            sp = EngineCommandSpec.from_dict(raw_sp)
            merged.setdefault(sp.name, sp)

    models: List[str] = []
    modes: List[str] = []
    acp_cmds: List[EngineCommandSpec] = []
    tui_cmds: List[EngineCommandSpec] = []
    known_aliases: Dict[str, str] = {}
    supports_interactive_tui = "--prompt-interactive" in combined_help

    (acp_raw, acp_meta), listed_cmds, (tui_raw, tui_aliases) = await asyncio.gather(
        probe_acp_commands(
            resolved, workspace_dir, env_passthrough=env_passthrough, timeout=min(timeout, 18.0)
        )
        if supports_acp
        else asyncio.sleep(0, result=([], {})),
        _probe_list_commands_flag(
            resolved, workspace_dir, env_passthrough=env_passthrough, timeout=min(timeout, 4.0)
        )
        if supports_list_cmds
        else asyncio.sleep(0, result=[]),
        probe_tui_commands(
            resolved, workspace_dir, env_passthrough=env_passthrough, timeout=min(timeout, 12.0)
        )
        if supports_interactive_tui
        else asyncio.sleep(0, result=([], {})),
    )

    if supports_acp:
        acp_cmds = [EngineCommandSpec.from_dict(item) for item in acp_raw]
        if acp_cmds:
            sources.append("acp")
            for sp in acp_cmds:
                merged.setdefault(sp.name, sp)
        info = acp_meta.get("agent_info") or {}
        if isinstance(info, dict) and info.get("version"):
            version_str = f"{info.get('name', engine_id)} {info['version']}".strip()
        models = list(acp_meta.get("models") or [])
        modes = list(acp_meta.get("modes") or [])
        if models and "model" in merged:
            config_options["model"] = models
        if modes and "mode" in merged:
            config_options["mode"] = modes

    if listed_cmds:
        sources.append("list_commands")
        for sp in listed_cmds:
            merged[sp.name] = sp

    if tui_raw:
        sources.append("tui_menu")
        known_aliases.update(tui_aliases)
        tui_cmds = [EngineCommandSpec.from_dict(item) for item in tui_raw]
        for sp in tui_cmds:
            if sp.name not in merged:
                merged[sp.name] = sp
            elif not merged[sp.name].description and sp.description:
                merged[sp.name].description = sp.description

    local_raw = scan_local_command_files(engine_id, workspace_dir, skip_skills=bool(acp_cmds))
    local_cmds = [EngineCommandSpec.from_dict(item) for item in local_raw]
    if local_cmds:
        sources.append("skill_dir")
        for sp in local_cmds:
            merged.setdefault(sp.name, sp)

    merged.setdefault(
        "clear",
        EngineCommandSpec(
            name="clear", slash="/clear", kind="session_reset", exec_mode="session_reset",
            description="Clear current conversation history and start a fresh thread", source="session", category="session",
        ),
    )
    merged.setdefault(
        "exit",
        EngineCommandSpec(
            name="exit", slash="/exit", kind="engine_exit", exec_mode="engine_exit",
            description="Exit active engine and return to Non-Engine workspace state", source="session", category="session",
        ),
    )
    merged.setdefault(
        "help",
        EngineCommandSpec(
            name="help", slash="/help", kind="engine_help", exec_mode="engine_help",
            description="Show dynamic mapped commands for this engine session", source="meta", category="help",
        ),
    )

    curated_raw, filtered_map = classify_and_curate(
        [sp.to_dict() for sp in merged.values()], known_aliases=known_aliases
    )
    for alias_name, canonical_name in known_aliases.items():
        filtered_map.setdefault(alias_name, f"alias_excluded:{canonical_name}")
    ordered = sorted([EngineCommandSpec.from_dict(item) for item in curated_raw], key=lambda c: c.name)
    return EngineDiscoveryResult(
        engine_id=engine_id,
        available=True,
        binary_resolved=resolved,
        version=version_str,
        commands=ordered,
        command_map={c.name: c for c in ordered},
        models=models,
        modes=modes,
        config_options=config_options,
        filtered_commands=filtered_map,
        session_capabilities=session_caps,
        discovery_sources=sources or ["meta"],
    )
