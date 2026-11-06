"""WeChat Message Gateway with Native Voice ASR & L0 Zero-Token Interceptor."""
import asyncio
import re
import time
from typing import Any, Dict, Optional, Tuple
from bridge.multi_cli_hub import MultiCLIHub
from compressor.cognitive_gate import CognitiveGate, PreInferenceDataValidator
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.voice_normalizer import VoiceTextNormalizer
from memory.core_state_store import CoreStateStore
from skills.scripts.hallucination_audit import run_audit
from skills.scripts.system_status import run_status
from skills.scripts.wind_control_dashboard import run_dashboard


class RuleMatcher:
    """Matches L0 exact commands, regex, and crystallized FAQ rules."""

    @classmethod
    def match(cls, text: str) -> Tuple[bool, str]:
        t = text.strip()
        if t in ["#状态", "状态", "查看状态", "系统状态"]:
            return True, f"Google agy 微信智能体运行看板\n{run_status()}"
        if t in ["#风控看板", "风控看板", "#风控"]:
            return True, f"五维防御看板\n{run_dashboard()}"
        if t in ["#幻觉审计", "幻觉审计"]:
            return True, run_audit()
        if t in ["#help", "帮助", "使用说明"]:
            return True, (
                "WeChat-AGY 系统帮助手册：\n"
                "1. 查看状态：发送 '#状态' 或语音 '查看状态'\n"
                "2. 风控看板：发送 '#风控看板' 或语音 '风控看板'\n"
                "3. 幻觉审计：发送 '#幻觉审计'\n"
                "4. 定向引擎：@pi, @claude, @agy, @omp"
            )
        if t.startswith("@pi"):
            return True, "已收到针对 Pi 引擎的定向指令。"
        if t.startswith("@claude"):
            return True, "已收到针对 Claude 引擎的定向指令。"
        if t.startswith("@agy"):
            return True, "已收到针对 Agy 引擎的定向指令。"
        if t.startswith("@omp"):
            return True, "已收到针对 OMP 引擎的定向指令。"
        return False, ""


class WeChatGateway:
    """WeChat Gateway handling voice/text inbound traffic, L0 short-circuiting, and CLI dispatch."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        hub: Optional[MultiCLIHub] = None,
        store: Optional[CoreStateStore] = None,
        governor: Any = None,
        bridge: Any = None,
        approval_guard: Any = None,
        crystallizer: Optional[PatternCrystallizer] = None,
        skill_manager: Any = None,
        health_monitor: Any = None,
        settings: Any = None,
    ):
        self.config = config or {}
        self.settings = settings
        self.store = store or CoreStateStore()
        self.governor = governor
        self.bridge = bridge
        self.approval_guard = approval_guard
        self.skill_manager = skill_manager
        self.health_monitor = health_monitor
        self.hub = hub or MultiCLIHub(self.config, governor=governor)
        self.normalizer = VoiceTextNormalizer(self.config)
        self.crystallizer = crystallizer or PatternCrystallizer(self.store, skill_manager=skill_manager, settings=settings)
        self.gate = CognitiveGate()

    async def process_inbound_message(self, user_id: str, content: str) -> str:
        """Asynchronous inbound message processing used in code-first test suite."""
        if self.health_monitor:
            self.health_monitor.record_incoming()

        normalized = self.normalizer.normalize(content)
        matched, reply = RuleMatcher.match(normalized)
        if matched:
            if self.health_monitor:
                self.health_monitor.record_l0_hit()
            return reply

        # Check crystallizer observation
        await self.crystallizer.observe_message(normalized)

        # Dispatch cognitive task
        if self.health_monitor:
            self.health_monitor.record_agy_call()

        if self.bridge and hasattr(self.bridge, "execute_prompt"):
            return await self.bridge.execute_prompt(normalized, priority=0)

        manifest = self.gate.build_anchor_manifest({"user_query": {"text": normalized}})
        prompt = self.gate.compress_context(normalized, manifest)
        success, answer, _ = await self.hub.execute_task(prompt, manifest, priority=0)
        return answer if success else f"抱歉，系统暂时无法处理该请求：{answer}"

    def process_incoming_message(self, msg_id: str, user_id: str, msg_type: str, raw_payload: str) -> str:
        """Synchronous inbound message processing used in full-system test suite."""
        is_voice = (msg_type == "voice")
        rec_text = None

        if is_voice:
            if raw_payload.strip().startswith("<xml>"):
                rec_text, _, _ = self.normalizer.extract_recognition_from_xml(raw_payload)
            else:
                rec_text = raw_payload

            if not rec_text:
                rec_text = self.normalizer.fallback_local_asr("media_unknown", "amr")

            normalized_text = self.normalizer.normalize(rec_text)
            echo_header = self.normalizer.format_voice_echo(normalized_text) + "\n"
        else:
            normalized_text = self.normalizer.normalize(raw_payload.strip())
            echo_header = ""

        # Pre-Inference Sanity Gate for Voice
        if is_voice:
            valid, reason = PreInferenceDataValidator.validate_wechat_voice(normalized_text)
            if not valid:
                self.store.log_hallucination(msg_id, "gateway", "CORRUPTED_ASR", reason)
                return f"{echo_header}⚠️ 语音识别结果不够清晰，为防止误操作，请重发语音或发送文字指令确认。"

        # L0 Zero-Token Interception
        matched, l0_reply = RuleMatcher.match(normalized_text)
        if matched:
            self.store.record_message(
                msg_id, "session_default", user_id, msg_type, raw_payload,
                normalized_text, is_voice, l0_reply, "L0_CODE", offloaded=True
            )
            return echo_header + l0_reply

        # Pattern Tracking for Crystallization
        self.crystallizer.record_interaction(normalized_text)

        # Cognitive CLI Awakening
        manifest = self.gate.build_anchor_manifest({"user_query": {"text": normalized_text, "source": msg_type}})
        compressed_prompt = self.gate.compress_context(normalized_text, manifest)

        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                self.hub.execute_task(compressed_prompt, manifest, priority=0),
                loop
            )
            success, answer, engine = future.result()
        except RuntimeError:
            success, answer, engine = asyncio.run(
                self.hub.execute_task(compressed_prompt, manifest, priority=0)
            )

        if not success:
            final_reply = f"抱歉，系统暂时无法处理该请求：{answer}"
        else:
            conc_match = re.search(r"<final_conclusion>(.*?)</final_conclusion>", answer, re.DOTALL)
            final_reply = conc_match.group(1).strip() if conc_match else answer
            self.crystallizer.crystallize_from_response(answer)

        self.store.record_message(
            msg_id, "session_default", user_id, msg_type, raw_payload,
            normalized_text, is_voice, final_reply, engine, offloaded=False
        )
        return echo_header + final_reply
