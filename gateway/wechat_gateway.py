"""WeChat Message Gateway with Unified Async Pipeline, 4-Tier Router & Deadlock-Free Sync Wrapper."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from bridge.multi_cli_hub import MultiCLIHub
from compressor.cognitive_gate import CognitiveGate, PreInferenceDataValidator
from crystallizer.pattern_crystallizer import PatternCrystallizer
from gateway.voice_normalizer import VoiceTextNormalizer
from memory.core_state_store import CoreStateStore
from router.pipeline import DeterministicRouter
from rsi.evolution_engine import EvolutionEngine
from skills.scripts.hallucination_audit import run_audit
from skills.scripts.system_status import run_status
from skills.scripts.wind_control_dashboard import run_dashboard
from tools.tool_registry import ToolRegistry

_GATEWAY_SYNC_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat_gateway_sync")


class RuleMatcher:
    """Backward-compatible L0 command matcher facade."""

    @classmethod
    def match(cls, text: str, db_path: str = "memory/agent_core.db") -> Tuple[bool, str]:
        t = text.strip()
        if t in ["#状态", "状态", "查看状态", "系统状态"]:
            return True, f"Google agy 微信智能体运行看板\n{run_status()}"
        if t in ["#风控看板", "风控看板", "#风控"]:
            return True, f"五维防御看板\n{run_dashboard(db_path=db_path)}"
        if t in ["#幻觉审计", "幻觉审计"]:
            return True, run_audit(db_path=db_path)
        if t in ["#规则", "#演进", "#rsi"]:
            return True, "请通过网关主路由查看 RSI 演进看板。"
        if t in ["#help", "帮助", "使用说明"]:
            return True, (
                "WeChat-AGY 系统帮助手册：\n"
                "1. 查看状态：发送 '#状态' 或语音 '查看状态'\n"
                "2. 风控看板：发送 '#风控看板' 或语音 '风控看板'\n"
                "3. 幻觉审计：发送 '#幻觉审计' | 演进治理：发送 '#规则'\n"
                "4. 定向引擎：@pi, @claude, @agy, @omp"
            )
        if t == "@pi":
            return True, "已收到针对 Pi 引擎的定向指令。"
        if t == "@claude":
            return True, "已收到针对 Claude 引擎的定向指令。"
        if t == "@agy":
            return True, "已收到针对 Agy 引擎的定向指令。"
        if t == "@omp":
            return True, "已收到针对 OMP 引擎的定向指令。"
        return False, ""


def _extract_final_conclusion(raw_answer: str) -> str:
    match = re.search(r"<final_conclusion>(.*?)</final_conclusion>", raw_answer, re.DOTALL)
    return match.group(1).strip() if match else raw_answer.strip()


class WeChatGateway:
    """WeChat Gateway handling voice/text inbound traffic, 4-tier routing, context building, and CLI dispatch."""

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
        rsi_engine: Optional[EvolutionEngine] = None,
    ):
        self.config = config or {}
        self.settings = settings
        self.store = store or CoreStateStore()
        self.governor = governor
        self.bridge = bridge
        self.approval_guard = approval_guard
        self.skill_manager = skill_manager
        self.health_monitor = health_monitor
        self.normalizer = VoiceTextNormalizer(self.config)
        self.crystallizer = crystallizer or PatternCrystallizer(
            self.store, skill_manager=skill_manager, settings=settings
        )
        self.rsi_engine = rsi_engine or EvolutionEngine(
            store=self.store, skill_manager=skill_manager, config=self.config
        )
        self.hub = hub or MultiCLIHub(
            self.config,
            governor=governor,
            store=self.store,
            evolution_engine=self.rsi_engine,
            crystallizer=self.crystallizer,
        )
        self.gate = CognitiveGate()
        self.det_router = DeterministicRouter(
            store=self.store,
            crystallizer=self.crystallizer,
            rsi_engine=self.rsi_engine,
            approval_guard=self.approval_guard,
            hub=self.hub,
            settings=self.settings,
        )
        self._load_rule_homophones()
        self.active_user: Optional[str] = None

    def _load_rule_homophones(self) -> None:
        """Load persisted voice homophone aliases from crystallized rules into normalizer."""
        for rule in self.det_router.repo.list_rules(statuses=["active"]):
            raw_vh = str(rule.get("voice_homophones") or "").strip()
            if not raw_vh:
                continue
            target = str(rule.get("rule_name") or "").strip()
            for alias in re.split(r"[,，|;；\s]+", raw_vh):
                if alias.strip() and target:
                    self.normalizer.learn_homophone(alias.strip(), f"#{target}")

    async def handle_message(
        self,
        msg_id: str,
        user_id: str,
        msg_type: str,
        raw_payload: str,
    ) -> str:
        """Single canonical async inbound pipeline shared by Webhook, iLink, and sync wrappers."""
        self.active_user = user_id
        if self.health_monitor:
            self.health_monitor.record_incoming()

        is_voice = msg_type == "voice"
        if is_voice:
            if raw_payload.strip().startswith("<xml>"):
                rec_text, _, _ = self.normalizer.extract_recognition_from_xml(raw_payload)
            else:
                rec_text = raw_payload
            if not rec_text:
                rec_text = self.normalizer.fallback_local_asr("media_unknown", "amr")
            normalized_text = self.normalizer.normalize(rec_text)
            echo_header = self.normalizer.format_voice_echo(normalized_text) + "\n"
            valid, reason = PreInferenceDataValidator.validate_wechat_voice(normalized_text)
            if not valid:
                self.store.log_hallucination(msg_id, "gateway", "CORRUPTED_ASR", reason)
                return f"{echo_header}⚠️ 语音识别结果不够清晰，为防止误操作，请重发语音或发送文字指令确认。"
        else:
            normalized_text = raw_payload.strip()
            echo_header = ""

        # Tier-0 / Tier-1 / Tier-2 deterministic routing
        decision = await self.det_router.route(user_id, normalized_text)
        if decision.handled:
            if self.health_monitor:
                self.health_monitor.record_l0_hit()
            self.store.record_message(
                msg_id, f"session_{user_id}", user_id, msg_type, raw_payload,
                normalized_text, is_voice, decision.reply, decision.tier, offloaded=True,
            )
            return echo_header + decision.reply

        eff_query = decision.effective_query or normalized_text
        clean_query = decision.base_query or eff_query
        is_feedback_retry = decision.tier == "T3_FEEDBACK_RETRY"
        threshold = int(self.config.get("crystallization", {}).get("faq_solidify_threshold", 3))
        if not is_feedback_retry:
            await self.crystallizer.observe_message(clean_query, threshold=threshold)

        # Auto-solidify periodic Cron when CV <= 0.25 and not yet proposed
        auto_cron = bool(self.config.get("crystallization", {}).get("auto_cron_enabled", True))
        fp = self.crystallizer.extract_fingerprint(clean_query)
        cron_notice = ""
        if auto_cron and not is_feedback_retry:
            pat = self.crystallizer.repo.get_pattern(fp)
            if pat and not pat.get("cron_proposed"):
                p_info = self.crystallizer.detect_periodicity(fp)
                if p_info:
                    cron_notice = "\n\n" + self.crystallizer.solidify_periodic_cron(p_info)

        # Tier-3 Cognitive CLI invocation with rich Context Engineering
        if self.health_monitor:
            self.health_monitor.record_agy_call()

        if self.bridge and hasattr(self.bridge, "execute_prompt"):
            raw_answer = await self.bridge.execute_prompt(eff_query, priority=0)
            final_reply = _extract_final_conclusion(raw_answer)
            self.store.record_message(
                msg_id, f"session_{user_id}", user_id, msg_type, raw_payload,
                normalized_text, is_voice, final_reply, "PTY_BRIDGE", offloaded=False,
            )
            return echo_header + final_reply + cron_notice

        session_hist = self.store.get_recent_exchanges(user_id, limit=3)
        memories = await self.store.search_episodic_memory(clean_query, limit=3)
        tools_summary = ", ".join(
            f"{s['name']}({s['version']})" for s in ToolRegistry.list_tool_signatures()
        )
        manifest = self.gate.build_anchor_manifest({"user_query": {"text": eff_query, "source": msg_type}})
        compressed_prompt = self.gate.compress_context(
            eff_query,
            manifest,
            session_history=session_hist,
            episodic_memories=memories,
            tools_summary=tools_summary,
        )

        success, answer, engine = await self.hub.execute_task(
            compressed_prompt,
            manifest,
            priority=0,
            preferred_engine=decision.preferred_engine,
            auto_apply_rsi_hooks=False,
        )

        if not success:
            final_reply = f"抱歉，系统暂时无法处理该请求：{answer}"
        else:
            final_reply = _extract_final_conclusion(answer)
            policy = self.rsi_engine.phase_controller.get_stage_policy()
            self.crystallizer.crystallize_from_response(
                answer, auto_promote=policy.auto_promote_verified, normalizer=self.normalizer
            )
            self.rsi_engine.process_model_rsi_patches(answer)
            if not is_feedback_retry:
                self.crystallizer.record_verified_interaction(
                    clean_query,
                    final_reply,
                    threshold=threshold,
                    auto_promote=policy.auto_promote_verified,
                )
                self.det_router.cache.put(clean_query, fp, final_reply)
                if self.det_router.cache.is_cacheable(clean_query):
                    fp_hash = self.crystallizer.get_pattern_hash(fp)
                    await self.store.set_episodic_memory(
                        user_id,
                        "qa_fact",
                        fp_hash,
                        f"Q: {clean_query[:120]} -> A: {final_reply[:240]}",
                    )

        self.store.record_message(
            msg_id, f"session_{user_id}", user_id, msg_type, raw_payload,
            normalized_text, is_voice, final_reply, engine, offloaded=False,
        )
        return echo_header + final_reply + cron_notice

    async def process_inbound_message(self, user_id: str, content: str, msg_id: Optional[str] = None) -> str:
        """Asynchronous inbound message entrypoint."""
        mid = msg_id or f"msg_{int(time.time() * 1000)}_{os.urandom(2).hex()}"
        msg_type = "voice" if content.lstrip().startswith("<xml>") else "text"
        return await self.handle_message(mid, user_id, msg_type, content)

    async def route(self, user_id: str, content: str) -> str:
        """Unified routing entrypoint for iLink Gateway and external controllers."""
        return await self.process_inbound_message(user_id, content)

    def process_incoming_message(self, msg_id: str, user_id: str, msg_type: str, raw_payload: str) -> str:
        """Deadlock-free synchronous wrapper preserved for synchronous callers and legacy tests."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if in_running_loop:
            return _GATEWAY_SYNC_POOL.submit(
                lambda: asyncio.run(self.handle_message(msg_id, user_id, msg_type, raw_payload))
            ).result(timeout=75.0)
        return asyncio.run(self.handle_message(msg_id, user_id, msg_type, raw_payload))
