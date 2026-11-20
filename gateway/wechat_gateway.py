"""WeChat Inbound Gateway with Strict Non-Engine Voice Ban & Native Engine Recognition Passthrough."""
import asyncio
import logging
from typing import Any, Dict, Optional
import xml.etree.ElementTree as ET

from bridge.multi_cli_hub import MultiCLIHub
from gateway.terminal_cleaner import ANSICleaner
from memory.core_state_store import CoreStateStore
from router.pipeline import DeterministicRouter
from telemetry.health_monitor import HealthMonitor

logger = logging.getLogger("wechat_agy.gateway")

NON_ENGINE_VOICE_DENIED_MSG = (
    "⚠️ 当前处于网关路由状态，不支持语音控制。请发送文字命令操作"
    "（输入 /help 查看命令，或输入 /agy、/pi、/claude、/omp 进入引擎后再使用语音交谈）。"
)
EMPTY_RECOGNITION_MSG = (
    "⚠️ 未获取到微信原生语音解析结果，请确认已开启微信语音转文字功能或重新发送。"
)


def _extract_xml_voice_fields(xml_payload: str) -> tuple[str, str]:
    """Extract (MsgType, Recognition) cleanly from an inbound WeChat XML payload."""
    try:
        root = ET.fromstring(xml_payload.strip())
    except ET.ParseError:
        return "", ""
    mtype_node = root.find("MsgType")
    rec_node = root.find("Recognition")
    mtype = mtype_node.text.strip().lower() if mtype_node is not None and mtype_node.text else ""
    rec = rec_node.text.strip() if rec_node is not None and rec_node.text else ""
    return mtype, rec


class WeChatGateway:
    """Lightweight two-state WeChat gateway routing directly to native CLI engines."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        hub: Optional[MultiCLIHub] = None,
        store: Optional[CoreStateStore] = None,
        health_monitor: Optional[HealthMonitor] = None,
        settings: Any = None,
        db_path: Optional[str] = None,
    ):
        self.config = config or {}
        self.settings = settings
        sys_cfg = self.config.get("system", {})
        resolved_db = (
            db_path
            or (str(settings.DB_PATH) if settings is not None and hasattr(settings, "DB_PATH") else None)
            or sys_cfg.get("db_path", "memory/agent_core.db")
        )
        self.store = store or CoreStateStore(resolved_db)
        self.hub = hub or MultiCLIHub(
            config=self.config, store=self.store, settings=settings
        )
        self.health_monitor = health_monitor or HealthMonitor(
            settings=settings, store=self.store
        )
        self.cleaner = ANSICleaner()
        self.det_router = DeterministicRouter(
            store=self.store,
            hub=self.hub,
            config=self.config,
            settings=settings,
            health_monitor=self.health_monitor,
        )

    async def process_inbound_message(
        self, user_id: str, text: str, msg_type: str = "text", msg_id: Optional[str] = None
    ) -> str:
        """Process inbound text or voice text from channels such as Tencent iLink."""
        eff_msg_id = msg_id or f"ilink_{int(asyncio.get_running_loop().time() * 1000)}_{user_id}"
        return await self.handle_message(eff_msg_id, user_id, msg_type, text)

    async def handle_message(self, msg_id: str, user_id: str, msg_type: str, raw_payload: str) -> str:
        """Enforce two-state voice boundary and route native text/voice cleanly."""
        self.health_monitor.record_incoming()

        payload = raw_payload or ""
        norm_type = (msg_type or "text").strip().lower()
        stripped_payload = payload.lstrip()
        is_xml = stripped_payload.startswith("<xml>") or stripped_payload.startswith("<?xml")
        xml_type, xml_recognition = _extract_xml_voice_fields(payload) if is_xml else ("", "")
        is_voice = norm_type == "voice" or xml_type == "voice"

        session_state = await asyncio.to_thread(self.det_router.engine_router.get_session_state, user_id)
        active_engine = str(session_state.get("active_engine") or "")

        # Outer Non-Engine State: voice strictly forbidden; text management commands only
        if not active_engine:
            if is_voice:
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, "voice", payload, "", True,
                    NON_ENGINE_VOICE_DENIED_MSG, "ROUTER_GUARD", False,
                )
                return NON_ENGINE_VOICE_DENIED_MSG

            if norm_type != "text":
                guard_msg = "⚠️ 当前处于网关路由状态，仅支持纯文本控制命令（发送 /help 查看可用命令）。"
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, norm_type, payload, "", False,
                    guard_msg, "ROUTER_GUARD", False,
                )
                return guard_msg

            text_cmd = payload.strip()
            decision = await self.det_router.route(user_id, text_cmd, session_state=session_state)
            self.health_monitor.record_command_hit()
            await asyncio.to_thread(
                self.store.record_message,
                msg_id, user_id, user_id, "text", payload, text_cmd, False,
                decision.reply, decision.preferred_engine or "ROUTER", False,
            )
            return decision.reply

        # Inner Engine State: allow verbatim WeChat native Recognition voice + text passthrough
        echo_header = ""
        if is_voice:
            rec_text = xml_recognition if is_xml else payload.strip()
            if not rec_text:
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, "voice", payload, "", True,
                    EMPTY_RECOGNITION_MSG, active_engine, False,
                )
                return EMPTY_RECOGNITION_MSG
            effective_input = rec_text
            echo_header = f'[🎙️ 语音识别: "{rec_text}"]\n'
        else:
            if norm_type != "text" or not payload.strip():
                notice = f"ℹ️ 当前引擎 [{active_engine.upper()}] 暂不支持 '{norm_type}' 空内容消息。"
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, norm_type, payload, "", False,
                    notice, active_engine, False,
                )
                return notice
            effective_input = payload.strip()

        decision = await self.det_router.route(user_id, effective_input, session_state=session_state)
        if decision.handled:
            self.health_monitor.record_command_hit()
            final_reply = f"{echo_header}{decision.reply}" if echo_header else decision.reply
            await asyncio.to_thread(
                self.store.record_message,
                msg_id, user_id, user_id, "voice" if is_voice else "text",
                payload, effective_input, is_voice, final_reply,
                decision.preferred_engine or active_engine, False,
            )
            return final_reply

        self.health_monitor.record_engine_call(active_engine)
        exec_meta: Dict[str, Any] = {}
        ok, answer, used_engine = await self.hub.execute_task(
            decision.effective_query,
            cli_flags=decision.cli_flags or None,
            workspace_dir=decision.workspace_dir or None,
            preferred_engine=decision.preferred_engine or active_engine,
            session_meta_out=exec_meta,
        )
        if ok:
            await asyncio.to_thread(
                self.det_router.engine_router.record_coding_turn,
                user_id,
                exec_meta.get("conversation_id"),
            )

        clean_reply = self.cleaner.clean_text(answer).strip()
        final_reply = f"{echo_header}{clean_reply}" if echo_header else clean_reply
        await asyncio.to_thread(
            self.store.record_message,
            msg_id, user_id, user_id, "voice" if is_voice else "text",
            payload, effective_input, is_voice, final_reply, used_engine, False,
        )
        return final_reply
