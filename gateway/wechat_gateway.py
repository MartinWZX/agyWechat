"""WeChat Message Gateway with Native Voice ASR & L0 Zero-Token Interceptor."""
import re
import time
from typing import Dict, Any, Tuple, Optional
from .voice_normalizer import VoiceTextNormalizer
from memory.core_state_store import CoreStateStore
from skills.scripts.system_status import run_status
from skills.scripts.wind_control_dashboard import run_dashboard
from skills.scripts.hallucination_audit import run_audit
from compressor.cognitive_gate import CognitiveGate, InvocationReason, PreInferenceDataValidator
from bridge.multi_cli_hub import MultiCLIHub
from crystallizer.pattern_crystallizer import PatternCrystallizer

class WeChatGateway:
    def __init__(self, config: Dict[str, Any] = None, hub: MultiCLIHub = None):
        self.config = config or {}
        self.normalizer = VoiceTextNormalizer(self.config)
        self.store = CoreStateStore()
        self.hub = hub or MultiCLIHub(self.config)
        self.crystallizer = PatternCrystallizer()
        self.gate = CognitiveGate()

    def process_incoming_message(self, msg_id: str, user_id: str, msg_type: str, raw_payload: str) -> str:
        is_voice = (msg_type == "voice")
        rec_text = None

        # 1. Voice Recognition Extraction
        if is_voice:
            if raw_payload.strip().startswith("<xml>"):
                rec_text, media_id, fmt = self.normalizer.extract_recognition_from_xml(raw_payload)
            else:
                rec_text = raw_payload # Direct text payload
            
            if not rec_text:
                # Fallback deterministic ASR
                rec_text = self.normalizer.fallback_local_asr("media_unknown", "amr")

            normalized_text = self.normalizer.normalize(rec_text)
            echo_header = self.normalizer.format_voice_echo(normalized_text) + "\n"
        else:
            normalized_text = raw_payload.strip()
            echo_header = ""

        # 2. Pre-Inference Sanity Gate for Voice
        if is_voice:
            valid, reason = PreInferenceDataValidator.validate_wechat_voice(normalized_text)
            if not valid:
                self.store.log_hallucination(msg_id, "gateway", "CORRUPTED_ASR", reason)
                return f"{echo_header}⚠️ 语音识别结果不够清晰，为防止误操作，请重发语音或发送文字指令确认。"

        # 3. L0 Zero-Token Rule Interception
        reply, handled = self._handle_l0_rules(normalized_text)
        if handled:
            self.store.record_message(
                msg_id, "session_default", user_id, msg_type, raw_payload,
                normalized_text, is_voice, reply, "L0_CODE", offloaded=True
            )
            return echo_header + reply

        # 4. Pattern Tracking for Crystallization
        should_crystallize, count = self.crystallizer.record_interaction(normalized_text)

        # 5. Cognitive CLI Awakening (Minimal LLM Invocation)
        manifest = self.gate.build_anchor_manifest({"user_query": {"text": normalized_text, "source": msg_type}})
        compressed_prompt = self.gate.compress_context(normalized_text, manifest)

        import asyncio
        loop = asyncio.get_event_loop()
        success, answer, engine = loop.run_until_complete(
            self.hub.execute_task(compressed_prompt, manifest, priority=0)
        )

        if not success:
            final_reply = f"抱歉，系统暂时无法处理该请求：{answer}"
        else:
            # Extract conclusion for clean user presentation
            conc_match = re.search(r"<final_conclusion>(.*?)</final_conclusion>", answer, re.DOTALL)
            final_reply = conc_match.group(1).strip() if conc_match else answer
            # Check if crystallization block is returned
            self.crystallizer.crystallize_from_response(answer)

        self.store.record_message(
            msg_id, "session_default", user_id, msg_type, raw_payload,
            normalized_text, is_voice, final_reply, engine, offloaded=False
        )
        return echo_header + final_reply

    def _handle_l0_rules(self, text: str) -> Tuple[str, bool]:
        t = text.strip()
        if t in ["#状态", "状态", "查看状态"]:
            return run_status(), True
        if t in ["#风控看板", "风控看板", "#风控"]:
            return run_dashboard(), True
        if t in ["#幻觉审计", "幻觉审计"]:
            return run_audit(), True
        if t.startswith("@pi"):
            return "已收到针对 Pi 引擎的定向指令。", True
        if t.startswith("@claude"):
            return "已收到针对 Claude 引擎的定向指令。", True
        if t.startswith("@agy"):
            return "已收到针对 Agy 引擎的定向指令。", True
        return "", False
