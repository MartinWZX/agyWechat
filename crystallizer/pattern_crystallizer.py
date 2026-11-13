"""Interaction Pattern Mining, Governed FAQ Crystallizer & Periodic Cron Detector."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from memory.core_state_store import CoreStateStore
from memory.rsi_repository import RSIRepository
from rsi.eval_harness import EvalHarness, pairwise_answer_consistency
from skills.skill_manager import SkillManager

_SYNC_BRIDGE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crystallizer_sync")


class PatternCrystallizer:
    """Detects recurring FAQ patterns and periodic time-series issues with EvalHarness gating."""

    def __init__(
        self,
        store: Optional[CoreStateStore] = None,
        skill_manager: Optional[SkillManager] = None,
        settings: Any = None,
        db_path: str = "memory/agent_core.db",
        workspace_dir: str = ".",
        consistency_threshold: float = 0.72,
    ):
        self.store = store or CoreStateStore(db_path)
        self.repo = RSIRepository(self.store)
        self.skill_manager = skill_manager or SkillManager(workspace_dir=workspace_dir, settings=settings)
        self.settings = settings
        self.consistency_threshold = consistency_threshold
        base_dir = getattr(settings, "WORKSPACE_ROOT", None) or workspace_dir or getattr(self.skill_manager, "workspace_root", ".")
        self.workspace_dir = str(base_dir)
        self.scripts_dir = str(getattr(self.skill_manager, "l0_scripts_dir", os.path.join(self.workspace_dir, "skills", "scripts")))
        self.jobs_path = os.path.join(self.workspace_dir, "scheduler", "jobs.json")
        os.makedirs(self.scripts_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.jobs_path), exist_ok=True)

    def extract_slots(self, text: str) -> Dict[str, List[str]]:
        """Extract typed parameter slots from raw user text."""
        t = text.strip()
        return {
            "url": re.findall(r"https?://[^\s]+", t),
            "ip": re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", t),
            "date": re.findall(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", t),
            "time": re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", t),
            "num": re.findall(r"\b\d+\b", t),
        }

    def extract_fingerprint(self, text: str) -> str:
        """Extract de-parameterized structural sentence skeleton preserving semantic tags."""
        t = text.strip()
        t = re.sub(r"https?://[^\s]+", "<url>", t)
        t = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", t)
        t = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", "<date>", t)
        t = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", t)
        t = re.sub(r"\b[0-9a-fA-F]{12,64}\b", "<hex>", t)
        t = re.sub(r"\b\d+\b", "<num>", t)
        t = re.sub(r"[？?！!。，,\s]+$", "", t)
        return re.sub(r"\s+", " ", t).strip()

    def fingerprint_to_regex(self, structural_fp: str) -> str:
        """Compile structural fingerprint into a safe full-string regex."""
        tag_map = {
            "<url>": r"https?://[^\s]+",
            "<ip>": r"(?:\d{1,3}\.){3}\d{1,3}",
            "<date>": r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
            "<time>": r"\d{1,2}:\d{2}(?::\d{2})?",
            "<hex>": r"[0-9a-fA-F]{12,64}",
            "<num>": r"\d+",
        }
        escaped = re.escape(structural_fp)
        for tag, pat in tag_map.items():
            escaped = escaped.replace(re.escape(tag), pat)
        escaped = escaped.replace(r"\ ", r"\s+")
        return rf"^\s*{escaped}[？?！!。，,\s]*$"

    def get_pattern_hash(self, structural_fp: str) -> str:
        clean = re.sub(r"[\s+\-_,.!?:：，。？！]", "", structural_fp.lower())
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]

    async def observe_message(self, text: str, threshold: int = 3) -> Tuple[bool, int]:
        """Observe inbound query and stage a `candidate` rule once occurrence reaches threshold."""
        fp = self.extract_fingerprint(text)
        fp_hash = self.get_pattern_hash(fp)
        count, _ = self.repo.observe_pattern(fp, text)

        should_stage = count >= threshold
        if should_stage:
            rule_name = f"crystallized_{fp_hash}"
            existing = self.repo.get_rule(rule_name)
            if existing is None:
                script_path = os.path.join(self.scripts_dir, f"{rule_name}.py")
                script_code = (
                    f'"""Candidate L0 script for pattern {fp_hash} (awaiting verified answer synthesis)."""\n'
                    f"def run(query: str = '', slots=None, **kwargs):\n"
                    f"    return '已命中固化规则 {rule_name} (candidate)'\n"
                )
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(script_code)
                self.repo.upsert_rule(
                    rule_name,
                    script_path=script_path,
                    status="candidate",
                    pattern_regex=self.fingerprint_to_regex(fp),
                    fingerprint=fp,
                    fingerprint_hash=fp_hash,
                    source="auto_candidate",
                )
                self.repo.mark_pattern(fp, crystallized=True)
        return should_stage, count

    def record_verified_interaction(
        self,
        query: str,
        verified_answer: str,
        threshold: int = 3,
        auto_promote: bool = True,
    ) -> Optional[str]:
        """Record a verified LLM answer for a query pattern and promote to `active` once EvalHarness passes."""
        clean_ans = verified_answer.strip()
        if not clean_ans:
            return None
        fp = self.extract_fingerprint(query)
        fp_hash = self.get_pattern_hash(fp)
        pattern = self.repo.append_pattern_answer(fp, clean_ans)
        if pattern is None or int(pattern.get("occurrence_count", 0)) < threshold:
            return None

        answers: List[str] = pattern.get("answers", [])
        if len(answers) < 2:
            return None
        consistency = pairwise_answer_consistency(answers)
        if consistency < self.consistency_threshold:
            return None

        rule_name = f"crystallized_{fp_hash}"
        existing = self.repo.get_rule(rule_name)
        if existing and existing.get("status") == "active":
            return existing.get("script_path")

        canonical_answer = answers[-1]
        slots = self.extract_slots(query)
        has_slots = any(bool(v) for v in slots.values())
        if has_slots:
            distinct_answers = {a.strip() for a in answers if a and a.strip()}
            slot_tokens = [tok for vals in slots.values() for tok in vals if len(tok) >= 2]
            embeds_slot = any(tok in canonical_answer for tok in slot_tokens)
            if len(distinct_answers) > 1 or embeds_slot:
                return None

        script_code = (
            f'"""Auto-synthesized & EvalHarness-verified L0 skill for pattern {fp_hash}."""\n'
            f"RESPONSE = {json.dumps(canonical_answer, ensure_ascii=False)}\n\n"
            f"def run(query: str = '', slots=None, **kwargs) -> str:\n"
            f"    return RESPONSE\n"
        )
        report = EvalHarness.evaluate_script(script_code, sample_queries=[query], sample_slots=[slots])
        if not report.passed:
            self.store.log_hallucination(rule_name, "eval_harness", "EVAL_REJECTED", report.reason)
            return None

        script_path = os.path.join(self.scripts_dir, f"{rule_name}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_code)

        target_status = "active" if auto_promote else "candidate"
        self.repo.upsert_rule(
            rule_name,
            script_path=script_path,
            status=target_status,
            pattern_regex=self.fingerprint_to_regex(fp),
            fingerprint=fp,
            fingerprint_hash=fp_hash,
            response_template=canonical_answer,
            source="verified_synthesis",
        )
        self.repo.mark_pattern(fp, crystallized=True)
        return script_path

    def record_interaction(self, text: str) -> Tuple[bool, int]:
        """Deadlock-free synchronous wrapper around `observe_message`."""
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if in_running_loop:
            return _SYNC_BRIDGE_POOL.submit(lambda: asyncio.run(self.observe_message(text))).result(timeout=10.0)
        return asyncio.run(self.observe_message(text))

    def detect_periodicity(self, fp: str) -> Optional[Dict[str, Any]]:
        """Compute time interval statistics (CV = sigma / mu) and deduce standard 5-field Cron."""
        pattern = self.repo.get_pattern(fp)
        if not pattern:
            return None
        ts_list: List[float] = pattern.get("timestamps", [])
        if len(ts_list) < 3:
            return None

        intervals = [ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list))]
        mean_interval = sum(intervals) / len(intervals)
        if mean_interval <= 0:
            return None

        variance = sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)
        cv = math.sqrt(variance) / mean_interval

        if cv <= 0.25:
            interval_minutes = max(1, int(round(mean_interval / 60)))
            if interval_minutes < 60:
                cron_expr = f"*/{max(5, interval_minutes)} * * * *"
            elif interval_minutes < 1440:
                cron_expr = f"0 */{max(1, int(round(interval_minutes / 60)))} * * *"
            else:
                cron_expr = "0 9 * * *"

            return {
                "fingerprint": fp,
                "raw_sample": pattern["raw_sample"],
                "occurrence_count": pattern["occurrence_count"],
                "mean_interval_sec": round(mean_interval, 2),
                "cv": round(cv, 4),
                "suggested_cron": cron_expr,
            }
        return None

    def solidify_periodic_cron(self, periodicity_info: Dict[str, Any], script_code: Optional[str] = None) -> str:
        """Solidify periodic pattern into `skills/scripts/cron_<hash>.py` and register in `jobs.json`."""
        fp = periodicity_info["fingerprint"]
        fp_hash = self.get_pattern_hash(fp)
        cron_expr = periodicity_info["suggested_cron"]
        raw_sample = periodicity_info["raw_sample"]
        count = periodicity_info["occurrence_count"]

        script_name = f"cron_{fp_hash}"
        script_path = os.path.join(self.scripts_dir, f"{script_name}.py")
        code = script_code or (
            f'"""Auto-crystallized Stage-1 periodic health check for {fp_hash}."""\n'
            f"import os, shutil\n\n"
            f"def run() -> dict | str:\n"
            f"    load1, _, _ = os.getloadavg()\n"
            f"    total_b, used_b, _ = shutil.disk_usage('/')\n"
            f"    disk_pct = round((used_b / total_b) * 100, 1)\n"
            f"    if load1 > 20.0 or disk_pct > 95.0:\n"
            f"        return {{'require_cognitive': True, 'reason': 'INFO_REASONING',\n"
            f"                'payload': {{'load_1m': load1, 'disk_percent': disk_pct, 'status': 'SPIKE_DETECTED'}}}}\n"
            f"    return f\"【主动定时巡检】'{script_name}' 执行正常: Load={{load1:.2f}}, Disk={{disk_pct}}% (0 Token)。\"\n"
        )
        report = EvalHarness.evaluate_script(code, sample_queries=[raw_sample], check_determinism=False)
        if not report.passed:
            raise ValueError(f"Periodic cron script failed EvalHarness: {report.reason}")

        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code.strip() + "\n")

        jobs: List[Dict[str, Any]] = []
        if os.path.exists(self.jobs_path):
            try:
                with open(self.jobs_path, "r", encoding="utf-8") as jf:
                    loaded = json.load(jf)
                    if isinstance(loaded, list):
                        jobs = loaded
            except (OSError, ValueError):
                jobs = []

        job_entry = {
            "id": script_name,
            "name": f"周期任务_{raw_sample[:15]}",
            "raw_sample": raw_sample,
            "cron": cron_expr,
            "schedule": cron_expr,
            "script": script_name,
            "script_path": script_path,
            "is_active": True,
        }
        jobs = [j for j in jobs if j.get("id") != script_name]
        jobs.append(job_entry)
        with open(self.jobs_path, "w", encoding="utf-8") as jf:
            json.dump(jobs, jf, ensure_ascii=False, indent=2)

        self.repo.mark_pattern(fp, cron_proposed=True)
        return (
            f"[⚙️ RSI 自动演进] 检测到您周期性询问/发生「{raw_sample[:20]}」已达 {count} 次"
            f"（周期约 {int(periodicity_info['mean_interval_sec'])} 秒），"
            f"已自动为您固化为纯代码定时任务（Cron: {cron_expr}），"
            f"后续将由代码按时自动执行并汇报，无需再消耗大模型算力。"
        )

    def crystallize_from_response(
        self,
        text_response: str,
        auto_promote: bool = True,
        normalizer: Any = None,
    ) -> Optional[str]:
        """Extract `<crystallize_script>` tags from model response, run EvalHarness, and persist."""
        tag_matches = list(
            re.finditer(
                r"<crystallize_script\s+([^>]+)>\s*(.*?)\s*</crystallize_script>",
                text_response,
                re.DOTALL,
            )
        )
        if not tag_matches:
            return None

        last_script_path: Optional[str] = None
        for tag_m in tag_matches:
            attr_str = tag_m.group(1)
            script_code = tag_m.group(2).strip()
            attrs = dict(re.findall(r'([a-zA-Z_]+)\s*=\s*["\']([^"\']*)["\']', attr_str))
            script_name = attrs.get("name", "").strip()
            if not script_name:
                continue
            trigger_regex = attrs.get("trigger_regex", "").strip()
            voice_homophones = attrs.get("voice_homophones", "").strip()
            cron_expr = attrs.get("cron_expr", "").strip()

            report = EvalHarness.evaluate_script(script_code, sample_queries=[script_name])
            if not report.passed:
                self.store.log_hallucination("crystallize", "all", "SHADOW_DRY_RUN_FAILED", report.reason)
                continue

            script_path = os.path.join(self.scripts_dir, f"{script_name}.py")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_code + "\n")

            status = "active" if (auto_promote and bool(trigger_regex)) else "candidate"
            self.repo.upsert_rule(
                script_name,
                script_path=script_path,
                status=status,
                pattern_regex=trigger_regex,
                voice_homophones=voice_homophones,
                fingerprint=script_name,
                fingerprint_hash=self.get_pattern_hash(script_name),
                response_template=report.sample_output,
                source="model_crystallize_tag",
            )
            if voice_homophones and normalizer is not None and hasattr(normalizer, "learn_homophone"):
                target_cmd = f"#{script_name}" if not trigger_regex else script_name
                for alias in re.split(r"[,，|;；\s]+", voice_homophones):
                    if alias.strip():
                        normalizer.learn_homophone(alias.strip(), target_cmd)

            if cron_expr and cron_expr != "auto":
                self.solidify_periodic_cron(
                    {
                        "fingerprint": script_name,
                        "raw_sample": script_name,
                        "occurrence_count": 3,
                        "mean_interval_sec": 3600,
                        "suggested_cron": cron_expr,
                    },
                    script_code=script_code,
                )
            last_script_path = script_path
        return last_script_path
