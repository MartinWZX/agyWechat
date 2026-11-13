"""Four-Tier Deterministic Routing DAG with Two-State Engine FSM, Active RSI Skills & Negative-Feedback Degradation."""
from collections import OrderedDict
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from crystallizer.pattern_crystallizer import PatternCrystallizer
from memory.core_state_store import CoreStateStore
from memory.rsi_repository import RSIRepository
from router.engine_session_router import EngineSessionRouter, RouteDecision
from rsi.eval_harness import char_ngram_similarity
from rsi.evolution_engine import EvolutionEngine
from sandbox.runner import run_file_async
from tools.tool_registry import ToolRegistry

logger = logging.getLogger("wechat_agy.router")

MUTATION_INTENT_RE = re.compile(
    r"(?:创建|新建|修改|编辑|删除|写入|重构|修复|执行命令|提交|推送|write|create|delete|modify|refactor|commit|push)",
    re.IGNORECASE,
)
NEGATIVE_FEEDBACK_RE = re.compile(
    r"^(?:不对|错了|答错了|答非所问|结果不对|重新回答|重来)[！!。，,\s]*$",
    re.IGNORECASE,
)


class SemanticCache:
    """TTL-bounded stage-adaptive semantic cache for verified non-mutating Q&A."""

    def __init__(self, max_entries: int = 256, ttl_seconds: float = 1800.0):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: "OrderedDict[str, Tuple[str, str, float]]" = OrderedDict()

    @staticmethod
    def is_cacheable(query: str) -> bool:
        if len(query.strip()) < 2:
            return False
        return not bool(MUTATION_INTENT_RE.search(query))

    def put(self, query: str, fingerprint: str, answer: str) -> None:
        if not self.is_cacheable(query) or not answer.strip():
            return
        key = query.strip()
        self._entries[key] = (fingerprint, answer.strip(), time.time())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def lookup(self, query: str, fingerprint: str, similarity_threshold: float = 0.88) -> Optional[str]:
        if not self.is_cacheable(query):
            return None
        now = time.time()
        clean_q = query.strip()
        expired = [k for k, (_, _, ts) in self._entries.items() if now - ts > self.ttl_seconds]
        for k in expired:
            self._entries.pop(k, None)

        if clean_q in self._entries:
            self._entries.move_to_end(clean_q)
            return self._entries[clean_q][1]

        best_score = 0.0
        best_ans: Optional[str] = None
        for cached_q, (cached_fp, ans, _) in self._entries.items():
            if cached_fp == fingerprint:
                return ans
            sim = char_ngram_similarity(clean_q, cached_q, n=2)
            if sim >= similarity_threshold and sim > best_score:
                best_score = sim
                best_ans = ans
        return best_ans

    def invalidate_fingerprint(self, fingerprint: str) -> int:
        to_remove = [k for k, (fp, _, _) in self._entries.items() if fp == fingerprint]
        for k in to_remove:
            self._entries.pop(k, None)
        return len(to_remove)

    def clear(self) -> None:
        self._entries.clear()


