"""WeChat Inbound Gateway with Strict Non-Engine Voice Ban & Native Engine Recognition Passthrough."""
import asyncio
import base64
import binascii
import json
import logging
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
import xml.etree.ElementTree as ET

import httpx

from bridge.multi_cli_hub import MultiCLIHub
from config import resolve_setting_or_yaml
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
_DATA_URI_RE = re.compile(r"^data:([a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+);base64,(.*)$", re.DOTALL)


def _extract_xml_fields(xml_payload: str) -> Dict[str, str]:
    """Extract MsgType, Recognition, and optional image/file attachment fields from WeChat XML."""
    try:
        root = ET.fromstring(xml_payload.strip())
    except ET.ParseError:
        return {}

    def _first_text(*tags: str) -> str:
        for tag in tags:
            node = root.find(tag)
            if node is not None and node.text:
                val = node.text.strip()
                if val:
                    return val
        return ""

    return {
        "msg_type": _first_text("MsgType").lower(),
        "recognition": _first_text("Recognition"),
        "pic_url": _first_text("PicUrl"),
        "file_url": _first_text("FileUrl", "Url"),
        "media_id": _first_text("MediaId"),
        "file_name": _first_text("FileName", "Title"),
        "content": _first_text("Content"),
        "base64_data": _first_text("ImageBase64", "Base64", "Data", "FileData"),
        "caption": _first_text("Caption", "Prompt", "Description"),
    }


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
        self.progress_interval = float(
            resolve_setting_or_yaml(
                settings,
                "PROGRESS_PUSH_INTERVAL_SECONDS",
                sys_cfg.get("progress_push_interval_seconds"),
                0.0,
            ) or 0.0
        )
        self.det_router = DeterministicRouter(
            store=self.store,
            hub=self.hub,
            config=self.config,
            settings=settings,
            health_monitor=self.health_monitor,
        )

    async def process_inbound_message(
        self,
        user_id: str,
        text: str,
        msg_type: str = "text",
        msg_id: Optional[str] = None,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        media_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Process inbound text, voice text, or attachment from channels such as Tencent iLink."""
        eff_msg_id = msg_id or f"ilink_{int(asyncio.get_running_loop().time() * 1000)}_{user_id}"
        return await self.handle_message(
            eff_msg_id,
            user_id,
            msg_type,
            text,
            progress_callback=progress_callback,
            media_meta=media_meta,
        )

    async def _resolve_and_save_asset(
        self,
        user_id: str,
        norm_type: str,
        raw_payload: str,
        xml_fields: Dict[str, str],
        media_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[Path], int, str, str]:
        """Decode or download an inbound WeChat image/file fragment and save it to user's workspace."""
        asset_type = "image" if (norm_type == "image" or xml_fields.get("msg_type") == "image") else "file"
        meta: Dict[str, Any] = dict(media_meta or {})
        stripped = (raw_payload or "").strip()
        if not meta and not xml_fields and stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    meta.update(parsed)
            except ValueError:
                pass

        caption = str(meta.get("caption") or meta.get("prompt") or xml_fields.get("caption") or "").strip()
        file_name = str(
            meta.get("file_name")
            or meta.get("filename")
            or meta.get("name")
            or xml_fields.get("file_name")
            or xml_fields.get("media_id")
            or ""
        ).strip()
        b64_val = str(meta.get("base64") or meta.get("data") or xml_fields.get("base64_data") or "").strip()
        text_fragment = str(meta.get("content") or xml_fields.get("content") or "")
        url_val = str(
            meta.get("url")
            or meta.get("pic_url")
            or meta.get("file_url")
            or meta.get("media_url")
            or xml_fields.get("pic_url")
            or xml_fields.get("file_url")
            or ""
        ).strip()

        if not b64_val and not text_fragment and not url_val and stripped and not stripped.startswith("{") and not xml_fields:
            if stripped.startswith(("http://", "https://")):
                url_val = stripped
            elif asset_type == "image":
                b64_val = stripped
            else:
                text_fragment = raw_payload

        raw_bytes = b""
        if b64_val:
            b64_body = b64_val
            m_uri = _DATA_URI_RE.match(b64_val)
            if m_uri:
                mime = m_uri.group(1).lower()
                b64_body = m_uri.group(2).strip()
                if not file_name:
                    ext = mime.split("/", 1)[1].split("+", 1)[0]
                    ext = "jpg" if ext == "jpeg" else ext
                    file_name = f"wx_{asset_type}.{ext}"
            try:
                raw_bytes = base64.b64decode(b64_body, validate=True)
            except (ValueError, binascii.Error):
                return False, None, 0, caption, "无效的 Base64 附件数据。"
        elif text_fragment:
            raw_bytes = text_fragment.encode("utf-8")
        elif url_val:
            if not url_val.startswith(("http://", "https://")):
                return False, None, 0, caption, "仅支持 http/https 远程附件下载链接。"
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(url_val)
                    resp.raise_for_status()
                    raw_bytes = resp.content
            except httpx.HTTPError as exc:
                return False, None, 0, caption, f"拉取微信附件失败：{exc}"
            if not file_name:
                url_tail = Path(url_val.split("?", 1)[0]).name
                if url_tail:
                    file_name = url_tail
        else:
            return False, None, 0, caption, "未提供有效的图片/文件数据（缺少 base64、content 或 url）。"

        nav = self.det_router.engine_router.workspace_nav
        ok, saved_path, err = await asyncio.to_thread(
            nav.save_inbound_asset, user_id, raw_bytes, file_name, asset_type
        )
        return ok, saved_path, len(raw_bytes), caption, err

    async def handle_message(
        self,
        msg_id: str,
        user_id: str,
        msg_type: str,
        raw_payload: str,
        progress_callback: Optional[Callable[[str], Awaitable[Any]]] = None,
        media_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enforce two-state voice boundary, persist inbound assets, and route directly to native CLIs."""
        self.health_monitor.record_incoming()

        payload = raw_payload or ""
        norm_type = (msg_type or "text").strip().lower()
        stripped_payload = payload.lstrip()
        is_xml = stripped_payload.startswith("<xml>") or stripped_payload.startswith("<?xml")
        xml_fields = _extract_xml_fields(payload) if is_xml else {}
        xml_type = xml_fields.get("msg_type", "")
        xml_recognition = xml_fields.get("recognition", "")
        is_voice = norm_type == "voice" or xml_type == "voice"
        is_asset = norm_type in {"image", "file", "attachment"} or xml_type in {"image", "file", "attachment"}

        session_state = await asyncio.to_thread(self.det_router.engine_router.get_session_state, user_id)
        active_engine = str(session_state.get("active_engine") or "")

        if is_asset:
            ok_save, saved_path, byte_size, caption, err_msg = await self._resolve_and_save_asset(
                user_id, norm_type, payload, xml_fields, media_meta=media_meta
            )
            asset_tag = "image" if (norm_type == "image" or xml_type == "image") else "file"
            asset_label = "图片" if asset_tag == "image" else "文件片段"
            if not ok_save or saved_path is None:
                fail_reply = f"⚠️ 微信{asset_label}落盘失败：{err_msg}"
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, asset_tag, payload, "", False,
                    fail_reply, active_engine or "WORKSPACE_ASSET", False,
                )
                return fail_reply

            saved_notice = (
                f"📥 已将微信{asset_label}自动落盘至当前绑定工作区：\n"
                f"• 路径: `{saved_path}`\n"
                f"• 大小: {byte_size} bytes"
            )
            if active_engine and caption:
                effective_prompt = f"[微信{asset_label}已落盘至工作区: {saved_path}]\n{caption}"
                decision = await self.det_router.route(user_id, effective_prompt, session_state=session_state)
                self.health_monitor.record_engine_call(active_engine)
                exec_meta: Dict[str, Any] = {}
                ok, answer, used_engine = await self.hub.execute_task(
                    decision.effective_query,
                    cli_flags=decision.cli_flags or None,
                    workspace_dir=decision.workspace_dir or None,
                    preferred_engine=decision.preferred_engine or active_engine,
                    session_meta_out=exec_meta,
                    progress_callback=progress_callback,
                    progress_interval=self.progress_interval,
                )
                if ok:
                    await asyncio.to_thread(
                        self.det_router.engine_router.record_coding_turn,
                        user_id,
                        exec_meta.get("conversation_id"),
                    )
                clean_reply = self.cleaner.clean_text(answer).strip()
                combined_reply = f"{saved_notice}\n\n{clean_reply}" if clean_reply else saved_notice
                await asyncio.to_thread(
                    self.store.record_message,
                    msg_id, user_id, user_id, asset_tag, payload, effective_prompt, False,
                    combined_reply, used_engine, False,
                )
                return combined_reply

            await asyncio.to_thread(
                self.store.record_message,
                msg_id, user_id, user_id, asset_tag, payload, str(saved_path), False,
                saved_notice, active_engine or "WORKSPACE_ASSET", False,
            )
            return saved_notice

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
            progress_callback=progress_callback,
            progress_interval=self.progress_interval,
        )
        if ok and not decision.tier.startswith("T3_ENGINE_BTW:"):
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
