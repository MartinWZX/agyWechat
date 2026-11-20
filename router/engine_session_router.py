"""Pure Two-State Engine Session FSM Router."""
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from config import resolve_workspace_root
from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
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
        self.workspace_nav = WorkspaceNavigator(
            self.session_repo,
            default_workspace=default_ws,
            allowed_roots=sys_cfg.get("allowed_workspace_roots"),
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

    def build_cli_flags(self, active_engine: str, meta: Dict[str, Any]) -> List[str]:
        """Build minimal native structured-output and session-resumption flags for active engine."""
        drv = self._get_driver(active_engine)
        if drv is None:
            return []
        flags: List[str] = []
        if getattr(drv, "supports_json_output", False):
            flags.extend(["--output-format", "json"])
        conv_flag = str(getattr(drv, "conversation_flag", "") or "").strip()
        conv_id = str(meta.get("conversation_id") or "").strip()
        if (
            conv_flag in ("--conversation", "--resume")
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
        """Handle strictly /exit, /clear, and /help; pass ALL other inputs straight to native CLI."""
        t = text.strip()
        low = t.lower()
        active_engine = state["active_engine"]
        meta = state.get("meta") or {}
        ws = state.get("workspace_dir") or self.workspace_nav.get_workspace(user_id)

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

        return None