class DeterministicRouter:
    """Executes Tier-0 (Commands), Tier-1 (Active RSI Skills), and Tier-2 (Semantic Cache) before LLM."""

    def __init__(
        self,
        store: CoreStateStore,
        crystallizer: PatternCrystallizer,
        rsi_engine: Optional[EvolutionEngine] = None,
        approval_guard: Any = None,
        hub: Any = None,
        settings: Any = None,
        semantic_cache: Optional[SemanticCache] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.store = store
        self.repo = RSIRepository(store)
        self.crystallizer = crystallizer
        self.rsi_engine = rsi_engine
        self.approval_guard = approval_guard
        self.hub = hub
        self.settings = settings
        self.cache = semantic_cache or SemanticCache()
        self.engine_router = EngineSessionRouter(store=store, hub=hub, config=config, settings=settings)
        self._last_served_by_user: Dict[str, Dict[str, str]] = {}

    def _is_admin(self, user_id: str) -> bool:
        admins = getattr(self.settings, "ADMIN_USERS", None) or ["admin_user"]
        return "*" in admins or user_id in admins

    def _render_rsi_rules_board(self) -> str:
        policy = self.rsi_engine.phase_controller.get_stage_policy() if self.rsi_engine else None
        active = self.repo.count_rules("active")
        cand = self.repo.count_rules("candidate")
        deg = self.repo.count_rules("degraded")
        tools_cnt = len(ToolRegistry.list_tools())
        stage_line = f"{policy.stage.value} (score={policy.maturity_score})" if policy else "UNKNOWN"
        return (
            "【🧬 RSI 自演进与固化治理看板】\n"
            f"• 演进阶段: {stage_line}\n"
            f"• 已激活技能 (Active): {active} | 候选评估中 (Candidate): {cand} | 已降级 (Degraded): {deg}\n"
            f"• 确定性工具数: {tools_cnt}"
        )

    def match_tier0_command(self, text: str, user_id: str = "default") -> Tuple[bool, str, Optional[str], str]:
        """Match read-only Non-Engine view commands."""
        t = text.strip()
        handled, reply = self.engine_router.match_non_engine_view_command(
            t, rsi_board_fn=self._render_rsi_rules_board, user_id=user_id
        )
        if handled:
            return True, reply, None, t
        return False, "", None, t

    async def route(self, user_id: str, normalized_text: str) -> RouteDecision:
        """Execute Two-State Engine FSM + Tier-1/Tier-2/Tier-3 routing pipeline."""
        t = normalized_text.strip()
        state = self.engine_router.get_session_state(user_id)
        active_engine = state["active_engine"]

        if not active_engine:
            return await self.engine_router.route_non_engine(
                user_id, t, rsi_board_fn=self._render_rsi_rules_board
            )

        in_eng_decision = await self.engine_router.route_in_engine(user_id, t, state)
        if in_eng_decision is not None:
            return in_eng_decision

        pref_engine = active_engine
        eff_query = t
        meta = state["meta"]
        session_flags = self.engine_router.build_cli_flags(meta)
        ws_dir = state.get("workspace_dir") or self.engine_router.workspace_nav.get_workspace(user_id)
        conv_id = str(meta.get("conversation_id") or "")
        sess_started = float(meta.get("session_started_at") or 0.0)
        native_sess = bool((meta.get("session_capabilities") or {}).get("supports_native_session"))

        # Negative conversational feedback inside active engine -> degrade rule + retry on active engine
        if NEGATIVE_FEEDBACK_RE.match(t) and user_id in self._last_served_by_user:
            last_ctx = self._last_served_by_user.pop(user_id)
            rule_name = last_ctx.get("rule_name", "")
            orig_query = last_ctx.get("query", "")
            fp = last_ctx.get("fingerprint", "")
            if rule_name:
                self.repo.record_rule_failure(rule_name)
                self.repo.set_rule_status(rule_name, "degraded")
            if fp:
                self.cache.invalidate_fingerprint(fp)
            if orig_query:
                return RouteDecision(
                    False,
                    tier="T3_FEEDBACK_RETRY",
                    preferred_engine=pref_engine,
                    effective_query=f"{orig_query}（用户反馈上一次回答有误，请重新核实并严谨回答）",
                    base_query=orig_query,
                    strict_engine_pin=True,
                    coding_mode=True,
                    workspace_dir=ws_dir,
                    conversation_id=conv_id,
                    session_started_at=sess_started,
                    supports_native_session=native_sess,
                    cli_flags=session_flags,
                )
            return RouteDecision(True, "已记录纠错并降级对应缓存/规则，请重发您的具体问题。", tier="T0_FEEDBACK")

        policy = self.rsi_engine.phase_controller.get_stage_policy() if self.rsi_engine else None
        skill_theta = policy.skill_similarity_threshold if policy else 0.82
        cache_theta = policy.semantic_cache_threshold if policy else 0.88

        # Tier-1: Active EvalHarness-verified RSI skills
        fp = self.crystallizer.extract_fingerprint(eff_query)
        slots = self.crystallizer.extract_slots(eff_query)
        for rule in self.repo.list_rules(statuses=["active"]):
            pat_re = rule.get("pattern_regex") or ""
            rule_fp = rule.get("fingerprint") or ""
            matched = False
            if pat_re:
                try:
                    matched = bool(re.search(pat_re, eff_query))
                except re.error:
                    matched = False
            if not matched and rule_fp:
                matched = (rule_fp == fp) or (char_ngram_similarity(rule_fp, fp, n=2) >= skill_theta)

            if not matched:
                continue

            script_path = str(rule.get("script_path") or "")
            rule_name = str(rule["rule_name"])
            if not script_path:
                continue
            sres = await run_file_async(
                script_path,
                entrypoint="run",
                kwargs={"query": eff_query, "slots": slots},
                timeout=10.0,
            )
            if sres.ok and sres.result is not None and str(sres.result).strip():
                reply_str = str(sres.result).strip()
                self.repo.record_rule_hit(rule_name)
                self._last_served_by_user[user_id] = {
                    "rule_name": rule_name,
                    "query": eff_query,
                    "fingerprint": fp,
                    "preferred_engine": pref_engine or "",
                }
                return RouteDecision(
                    True, reply_str, tier=f"T1_SKILL:{rule_name}",
                    preferred_engine=pref_engine, effective_query=eff_query, base_query=eff_query,
                    strict_engine_pin=True,
                )

            # Sandbox runtime failure -> degrade rule & trigger self-healing; re-run healed skill immediately if recovered
            err_msg = sres.error_summary()
            logger.warning("Active skill '%s' failed (%s); degrading & scheduling self-heal.", rule_name, err_msg)
            self.repo.record_rule_failure(rule_name)
            self.repo.set_rule_status(rule_name, "degraded")
            if self.rsi_engine and self.hub:
                healed = await self.rsi_engine.run_self_healing_cycle(
                    script_path, err_msg, self.hub, rule_name=rule_name, priority=0
                )
                if healed:
                    retry_res = await run_file_async(
                        script_path,
                        entrypoint="run",
                        kwargs={"query": eff_query, "slots": slots},
                        timeout=10.0,
                    )
                    if retry_res.ok and retry_res.result is not None and str(retry_res.result).strip():
                        reply_str = str(retry_res.result).strip()
                        self.repo.record_rule_hit(rule_name)
                        self._last_served_by_user[user_id] = {
                            "rule_name": rule_name,
                            "query": eff_query,
                            "fingerprint": fp,
                            "preferred_engine": pref_engine or "",
                        }
                        return RouteDecision(
                            True, reply_str, tier=f"T1_SKILL_HEALED:{rule_name}",
                            preferred_engine=pref_engine, effective_query=eff_query, base_query=eff_query,
                            strict_engine_pin=True,
                        )

        # Tier-2: Stage-adaptive semantic cache (bypassed inside pinned active engine)
        if not pref_engine:
            cached_ans = self.cache.lookup(eff_query, fp, similarity_threshold=cache_theta)
            if cached_ans:
                self._last_served_by_user[user_id] = {
                    "rule_name": "",
                    "query": eff_query,
                    "fingerprint": fp,
                    "preferred_engine": "",
                }
                return RouteDecision(True, cached_ans, tier="T2_CACHE", effective_query=eff_query, base_query=eff_query)

        # Tier-3: Escalate to Cognitive CLI pinned strictly to active engine
        return RouteDecision(
            False,
            tier="T3_COGNITIVE",
            preferred_engine=pref_engine,
            effective_query=eff_query,
            base_query=eff_query,
            strict_engine_pin=True,
            coding_mode=True,
            workspace_dir=ws_dir,
            conversation_id=conv_id,
            session_started_at=sess_started,
            supports_native_session=native_sess,
            cli_flags=session_flags,
        )
