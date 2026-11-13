"""Engine CLI Subcommand, Session Config & --agent / --add-dir Handlers."""
import shlex
from typing import Any, Dict, List, Optional, Tuple

from bridge.engine_command_discovery import EngineCommandSpec
from memory.engine_session_repository import EngineSessionRepository
from router.engine_session_presenter import clean_cli_output, format_session_config_prompt
from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError

MAX_CHAT_OUTPUT_CHARS = 3000


def build_safe_subcommand_argv(spec: EngineCommandSpec, args_str: str) -> Tuple[bool, List[str], str]:
    """Validate arbitrary CLI subcommand arguments against shell injection before execution."""
    try:
        tokens = shlex.split(args_str) if args_str else []
    except ValueError as err:
        return False, [], f"参数语法错误：{err}"

    if spec.exec_mode == "subcommand_help":
        return True, [spec.name, "--help"], ""

    argv = [spec.name, *tokens]
    try:
        ApprovalGuard.scan_shell_command(" ".join(shlex.quote(x) for x in argv))
    except SandboxSecurityError as sec_err:
        return False, [], f"⛔ 安全策略拦截：{sec_err}"
    return True, argv, ""


def apply_session_config(
    session_repo: EngineSessionRepository,
    user_id: str,
    active_engine: str,
    spec: EngineCommandSpec,
    args_str: str,
    meta: Dict[str, Any],
) -> Tuple[str, str, Optional[str]]:
    """Inspect or update persisted session config (--model/--mode/--effort); return (tier, reply, engine)."""
    cfg = dict(meta.get("config") or {})
    current_val = cfg.get(spec.name) or "默认"
    options = list((meta.get("config_options") or {}).get(spec.name) or [])
    if not options and spec.name == "model":
        options = list(meta.get("models") or [])
    elif not options and spec.name == "mode":
        options = list(meta.get("modes") or [])

    if not args_str:
        return (
            f"T0_ENGINE_CONFIG:{active_engine}:{spec.name}",
            format_session_config_prompt(active_engine, spec.name, current_val, options),
            active_engine,
        )

    target_val = args_str.split()[0].strip()
    if target_val.lower() in ("default", "reset", "clear"):
        session_repo.update_session_config(user_id, spec.name, "")
        return (
            f"T0_ENGINE_CONFIG:{active_engine}:{spec.name}",
            f"✅ [{active_engine.upper()}] 已将 /{spec.name} 恢复为默认配置。",
            active_engine,
        )

    if options:
        lookup = {opt.lower(): opt for opt in options}
        if target_val.lower() not in lookup:
            return (
                f"T0_ENGINE_GUARD:{active_engine}",
                f"⛔ 参数无效：'{target_val}' 不在 /{spec.name} 实时可用列表内。\n可选：{', '.join(options)}",
                None,
            )
        target_val = lookup[target_val.lower()]

    session_repo.update_session_config(user_id, spec.name, target_val)
    return (
        f"T0_ENGINE_CONFIG:{active_engine}:{spec.name}",
        f"✅ [{active_engine.upper()}] 当前会话 /{spec.name} 已设置为：{target_val}",
        active_engine,
    )


async def handle_agent_command(
    hub: Any,
    session_repo: EngineSessionRepository,
    user_id: str,
    active_engine: str,
    args_str: str,
    meta: Dict[str, Any],
    workspace_dir: str,
) -> Tuple[str, str, Optional[str]]:
    """List available agents when bare/help, or set/reset session `--agent` override."""
    arg = args_str.strip()
    cfg = dict(meta.get("config") or {})
    current = cfg.get("agent") or "默认"

    if not arg or arg in ("list", "-h", "--help"):
        out_txt = ""
        if hub and hasattr(hub, "execute_engine_subcommand"):
            argv = ["agent", "--help"] if arg in ("-h", "--help") else ["agent"]
            rc, out, err = await hub.execute_engine_subcommand(active_engine, argv, workspace_dir=workspace_dir)
            if rc == 0:
                out_txt = clean_cli_output(out or err or "")
        body = out_txt or "当前未配置自定义子智能体（默认主智能体）。"
        return (
            f"T0_ENGINE_SUBCMD:{active_engine}:agent",
            f"【⚙️ {active_engine.upper()} · /agent】（当前会话: {current}）\n{body}\n\n👉 用法：/agent <名称> 切换，或 /agent reset 恢复默认。",
            active_engine,
        )

    target = arg.split()[0].strip()
    if target.lower() in ("default", "reset", "clear"):
        session_repo.update_session_config(user_id, "agent", "")
        return f"T0_ENGINE_CONFIG:{active_engine}:agent", f"✅ [{active_engine.upper()}] 已恢复默认 /agent。", active_engine

    session_repo.update_session_config(user_id, "agent", target)
    return (
        f"T0_ENGINE_CONFIG:{active_engine}:agent",
        f"✅ [{active_engine.upper()}] 当前会话 /agent 已设置为：{target}",
        active_engine,
    )


