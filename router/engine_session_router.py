"""Pure Two-State Engine Session FSM Router."""
import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from config import resolve_user_workspaces, resolve_workspace_root
from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
from router.engine_command_handlers import INTERNAL_ENGINE_COMMANDS, dispatch_internal_engine_command
from router.engine_session_presenter import (
    NON_ENGINE_HELP_TEXT,
    format_engines_summary,
    format_entry_summary,
    format_in_engine_help,
)
from router.workspace_navigator import WorkspaceNavigator

logger = logging.getLogger("wechat_agy.engine_router")

ENGINE_ENTRY_RE = re.compile(r"^/(agy|pi|claude|omp)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


@dataclass
class RouteDecision:
    """Routing outcome produced by DeterministicRouter / EngineSessionRouter."""

    handled: bool
    reply: str = ""
    tier: str = "T3_ENGINE_PASSTHROUGH"
    preferred_engine: Optional[str] = None
    effective_query: str = ""
    base_query: str = ""
    strict_engine_pin: bool = False
    is_engine_slash: bool = False
    workspace_dir: str = ""
    conversation_id: str = ""
    session_started_at: float = 0.0
    supports_native_session: bool = False
    cli_flags: List[str] = field(default_factory=list)


def _resolve_default_workspace(
    hub: Any = None, config: Optional[Dict[str, Any]] = None, settings: Any = None
) -> str:
    if settings is not None and "WORKSPACE_ROOT" in getattr(settings, "model_fields_set", set()):
        return resolve_workspace_root(settings=settings, yaml_config=config)
    sys_cfg = (config or {}).get("system") or {}
    if isinstance(sys_cfg, dict) and sys_cfg.get("workspace_dir"):
        return str(Path(str(sys_cfg["workspace_dir"])).expanduser().resolve())
    hub_ws = getattr(hub, "workspace_dir", None)
    if isinstance(hub_ws, str) and hub_ws.strip():
        return hub_ws
    return resolve_workspace_root(settings=settings, yaml_config=config)


class EngineSessionRouter:
    """Enforces Non-Engine command gateway state and 100% native Inside-Engine passthrough."""

    def __init__(
        self,
        store: CoreStateStore,
        hub: Any = None,
        config: Optional[Dict[str, Any]] = None,
        settings: Any = None,
        health_monitor: Any = None,
    ):
        self.store = store
        self.session_repo = EngineSessionRepository(store)
        self.hub = hub
        self.health_monitor = health_monitor
        sys_cfg = (config or {}).get("system") or {}
        default_ws = _resolve_default_workspace(hub=hub, config=config, settings=settings)
        user_ws_map = resolve_user_workspaces(settings=settings, yaml_config=config)
        self.workspace_nav = WorkspaceNavigator(
            self.session_repo,
            default_workspace=default_ws,
            allowed_roots=sys_cfg.get("allowed_workspace_roots"),
            user_workspaces=user_ws_map,
        )

    def get_session_state(self, user_id: str) -> Dict[str, Any]:
        return self.session_repo.get_session_state(user_id)

    def record_coding_turn(self, user_id: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        return self.session_repo.update_conversation_state(user_id, conversation_id=conversation_id)

    def _get_driver(self, engine_id: str) -> Any:
        drivers = getattr(self.hub, "drivers", None) if self.hub else None
        return drivers.get(engine_id) if isinstance(drivers, dict) else None

    def _format_engines_summary(self, user_id: str = "default") -> str:
        ws = self.workspace_nav.get_workspace(user_id)
        active = self.get_session_state(user_id).get("active_engine", "")
        if not self.hub or not hasattr(self.hub, "list_configured_engines"):
            return "已配置引擎：agy, pi, claude, omp（发送 /<引擎名称> 进入）"
        return format_engines_summary(
            self.hub.list_configured_engines(), active_engine=active, workspace_dir=ws
        )

    async def _format_engines_summary_async(self, user_id: str) -> str:
        ws = self.workspace_nav.get_workspace(user_id)
        active = self.get_session_state(user_id).get("active_engine", "")
        if self.hub and hasattr(self.hub, "probe_engines_summary"):
            rows = await self.hub.probe_engines_summary(workspace_dir=ws)
            return format_engines_summary(rows, active_engine=active, workspace_dir=ws)
        return self._format_engines_summary(user_id=user_id)

    def match_non_engine_view_command(self, text: str, user_id: str = "default") -> Tuple[bool, str]:
        """Execute synchronous workspace/help/engine view commands in Non-Engine outer state."""
        t = text.strip()
        parts = t.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if t.lower() == "/help":
            return True, NON_ENGINE_HELP_TEXT
        if cmd == "/pwd" and not arg:
            return True, self.workspace_nav.format_pwd(user_id)
        if cmd == "/workspace":
            return True, self.workspace_nav.switch_workspace(user_id, arg)
        if cmd == "/ls":
            return True, self.workspace_nav.format_ls(user_id, arg)
        return False, ""

    def build_cli_flags(
        self, active_engine: str, meta: Dict[str, Any], skip_conversation: bool = False
    ) -> List[str]:
        """Build session CLI overrides, structured-output, and session-resumption flags."""
        drv = self._get_driver(active_engine)
        if drv is None:
            return []
        flags: List[str] = []
        supported = set(getattr(drv, "supported_flags", ()) or ())
        cfg = meta.get("config") or {}
        if isinstance(cfg, dict):
            for key in ("model", "mode", "effort", "agent"):
                flag_name = f"--{key}"
                val = str(cfg.get(key) or "").strip()
                if flag_name in supported and val and re.match(r"^[a-zA-Z0-9._:/-]+$", val):
                    flags.extend([flag_name, val])
            if "--sandbox" in supported and cfg.get("sandbox"):
                flags.append("--sandbox")
            if "--add-dir" in supported:
                for extra_dir in cfg.get("add_dirs") or []:
                    d_str = str(extra_dir).strip()
                    if d_str and "\n" not in d_str and "\x00" not in d_str:
                        flags.extend(["--add-dir", d_str])

        if getattr(drv, "supports_json_output", False):
            flags.extend(["--output-format", "json"])
        conv_flag = str(getattr(drv, "conversation_flag", "") or "").strip()
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
        """Verify local binary presence and switch immediately into the native engine session."""
        drv = self._get_driver(engine_id)
        if not self.hub or drv is None:
            return RouteDecision(True, f"⛔ 未配置引擎 [{engine_id}]，无法进入。", tier="T0_ENGINE_UNAVAILABLE")

        ws = self.workspace_nav.get_workspace(user_id)
        if hasattr(drv, "is_installed") and not drv.is_installed():
            ready = [f"/{r['engine_id']}" for r in self.hub.list_configured_engines() if r.get("installed")]
            ready_hint = ", ".join(ready) if ready else "无已安装引擎"
            return RouteDecision(
                True,
                f"⚠️ 当前宿主未检测到 {engine_id} 可执行文件（{drv.binary_path}），"
                f"请先在宿主安装并配置 PATH 或 {engine_id.upper()}_BINARY。\n当前主机已安装就绪引擎：{ready_hint}。",
                tier="T0_ENGINE_UNAVAILABLE",
            )

        version = await drv.probe_version(ws) if hasattr(drv, "probe_version") else ""
        meta = {
            "version": version,
            "workspace_dir": ws,
        }
        self.session_repo.enter_engine(user_id, engine_id, meta=meta)
        saved_state = self.session_repo.get_session_state(user_id)
        conv_id = str((saved_state.get("meta") or {}).get("conversation_id") or "")
        return RouteDecision(
            True,
            format_entry_summary(
                engine_id,
                version=version,
                workspace_dir=ws,
                conversation_id=conv_id,
                supports_native_session=bool(getattr(drv, "supports_native_session", False)),
            ),
            tier="T0_ENGINE_ENTER",
            preferred_engine=engine_id,
            workspace_dir=ws,
        )

    async def _handle_engine_audit_export(self, user_id: str, engine_arg: str) -> RouteDecision:
        """Generate and export fine-grained per-engine command audit JSON for the user's workspace."""
        target = engine_arg.strip().lower()
        if target and target not in ("agy", "pi", "claude", "omp"):
            return RouteDecision(
                True,
                "⛔ 无效引擎名称，可选值：agy、pi、claude、omp（留空则导出全部四引擎审计）。",
                tier="T0_COMMAND",
            )
        norm_engine = target or None
        ws = self.workspace_nav.get_workspace(user_id)
        out_path, audit = await asyncio.to_thread(
            self.store.export_engine_audit_to_file,
            ws,
            norm_engine,
            user_id,
            100,
        )
        lines = [f"📑 【每引擎指令审计报告 · {audit['filter_engine'].upper()}】"]
        for eid, stats in audit["engine_summaries"].items():
            lines.append(
                f"• /{eid}: 总调用 {stats['total_calls']} 次 "
                f"(文字 {stats['text_calls']} / 语音 {stats['voice_calls']} / 斜杠命令 {stats['slash_calls']})"
            )
        recent = audit["records"][:5]
        if recent:
            lines.append("")
            lines.append(f"🕒 最近 {len(recent)} 条记录：")
            for rec in recent:
                cmd_preview = rec["command"].replace("\n", " ")[:48]
                kind = "语音" if rec["is_voice"] else "文字"
                lines.append(f"  - [{rec['engine_used'].upper()}·{kind}] {cmd_preview}")
        else:
            lines.append("\n（暂无已记录的引擎执行明细）")
        lines.append("")
        lines.append(f"💾 审计 JSON 已导出至：`{out_path}`")
        return RouteDecision(True, "\n".join(lines), tier="T0_COMMAND", workspace_dir=ws)

    async def route_non_engine(self, user_id: str, text: str) -> RouteDecision:
        """Handle text-only routing and host/workspace inspection when outside all engines."""
        t = text.strip()
        low = t.lower()

        if low == "/status":
            ws = self.workspace_nav.get_workspace(user_id)
            if self.health_monitor and hasattr(self.health_monitor, "generate_status_report"):
                report = await self.health_monitor.generate_status_report(
                    hub=self.hub, workspace_dir=ws, active_engine=""
                )
            else:
                report = f"📊 宿主机网关运行正常 | 当前工作区: {ws}"
            return RouteDecision(True, report, tier="T0_COMMAND", effective_query=t)

        if low == "/engines":
            summary = await self._format_engines_summary_async(user_id)
            return RouteDecision(True, summary, tier="T0_COMMAND", effective_query=t)

        if low == "/engines audit" or low.startswith("/engines audit "):
            return await self._handle_engine_audit_export(user_id, t[14:])

        if low == "/export" or low.startswith("/export "):
            return await self._handle_engine_audit_export(user_id, t[7:])

        view_hit, view_reply = self.match_non_engine_view_command(t, user_id=user_id)
        if view_hit:
            return RouteDecision(True, view_reply, tier="T0_COMMAND", effective_query=t)

        m_eng = ENGINE_ENTRY_RE.match(t)
        if m_eng:
            target = m_eng.group(1).lower()
            remainder = (m_eng.group(2) or "").strip()
            if remainder:
                return RouteDecision(
                    True,
                    f"⛔ 当前处于【网关路由状态】，不支持在入口命令后直接附带任务指令。\n👉 请先发送纯命令 /{target} 进入引擎后，再发送对话或任务。",
                    tier="T0_STATE_GUARD",
                )
            return await self.enter_engine(user_id, target)

        return RouteDecision(
            True,
            "⛔ 当前处于【网关路由状态】（外层严禁语音，仅支持文字管理命令）：\n"
            "• /help — 查看帮助与命令说明\n"
            "• /status — 查看宿主机实时硬件状态与已安装引擎\n"
            "• /engines — 查看四大引擎安装状态、版本与路径\n"
            "• /export [引擎名] — 导出每引擎指令调用审计 JSON 至工作区\n"
            "• /pwd — 查看当前工作目录\n"
            "• /workspace <路径> — 校验并切换工作目录\n"
            "• /ls [子目录] — 列出当前工作目录文件\n"
            "• /agy | /pi | /claude | /omp — 进入指定引擎原生会话",
            tier="T0_STATE_GUARD",
        )

    def _exit_engine_decision(self, user_id: str, active_engine: str, ws: str) -> RouteDecision:
        """Exit the current engine session back to the Non-Engine Router State."""
        exited = self.session_repo.exit_engine(user_id) or active_engine
        return RouteDecision(
            True,
            f"✅ 已退出 [{exited.upper()}] 引擎，回到【网关路由状态】。\n"
            f"📂 工作目录保持在 `{ws}`，可发送 /help、/status、/engines、/workspace <路径> 或 /<引擎名>。",
            tier="T0_ENGINE_EXIT",
            workspace_dir=ws,
        )

    async def route_in_engine(
        self, user_id: str, text: str, state: Dict[str, Any]
    ) -> Optional[RouteDecision]:
        """Handle /exit, /clear, /help, interactive replies, and internal engine commands."""
        t = text.strip()
        low = t.lower()
        active_engine = state["active_engine"]
        meta = state.get("meta") or {}
        ws = state.get("workspace_dir") or self.workspace_nav.get_workspace(user_id)
        pending = meta.get("pending_interaction") if isinstance(meta.get("pending_interaction"), dict) else None

        if pending and low in ("/cancel", "cancel", "取消"):
            self.session_repo.clear_pending_interaction(user_id)
            return RouteDecision(
                True,
                "✅ 已取消当前交互式操作。",
                tier=f"T0_ENGINE_INTERACT_CANCEL:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        if low == "/exit":
            return self._exit_engine_decision(user_id, active_engine, ws)

        if low == "/clear":
            self.session_repo.update_conversation_state(user_id, reset=True)
            return RouteDecision(
                True,
                f"🆕 [{active_engine.upper()}] 已清空网关记录的 conversation_id，下轮对话将开启全新原生会话。\n"
                f"📂 工作目录仍保持在 `{ws}`。",
                tier=f"T0_ENGINE_NEW_SESSION:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        if low == "/help":
            return RouteDecision(
                True,
                format_in_engine_help(active_engine, workspace_dir=ws, meta=meta),
                tier=f"T0_ENGINE_HELP:{active_engine}",
                preferred_engine=active_engine,
                workspace_dir=ws,
            )

        drv = self._get_driver(active_engine)
        if pending and not t.startswith("/"):
            pending_cmd = str(pending.get("command") or "").strip().lower()
            if pending_cmd in INTERNAL_ENGINE_COMMANDS:
                res = await dispatch_internal_engine_command(
                    pending_cmd,
                    t,
                    user_id=user_id,
                    active_engine=active_engine,
                    ws=ws,
                    meta=meta,
                    drv=drv,
                    store=self.store,
                    session_repo=self.session_repo,
                    workspace_nav=self.workspace_nav,
                    pending_ctx=pending,
                )
                return self._build_command_decision(res, active_engine, meta)

        if t.startswith("/"):
            parts = t.split(None, 1)
            cmd_name = parts[0][1:].strip().lower()
            args_str = parts[1].strip() if len(parts) > 1 else ""
            if cmd_name in INTERNAL_ENGINE_COMMANDS:
                if pending:
                    self.session_repo.clear_pending_interaction(user_id)
                res = await dispatch_internal_engine_command(
                    cmd_name,
                    args_str,
                    user_id=user_id,
                    active_engine=active_engine,
                    ws=ws,
                    meta=meta,
                    drv=drv,
                    store=self.store,
                    session_repo=self.session_repo,
                    workspace_nav=self.workspace_nav,
                )
                return self._build_command_decision(res, active_engine, meta)

        return None

    def _build_command_decision(
        self, res: Dict[str, Any], active_engine: str, meta: Dict[str, Any]
    ) -> RouteDecision:
        handled = bool(res.get("handled", True))
        ws = str(res.get("workspace_dir") or "")
        if handled:
            return RouteDecision(
                True,
                str(res.get("reply") or ""),
                tier=str(res.get("tier") or f"T0_ENGINE_CMD:{active_engine}"),
                preferred_engine=active_engine,
                workspace_dir=ws,
            )
        drv = self._get_driver(active_engine)
        q = str(res.get("effective_query") or "")
        return RouteDecision(
            False,
            tier=str(res.get("tier") or f"T3_ENGINE_PASSTHROUGH:{active_engine}"),
            preferred_engine=active_engine,
            effective_query=q,
            base_query=q,
            strict_engine_pin=True,
            is_engine_slash=False,
            workspace_dir=ws,
            conversation_id=str(meta.get("conversation_id") or ""),
            session_started_at=float(meta.get("session_started_at") or 0.0),
            supports_native_session=bool(getattr(drv, "supports_native_session", False)),
            cli_flags=self.build_cli_flags(active_engine, meta, skip_conversation=bool(res.get("skip_conv"))),
        )
