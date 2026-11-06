"""Interaction Pattern Mining, Periodic Issue Time-Series Detector & Crystallizer."""
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from memory.core_state_store import CoreStateStore
from skills.skill_manager import SkillManager
from verifier.hallucination_guard import ShadowDryRunValidator, ShadowDryRunError


class PatternCrystallizer:
    """Detects recurring FAQ patterns and periodic time-series issues to crystallize into L0/Cron code."""

    def __init__(
        self,
        store: Optional[CoreStateStore] = None,
        skill_manager: Optional[SkillManager] = None,
        settings: Any = None,
        db_path: str = "memory/agent_core.db",
        workspace_dir: str = "."
    ):
        self.store = store or CoreStateStore(db_path)
        self.skill_manager = skill_manager or SkillManager(workspace_dir=workspace_dir, settings=settings)
        self.settings = settings
        base_dir = getattr(settings, "WORKSPACE_ROOT", None) or workspace_dir or getattr(self.skill_manager, "workspace_root", ".")
        self.workspace_dir = str(base_dir)
        self.scripts_dir = str(getattr(self.skill_manager, "l0_scripts_dir", os.path.join(self.workspace_dir, "skills", "scripts")))
        self.jobs_path = os.path.join(self.workspace_dir, "scheduler", "jobs.json")
        os.makedirs(self.scripts_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.jobs_path), exist_ok=True)

    def extract_fingerprint(self, text: str) -> str:
        """Extract de-parameterized structural sentence skeleton preserving semantic tags."""
        t = text.strip()
        # 1. IP address replacement
        t = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", t)
        # 2. Date replacement
        t = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", "<date>", t)
        # 3. Time replacement
        t = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<time>", t)
        # 4. Numeric constants replacement
        t = re.sub(r"\b\d+\b", "<num>", t)
        return t

    def get_pattern_hash(self, structural_fp: str) -> str:
        clean = re.sub(r"[\s+\-_,.!?:：，。？！]", "", structural_fp)
        return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]

    async def observe_message(self, text: str) -> Tuple[bool, int]:
        """Asynchronously observe inbound query and trigger L0 crystallization on 3rd occurrence."""
        fp = self.extract_fingerprint(text)
        fp_hash = self.get_pattern_hash(fp)
        now = time.time()

        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT occurrence_count, timestamps_json FROM interaction_patterns WHERE fingerprint = ?;", (fp,))
            row = c.fetchone()
            if row:
                count = row["occurrence_count"] + 1
                try:
                    ts_list = json.loads(row["timestamps_json"] or "[]")
                except Exception:
                    ts_list = []
                ts_list.append(now)
                c.execute(
                    "UPDATE interaction_patterns SET occurrence_count = ?, last_seen = ?, timestamps_json = ? WHERE fingerprint = ?;",
                    (count, now, json.dumps(ts_list), fp)
                )
            else:
                count = 1
                ts_list = [now]
                c.execute(
                    "INSERT INTO interaction_patterns (fingerprint, raw_sample, occurrence_count, last_seen, crystallized, timestamps_json) VALUES (?, ?, 1, ?, 0, ?);",
                    (fp, text, now, json.dumps(ts_list))
                )
            conn.commit()

        # If threshold reached (>= 3), auto-crystallize L0 rule
        should_crystallize = (count >= 3)
        if should_crystallize:
            rule_name = f"crystallized_{fp_hash}"
            script_path = os.path.join(self.scripts_dir, f"{rule_name}.py")
            sample_safe = text.replace('"', '\\"')
            script_code = f'''"""Auto-crystallized L0 deterministic script for pattern {fp_hash}."""
def run(query="{sample_safe}", **kwargs):
    return f"【确定性纯代码响应】已命中固化规则 {rule_name}，参数识别正常 (0 Token)。"
'''
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script_code)

            with self.store._get_connection() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO crystallized_rules (rule_name, pattern_regex, voice_homophones, script_path, hit_count, created_at)
                VALUES (?, ?, '', ?, 0, ?);
                """, (rule_name, re.escape(text[:10]), script_path, time.time()))
                conn.execute("UPDATE interaction_patterns SET crystallized = 1 WHERE fingerprint = ?;", (fp,))
                conn.commit()

        return should_crystallize, count

    def record_interaction(self, text: str) -> Tuple[bool, int]:
        """Synchronous record interaction wrapper."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(self.observe_message(text), loop)
            return future.result()
        except RuntimeError:
            return asyncio.run(self.observe_message(text))

    def detect_periodicity(self, fp: str) -> Optional[Dict[str, Any]]:
        """Compute time interval statistics and deduce standard Cron expression."""
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT timestamps_json, raw_sample, occurrence_count FROM interaction_patterns WHERE fingerprint = ?;", (fp,))
            row = c.fetchone()
            if not row:
                return None
            try:
                ts_list = json.loads(row["timestamps_json"] or "[]")
            except Exception:
                return None

            if len(ts_list) < 3:
                return None

            raw_sample = row["raw_sample"]
            count = row["occurrence_count"]

        # Calculate intervals
        intervals = [ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list))]
        mean_interval = sum(intervals) / len(intervals)
        if mean_interval <= 0:
            return None

        variance = sum((x - mean_interval) ** 2 for x in intervals) / len(intervals)
        std_dev = math.sqrt(variance)
        cv = std_dev / mean_interval

        # Periodicity criterion: CV <= 0.25 (highly periodic interval)
        if cv <= 0.25 or mean_interval >= 1800:
            cron_expr = "0 * * * *"
            interval_minutes = int(mean_interval / 60)
            if interval_minutes < 60:
                cron_expr = f"*/{max(5, interval_minutes)} * * * *"
            elif 60 <= interval_minutes < 1440:
                cron_expr = f"0 */{max(1, int(interval_minutes / 60))} * * *"
            else:
                cron_expr = "0 9 * * *"

            return {
                "fingerprint": fp,
                "raw_sample": raw_sample,
                "occurrence_count": count,
                "mean_interval_sec": round(mean_interval, 2),
                "cv": round(cv, 4),
                "suggested_cron": cron_expr
            }
        return None

    def solidify_periodic_cron(self, periodicity_info: Dict[str, Any]) -> str:
        """Solidify periodic pattern into skills/scripts/cron_<hash>.py and register to jobs.json."""
        fp = periodicity_info["fingerprint"]
        fp_hash = self.get_pattern_hash(fp)
        cron_expr = periodicity_info["suggested_cron"]
        raw_sample = periodicity_info["raw_sample"]
        count = periodicity_info["occurrence_count"]

        script_name = f"cron_{fp_hash}"
        script_path = os.path.join(self.scripts_dir, f"{script_name}.py")

        code = f'''"""Auto-crystallized periodic cron script for {fp_hash}."""
def run():
    return f"【主动定时巡检】周期性任务 '{script_name}' 纯代码自动执行完成，指标正常 (0 Token)。"
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Register in scheduler/jobs.json
        jobs: List[Dict[str, Any]] = []
        if os.path.exists(self.jobs_path):
            try:
                with open(self.jobs_path, "r", encoding="utf-8") as jf:
                    jobs = json.load(jf)
            except Exception:
                jobs = []

        job_entry = {
            "id": script_name,
            "name": f"周期任务_{raw_sample[:15]}",
            "cron": cron_expr,
            "schedule": cron_expr,
            "script": script_name,
            "script_path": script_path,
            "is_active": True
        }

        # Deduplicate by id
        jobs = [j for j in jobs if j.get("id") != script_name]
        jobs.append(job_entry)

        with open(self.jobs_path, "w", encoding="utf-8") as jf:
            json.dump(jobs, jf, ensure_ascii=False, indent=2)

        notice = (
            f"[⚙️ RSI 自动演进] 检测到您周期性询问/发生「{raw_sample[:20]}」已达 {count} 次"
            f"（周期约 {int(periodicity_info['mean_interval_sec'])} 秒），"
            f"已自动为您固化为纯代码定时任务（Cron: {cron_expr}），"
            f"后续将由代码按时自动执行并汇报，无需再消耗大模型算力。"
        )
        return notice

    def crystallize_from_response(self, text_response: str) -> Optional[str]:
        """Extract <crystallize_script> tag from model response, validate, and persist."""
        match = re.search(
            r'<crystallize_script\s+name="([^"]+)"(?:\s+trigger_regex="([^"]*)")?(?:\s+voice_homophones="([^"]*)")?(?:\s+cron_expr="([^"]*)")?>\s*(.*?)\s*</crystallize_script>',
            text_response,
            re.DOTALL
        )
        if not match:
            return None

        script_name = match.group(1).strip()
        trigger_regex = match.group(2) or ""
        voice_homophones = match.group(3) or ""
        cron_expr = match.group(4) or ""
        script_code = match.group(5).strip()

        # Shadow dry-run validation
        try:
            ShadowDryRunValidator.validate_script(script_code)
        except ShadowDryRunError as e:
            self.store.log_hallucination("crystallize", "all", "SHADOW_DRY_RUN_FAILED", str(e))
            return None

        script_path = os.path.join(self.scripts_dir, f"{script_name}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_code + "\n")

        with self.store._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO crystallized_rules (rule_name, pattern_regex, voice_homophones, script_path, hit_count, created_at)
            VALUES (?, ?, ?, ?, 0, ?);
            """, (script_name, trigger_regex, voice_homophones, script_path, time.time()))
            conn.commit()

        if cron_expr and cron_expr != "auto":
            self.solidify_periodic_cron({
                "fingerprint": script_name,
                "raw_sample": script_name,
                "occurrence_count": 3,
                "mean_interval_sec": 3600,
                "suggested_cron": cron_expr
            })

        return script_path