async def run_cli_subcommand(
    hub: Any,
    active_engine: str,
    spec: EngineCommandSpec,
    args_str: str,
    meta: Dict[str, Any],
    workspace_dir: str,
) -> Tuple[str, str, Optional[str]]:
    """Execute a CLI subcommand inside `workspace_dir` with full argument passthrough."""
    if not hub or not hasattr(hub, "execute_engine_subcommand"):
        return f"T0_ENGINE_ERR:{active_engine}", "⛔ 未挂载引擎执行中心，无法执行子命令。", None

    ok_args, argv, err_msg = build_safe_subcommand_argv(spec, args_str)
    if not ok_args:
        return f"T0_ENGINE_GUARD:{active_engine}", err_msg, None

    rc, out, err = await hub.execute_engine_subcommand(active_engine, argv, workspace_dir=workspace_dir)
    raw_text = clean_cli_output(out or err or "")
    if rc != 0 and spec.name == "models" and meta.get("models"):
        cached = "\n".join(f"• {m}" for m in meta["models"])
        return (
            f"T0_ENGINE_SUBCMD:{active_engine}:{spec.name}",
            f"【⚙️ {active_engine.upper()} · /models（会话入口探测快照）】\n{cached}",
            active_engine,
        )
    if rc != 0:
        detail = raw_text[:800] or f"exit={rc}"
        return (
            f"T0_ENGINE_SUBCMD:{active_engine}:{spec.name}",
            f"⚠️ [{active_engine.upper()}] '/{' '.join(argv)}' (exit={rc})：\n{detail}",
            active_engine,
        )
    if len(raw_text) > MAX_CHAT_OUTPUT_CHARS:
        raw_text = raw_text[:MAX_CHAT_OUTPUT_CHARS] + "\n...（输出过长已截断）"
    header = f"【⚙️ {active_engine.upper()} · /{' '.join(argv)}】\n"
    return (
        f"T0_ENGINE_SUBCMD:{active_engine}:{spec.name}",
        header + (raw_text or "（执行完成，无标准输出）"),
        active_engine,
    )


async def run_panel_slash(
    hub: Any,
    active_engine: str,
    spec: EngineCommandSpec,
    cli_flags: List[str],
    workspace_dir: str,
) -> Tuple[str, str, Optional[str]]:
    """Execute a zero-token native CLI panel slash command without consuming LLM quota."""
    if not hub or not hasattr(hub, "execute_engine_panel"):
        return f"T0_ENGINE_ERR:{active_engine}", "⛔ 未挂载引擎执行中心，无法执行面板命令。", None
    ok, txt = await hub.execute_engine_panel(
        active_engine, f"/{spec.name}", workspace_dir=workspace_dir, extra_flags=cli_flags
    )
    clean = clean_cli_output(txt)
    if not ok:
        return (
            f"T0_ENGINE_PANEL:{active_engine}:{spec.name}",
            f"⚠️ [{active_engine.upper()}] /{spec.name} 失败：{clean[:800]}",
            active_engine,
        )
    if len(clean) > MAX_CHAT_OUTPUT_CHARS:
        clean = clean[:MAX_CHAT_OUTPUT_CHARS] + "\n...（输出过长已截断）"
    return f"T0_ENGINE_PANEL:{active_engine}:{spec.name}", clean or "✅ 执行完成。", active_engine
