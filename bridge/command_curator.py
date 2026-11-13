"""Deterministic WeChat Command Curator: full CLI command parity with zero duplicate aliases."""
import re
from typing import Any, Dict, List, Optional, Tuple

DAEMON_HELP_ONLY_COMMANDS = {"acp", "mic-serve"}

TUI_OP_COMMANDS = {
    "diff", "codesearch", "context", "copy", "open", "artifact",
    "tasks", "rename", "fork", "resume", "rewind", "projects",
    "feedback", "logout", "keybindings", "statusline", "title",
    "record", "voice", "btw",
}

SESSION_CMD_NAMES = {"clear", "exit", "add-dir", "fork", "resume", "rewind", "rename", "btw"}
CONFIG_CMD_NAMES = {
    "model", "mode", "effort", "models", "agent", "mcp", "plugin",
    "config", "hooks", "permissions", "skills", "usage", "credits",
    "install", "update", "remote-control", "changelog",
}
WORKSPACE_TOOL_NAMES = {
    "diff", "codesearch", "context", "copy", "open", "artifact",
    "tasks", "projects", "feedback", "keybindings", "statusline",
    "title", "record", "voice", "logout", "acp", "mic-serve",
}

SESSION_CONFIG_FLAGS: Dict[str, str] = {
    "model": "View or set active model for this engine session",
    "mode": "View or set execution mode for this engine session",
    "effort": "View or set reasoning effort level for this engine session",
}

ALIAS_DESC_RE = re.compile(r"^alias\s+for\s+([a-z0-9_-]+)\b", re.IGNORECASE)
PAREN_OPTIONS_RE = re.compile(r"\(([^()]{3,80})\)\s*$")


def extract_session_capabilities(help_text: str) -> Dict[str, Any]:
    """Detect native CLI session continuation and structured JSON output flags from --help."""
    supports_json = bool(re.search(r"--output-format\b", help_text) and re.search(r"\bjson\b", help_text))
    conv_flag = ""
    if re.search(r"--conversation\b", help_text):
        conv_flag = "--conversation"
    elif re.search(r"--resume\b", help_text):
        conv_flag = "--resume"
    return {
        "supports_json_output": supports_json,
        "conversation_flag": conv_flag,
        "supports_native_session": bool(supports_json and conv_flag),
    }


def extract_session_config_specs(help_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    """Dynamically extract supported session config commands (--model, --mode, --effort) from CLI --help."""
    specs: List[Dict[str, Any]] = []
    options_map: Dict[str, List[str]] = {}
    seen: set[str] = set()

    for raw_line in help_text.splitlines():
        m = re.match(
            r"^\s+(?:-[a-zA-Z],\s*)?--([a-zA-Z0-9_-]+)(?:[=\s]+<[^>]+>|\s+\[[A-Z0-9_]+\]|\s+[A-Z0-9_]{2,})?\s{2,}(.+)$",
            raw_line,
        )
        if not m:
            continue
        flag = m.group(1).strip().lower()
        desc = m.group(2).strip()
        if flag not in SESSION_CONFIG_FLAGS or flag in seen:
            continue
        seen.add(flag)
        opts_match = PAREN_OPTIONS_RE.search(desc)
        if opts_match:
            raw_opts = opts_match.group(1)
            sep = "|" if "|" in raw_opts else ","
            parsed = [x.strip().lower() for x in raw_opts.split(sep) if x.strip()]
            if parsed:
                options_map[flag] = parsed
        specs.append({
            "name": flag,
            "slash": f"/{flag}",
            "kind": "session_config",
            "exec_mode": "session_config",
            "category": "config",
            "description": desc or SESSION_CONFIG_FLAGS[flag],
            "source": "cli_flag",
        })
    return specs, options_map


def normalize_command_exec_mode(name: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize kind and exec_mode so every discovered CLI command is executable in WeChat."""
    updated = dict(item)
    low = name.lower()
    if low == "clear":
        updated["kind"] = "session_reset"
        updated["exec_mode"] = "session_reset"
    elif low == "exit":
        updated["kind"] = "engine_exit"
        updated["exec_mode"] = "engine_exit"
    elif low == "help":
        updated["kind"] = "engine_help"
        updated["exec_mode"] = "engine_help"
    elif low == "add-dir":
        updated["kind"] = "session_config"
        updated["exec_mode"] = "session_add_dir"
    elif low == "agent":
        updated["kind"] = "cli_subcommand"
        updated["exec_mode"] = "agent_command"
    elif low in {"usage", "credits", "config", "hooks", "permissions", "skills"} and updated.get("exec_mode", "prompt") == "prompt":
        updated["kind"] = "cli_panel"
        updated["exec_mode"] = "panel_slash"
    elif low in DAEMON_HELP_ONLY_COMMANDS:
        updated["kind"] = "cli_subcommand"
        updated["exec_mode"] = "subcommand_help"
    elif low in TUI_OP_COMMANDS:
        updated["kind"] = "workspace_op"
        updated["exec_mode"] = "tui_op"
    return updated


def _resolve_category(name: str, exec_mode: str) -> str:
    if name == "help" or exec_mode == "engine_help":
        return "help"
    if name in SESSION_CMD_NAMES or exec_mode in ("session_reset", "engine_exit", "session_add_dir"):
        return "session"
    if name in CONFIG_CMD_NAMES or exec_mode in ("session_config", "subcommand_passthrough", "agent_command", "panel_slash"):
        return "config"
    if name in WORKSPACE_TOOL_NAMES or exec_mode in ("tui_op", "subcommand_help"):
        return "tools"
    return "ai_core"


def classify_and_curate(
    raw_specs: List[Dict[str, Any]],
    known_aliases: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Expose full CLI command set while enforcing a single canonical copy per command (no aliases)."""
    alias_lookup = {str(k).lower(): str(v).lower() for k, v in (known_aliases or {}).items()}
    by_name: Dict[str, Dict[str, Any]] = {}
    for item in raw_specs:
        name = str(item.get("name") or "").lstrip("/").strip().lower()
        if name:
            by_name[name] = normalize_command_exec_mode(name, item)

    curated: List[Dict[str, Any]] = []
    deduped_aliases: Dict[str, str] = {}

    for name, item in by_name.items():
        desc = str(item.get("description") or "").strip()

        if name in alias_lookup and alias_lookup[name] in by_name:
            deduped_aliases[name] = f"alias_excluded:{alias_lookup[name]}"
            continue

        alias_match = ALIAS_DESC_RE.match(desc)
        if alias_match:
            target = alias_match.group(1).lower()
            if target in by_name:
                deduped_aliases[name] = f"alias_excluded:{target}"
                continue

        if name.endswith("s") and len(name) > 2:
            singular = name[:-1]
            if singular in by_name:
                s_desc = str(by_name[singular].get("description") or "").strip().lower()
                if singular in ("agent", "plugin") or (s_desc and s_desc == desc.lower()):
                    deduped_aliases[name] = f"alias_excluded:{singular}"
                    continue

        enriched = dict(item)
        enriched["name"] = name
        enriched["slash"] = f"/{name}"
        enriched["category"] = _resolve_category(name, str(enriched.get("exec_mode") or "prompt"))
        curated.append(enriched)

    return curated, deduped_aliases
