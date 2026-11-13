"""Two-State Engine Session FSM & Dynamic Command Mapping Router."""
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from bridge.engine_command_discovery import EngineCommandSpec, EngineDiscoveryResult
from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
from router.engine_session_presenter import (
    NON_ENGINE_HELP_TEXT,
    format_engines_summary,
    format_entry_summary,
    format_in_engine_help,
    format_unknown_engine_command_reply,
)
from router.engine_subcommand_handler import apply_session_config, handle_agent_command, run_cli_subcommand, run_panel_slash
from router.engine_tui_ops import execute_tui_op
from router.workspace_navigator import WorkspaceNavigator
from skills.scripts.hallucination_audit import run_audit
from skills.scripts.system_status import run_status
from skills.scripts.wind_control_dashboard import run_dashboard

logger = logging.getLogger("wechat_agy.engine_router")

ENGINE_ENTRY_RE = re.compile(r"^/(agy|pi|claude|omp)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


@dataclass
class RouteDecision:
    """Routing outcome produced by DeterministicRouter / EngineSessionRouter."""

    handled: bool
    reply: str = ""
    tier: str = "T3_COGNITIVE"
    preferred_engine: Optional[str] = None
    effective_query: str = ""
    base_query: str = ""
    strict_engine_pin: bool = False
    is_engine_slash: bool = False
    coding_mode: bool = False
    workspace_dir: str = ""
    conversation_id: str = ""
    session_started_at: float = 0.0
    supports_native_session: bool = False
    cli_flags: List[str] = field(default_factory=list)


def _resolve_default_workspace(
    hub: Any = None, config: Optional[Dict[str, Any]] = None, settings: Any = None
) -> str:
    sys_cfg = (config or {}).get("system") or {}
    if sys_cfg.get("workspace_dir"):
        return str(sys_cfg["workspace_dir"])
    hub_ws = getattr(hub, "workspace_dir", None)
    if isinstance(hub_ws, str) and hub_ws.strip():
        return hub_ws
    settings_ws = getattr(settings, "WORKSPACE_ROOT", None)
    if settings_ws is not None and not callable(settings_ws):
        ws_str = str(settings_ws).strip()
        if ws_str and not ws_str.startswith("<MagicMock"):
            return ws_str
    return "."


class EngineSessionRouter:
    """Enforces Non-Engine read-only default state and Inside-Engine dynamic command mapping."""

    def __init__(
        self,
        store: CoreStateStore,
        hub: Any = None,
        config: Optional[Dict[str, Any]] = None,
        settings: Any = None,
    ):
        self.store = store
        self.session_repo = EngineSessionRepository(store)
        self.hub = hub
        sys_cfg = (config or {}).get("system") or {}
        default_ws = _resolve_default_workspace(hub=hub, config=config, settings=settings)
        self.workspace_nav = WorkspaceNavigator(
            self.session_repo,
            default_workspace=default_ws,
            allowed_roots=sys_cfg.get("allowed_workspace_roots"),
        )

    def get_session_state(self, user_id: str) -> Dict[str, Any]:
        return self.session_repo.get_session_state(user_id)

    def record_coding_turn(self, user_id: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        return self.session_repo.update_conversation_state(user_id, conversation_id=conversation_id)

    def _format_engines_summary(self) -> str:
        if not self.hub or not hasattr(self.hub, "list_configured_engines"):
            return "已配置引擎：agy, pi, claude, omp（发送 /<引擎名称> 进入并实时检测）"
        return format_engines_summary(self.hub.list_configured_engines())

    def match_non_engine_view_command(
        self, text: str, rsi_board_fn: Optional[Callable[[], str]] = None, user_id: str = "default"
    ) -> Tuple[bool, str]:
        """Execute read-only view and workspace navigation commands in Non-Engine default state."""
        t = text.strip()
        parts = t.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        db_path = self.store.db_path

        if cmd == "/pwd" and not arg:
            return True, self.workspace_nav.format_pwd(user_id)
        if cmd == "/ls":
            return True, self.workspace_nav.format_ls(user_id, arg)
        if cmd == "/workspace":
            return True, self.workspace_nav.switch_workspace(user_id, arg)
        if cmd == "/status" and not arg:
            eng_hint = (
                f"当前状态: 非引擎默认状态 (仅查看与目录选择权限)\n"
                f"当前工作目录: {self.workspace_nav.get_workspace(user_id)}"
            )
            if self.hub and hasattr(self.hub, "list_configured_engines"):
                ready = [r["engine_id"] for r in self.hub.list_configured_engines() if r["installed"]]
                eng_hint += f"\n已就绪引擎: {', '.join(ready) if ready else '无'}"
            return True, f"Google agy 微信智能体运行看板\n{run_status()}\n{eng_hint}"
        if t.lower() == "/security":
            return True, f"五维防御看板\n{run_dashboard(db_path=db_path)}"
        if t.lower() == "/audit":
            return True, run_audit(db_path=db_path)
        if t.lower() == "/rules":
            return True, rsi_board_fn() if rsi_board_fn else "请通过网关主路由查看 RSI 演进看板。"
        if t.lower() == "/engines":
            return True, self._format_engines_summary()
        if t.lower() == "/help":
            return True, NON_ENGINE_HELP_TEXT
        return False, ""

    @staticmethod
    def build_cli_flags(meta: Dict[str, Any], skip_conversation: bool = False) -> List[str]:
        """Build CLI flag overrides (--model/--mode/--effort/--agent/--add-dir) and native session resume."""
        cfg = meta.get("config") or {}
        flags: List[str] = []
        if isinstance(cfg, dict):
            for key in ("model", "mode", "effort", "agent"):
                val = str(cfg.get(key) or "").strip()
                if val and re.match(r"^[a-zA-Z0-9._:/-]+$", val):
                    flags.extend([f"--{key}", val])
            for extra_dir in cfg.get("add_dirs") or []:
                d_str = str(extra_dir).strip()
                if d_str and "\n" not in d_str and "\x00" not in d_str:
                    flags.extend(["--add-dir", d_str])

        caps = meta.get("session_capabilities") or {}
        if isinstance(caps, dict):
            if caps.get("supports_json_output"):
                flags.extend(["--output-format", "json"])
            conv_flag = str(caps.get("conversation_flag") or "").strip()
            conv_id = str(meta.get("conversation_id") or "").strip()
            if (
                not skip_conversation
                and conv_flag in ("--conversation", "--resume")
                and conv_id
                and re.match(r"^[a-zA-Z0-9._-]+$", conv_id)
            ):
                flags.extend([conv_flag, conv_id])
        return flags

    async def enter_engine(self, user_id: str, engine_id: str) -> RouteDecision:
        """Probe engine capabilities in user's selected workspace and enter engine session."""
        if not self.hub or not hasattr(self.hub, "inspect_engine"):
            return RouteDecision(True, f"⛔ 当前未挂载引擎探测中心，无法进入 [{engine_id}]。", tier="T0_ENGINE_UNAVAILABLE")

        if hasattr(self.hub, "governor"):
            avail, reason = self.hub.governor.check_engine_availability(engine_id, is_p0_urgent=True)
            if not avail and "LOCKDOWN" in str(reason).upper():
                return RouteDecision(True, f"⛔ 引擎 '{engine_id}' 处于安全熔断锁定状态（{reason}），暂时无法进入。", tier="T0_ENGINE_UNAVAILABLE")

        ws = self.workspace_nav.get_workspace(user_id)
        discovery: EngineDiscoveryResult = await self.hub.inspect_engine(engine_id, workspace_dir=ws)
        if not discovery.available:
            ready = [f"/{r['engine_id']}" for r in self.hub.list_configured_engines() if r.get("installed")]
            ready_hint = ", ".join(ready) if ready else "无已安装引擎"
            return RouteDecision(
                True,
                f"⛔ 无法进入引擎 '{engine_id}'：{discovery.error}\n当前主机已安装就绪引擎：{ready_hint}。",
                tier="T0_ENGINE_UNAVAILABLE",
            )

        meta = {
            "version": discovery.version,
            "sources": discovery.discovery_sources,
            "models": discovery.models,
            "modes": discovery.modes,
            "config_options": discovery.config_options,
            "filtered_commands": discovery.filtered_commands,
            "session_capabilities": discovery.session_capabilities,
            "workspace_dir": ws,
            "binary": discovery.binary_resolved,
            "config": {},
        }
        cmd_dicts = [c.to_dict() for c in discovery.commands]
        self.session_repo.enter_engine(user_id, engine_id, cmd_dicts, meta)
        return RouteDecision(
            True,
            format_entry_summary(discovery, workspace_dir=ws),
            tier="T0_ENGINE_ENTER",
            preferred_engine=engine_id,
            workspace_dir=ws,
        )

    async def route_non_engine(
        self, user_id: str, text: str, rsi_board_fn: Optional[Callable[[], str]] = None
    ) -> RouteDecision:
        """Handle routing when user is in Non-Engine default state."""
        t = text.strip()
        view_hit, view_reply = self.match_non_engine_view_command(
            t, rsi_board_fn=rsi_board_fn, user_id=user_id
        )
        if view_hit:
            return RouteDecision(True, view_reply, tier="T0_COMMAND", effective_query=t)

        m_eng = ENGINE_ENTRY_RE.match(t)
        if m_eng:
            target = m_eng.group(1).lower()
            remainder = (m_eng.group(2) or "").strip()
            if remainder:
                return RouteDecision(
                    True,
                    f"⛔ 当前处于【非引擎默认状态】，禁止直接执行引擎业务指令。\n👉 请先发送纯命令 /{target} 进入引擎并同步命令映射后，再执行任务。",
                    tier="T0_STATE_GUARD",
                )
            return await self.enter_engine(user_id, target)

        if t.lower() == "/exit":
            return RouteDecision(
                True,
                f"当前已在【非引擎默认状态】，工作目录：`{self.workspace_nav.get_workspace(user_id)}`。\n"
                "可发送 /pwd、/ls、/workspace <路径> 调整目录，或发送 /<引擎名> 进入引擎。",
                tier="T0_COMMAND",
            )

        return RouteDecision(
            True,
            "⛔ 当前处于【非引擎默认状态】，仅允许最小限度的查看与工作目录命令（/workspace、/pwd、/ls、/status、/engines、/security、/audit、/rules、/help），禁止执行具体引擎的业务指令。\n"
            "👉 请先使用 /workspace <路径> 确认工作目录，再发送 /agy、/pi、/claude 或 /omp 进入指定引擎。",
            tier="T0_STATE_GUARD",
        )

    def _reject_unknown_command(
        self, active_engine: str, cmd_name: str, commands: List[Dict[str, Any]]
    ) -> RouteDecision:
        msg = format_unknown_engine_command_reply(active_engine, cmd_name, commands)
        return RouteDecision(True, msg, tier=f"T0_ENGINE_GUARD:{active_engine}")

    async def route_in_engine(
        self, user_id: str, text: str, state: Dict[str, Any]
    ) -> Optional[RouteDecision]:
        """Handle /exit, /clear, engine-switch blocking, and full dynamic engine /commands."""
        t = text.strip()
        active_engine = state["active_engine"]
        commands = state["commands"]
        command_map = state["command_map"]
        meta = state["meta"]
        ws = state.get("workspace_dir") or self.workspace_nav.get_workspace(user_id)

        if t.lower() == "/exit":
            exited = self.session_repo.exit_engine(user_id) or active_engine
            return RouteDecision(
                True,
                f"✅ 已退出 [{exited.upper()}] 引擎，回到【非引擎默认状态】。\n"
                f"📂 工作目录保持在 `{ws}`，可继续发送 /workspace <路径> 切换目录或 /<引擎名称> 进入其他引擎。",
                tier="T0_ENGINE_EXIT",
                workspace_dir=ws,
            )

        m_eng = ENGINE_ENTRY_RE.match(t)
        if m_eng:
            target = m_eng.group(1).lower()
            if target == active_engine and not (m_eng.group(2) or "").strip():
                return RouteDecision(
                    True,
                    f"ℹ️ 当前已在 [{active_engine.upper()}] 引擎会话中（工作目录：`{ws}`，已同步 {len(commands)} 个命令，发送 /help 查看）。如需退出或切换工作目录，请先发送 /exit。",
                    tier="T0_STATE_GUARD",
                )
            return RouteDecision(
                True,
                f"⛔ 当前正处于 [{active_engine.upper()}] 引擎会话中，禁止直接切换至 [{target.upper()}]。\n👉 请先发送 /exit 退出当前引擎，回到默认状态后再进入目标引擎。",
                tier="T0_STATE_GUARD",
            )

        if not t.startswith("/"):
            return None

        parts = t[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        args_str = parts[1].strip() if len(parts) > 1 else ""

        spec_dict = command_map.get(cmd_name)
        if not spec_dict:
            return self._reject_unknown_command(active_engine, cmd_name, commands)

        spec = EngineCommandSpec.from_dict(spec_dict)
        if spec.exec_mode == "engine_help":
            return RouteDecision(
                True,
                format_in_engine_help(active_engine, commands, meta, workspace_dir=ws),
                tier=f"T0_ENGINE_HELP:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        if spec.exec_mode == "session_reset":
            self.session_repo.update_conversation_state(user_id, reset=True)
            return RouteDecision(
                True,
                f"🆕 [{active_engine.upper()}] 已开启全新会话（原生 conversation_id 与本地历史窗口已清空）。\n"
                f"📂 工作目录仍保持在 `{ws}`（模型与会话配置保留）。",
                tier=f"T0_ENGINE_NEW_SESSION:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        if spec.exec_mode == "session_config":
            tier, msg, pref = apply_session_config(self.session_repo, user_id, active_engine, spec, args_str, meta)
            return RouteDecision(True, msg, tier=tier, preferred_engine=pref, workspace_dir=ws)

        if spec.exec_mode == "session_add_dir":
            op_res = await execute_tui_op(
                "add-dir", args_str, user_id, active_engine, ws, meta, self.store, self.session_repo, self.workspace_nav
            )
            return RouteDecision(
                True,
                op_res.get("reply", ""),
                tier=f"T0_ENGINE_ADD_DIR:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        if spec.exec_mode == "agent_command":
            tier, msg, pref = await handle_agent_command(
                self.hub, self.session_repo, user_id, active_engine, args_str, meta, ws
            )
            return RouteDecision(True, msg, tier=tier, preferred_engine=pref, workspace_dir=ws)

        if spec.exec_mode in ("subcommand_passthrough", "subcommand_help", "subcommand_readonly", "subcommand_readonly_list"):
            tier, msg, pref = await run_cli_subcommand(self.hub, active_engine, spec, args_str, meta, ws)
            return RouteDecision(True, msg, tier=tier, preferred_engine=pref, workspace_dir=ws)

        if spec.exec_mode == "tui_op":
            op_res = await execute_tui_op(
                spec.name, args_str, user_id, active_engine, ws, meta, self.store, self.session_repo, self.workspace_nav
            )
            if op_res.get("handled", True):
                return RouteDecision(
                    True,
                    op_res.get("reply", ""),
                    tier=f"T0_ENGINE_TUI_OP:{active_engine}:{spec.name}",
                    preferred_engine=active_engine,
                    workspace_dir=ws,
                )
            caps = meta.get("session_capabilities") or {}
            return RouteDecision(
                handled=False,
                tier=f"T3_ENGINE_BTW:{active_engine}",
                preferred_engine=active_engine,
                effective_query=str(op_res.get("effective_query") or args_str),
                strict_engine_pin=True,
                is_engine_slash=True,
                coding_mode=True,
                workspace_dir=ws,
                supports_native_session=bool(caps.get("supports_native_session")),
                cli_flags=self.build_cli_flags(meta, skip_conversation=bool(op_res.get("skip_conv"))),
            )

        if spec.exec_mode == "panel_slash":
            flags = self.build_cli_flags(meta, skip_conversation=True)
            tier, msg, pref = await run_panel_slash(self.hub, active_engine, spec, flags, ws)
            return RouteDecision(True, msg, tier=tier, preferred_engine=pref, workspace_dir=ws)

        caps = meta.get("session_capabilities") or {}
        full_cmd = f"/{spec.name} {args_str}".strip() if args_str else f"/{spec.name}"
        return RouteDecision(
            handled=False,
            tier=f"T3_ENGINE_SLASH:{active_engine}:{spec.name}",
            preferred_engine=active_engine,
            effective_query=full_cmd,
            strict_engine_pin=True,
            is_engine_slash=True,
            coding_mode=True,
            workspace_dir=ws,
            conversation_id=str(meta.get("conversation_id") or ""),
            session_started_at=float(meta.get("session_started_at") or 0.0),
            supports_native_session=bool(caps.get("supports_native_session")),
            cli_flags=self.build_cli_flags(meta),
        )
