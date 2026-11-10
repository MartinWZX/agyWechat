"""RSI Repository: the single owner of SQL for interaction patterns, crystallized rules and evolution history.

Upper layers (crystallizer / rsi / router) MUST use this repository instead of
reaching into `CoreStateStore._get_connection()` directly.

Rule lifecycle (status column):
    candidate -> active -> degraded -> (healed) candidate | retired
"""
import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from memory.core_state_store import CoreStateStore

RULE_STATUSES = ("candidate", "active", "degraded", "retired")


class RSIRepository:
    """Persistence gateway for the governed RSI crystallization lifecycle."""

    def __init__(self, store: CoreStateStore):
        self.store = store

    # ------------------------------------------------------------------
    # Interaction patterns
    # ------------------------------------------------------------------
    def observe_pattern(self, fingerprint: str, raw_sample: str, now: Optional[float] = None) -> Tuple[int, List[float]]:
        """Increment occurrence counter for a fingerprint and return (count, timestamps)."""
        ts_now = now if now is not None else time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT occurrence_count, timestamps_json FROM interaction_patterns WHERE fingerprint = ?;", (fingerprint,))
            row = c.fetchone()
            if row:
                count = int(row["occurrence_count"]) + 1
                ts_list = self._safe_json_list(row["timestamps_json"])
                ts_list.append(ts_now)
                ts_list = ts_list[-50:]
                c.execute(
                    "UPDATE interaction_patterns SET occurrence_count = ?, last_seen = ?, timestamps_json = ? WHERE fingerprint = ?;",
                    (count, ts_now, json.dumps(ts_list), fingerprint)
                )
            else:
                count = 1
                ts_list = [ts_now]
                c.execute(
                    "INSERT INTO interaction_patterns (fingerprint, raw_sample, occurrence_count, last_seen, crystallized, timestamps_json, answers_json) "
                    "VALUES (?, ?, 1, ?, 0, ?, '[]');",
                    (fingerprint, raw_sample, ts_now, json.dumps(ts_list))
                )
        return count, ts_list

    def get_pattern(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM interaction_patterns WHERE fingerprint = ?;", (fingerprint,))
            row = c.fetchone()
        if not row:
            return None
        data = dict(row)
        data["timestamps"] = self._safe_json_list(data.get("timestamps_json"))
        data["answers"] = self._safe_json_list(data.get("answers_json"))
        return data

    def append_pattern_answer(self, fingerprint: str, answer: str, keep: int = 5) -> Optional[Dict[str, Any]]:
        """Append an LLM answer to the pattern's rolling answer window; returns updated pattern."""
        pattern = self.get_pattern(fingerprint)
        if pattern is None:
            return None
        answers = pattern["answers"] + [answer]
        answers = answers[-keep:]
        with self.store._get_connection() as conn:
            conn.execute(
                "UPDATE interaction_patterns SET answers_json = ? WHERE fingerprint = ?;",
                (json.dumps(answers, ensure_ascii=False), fingerprint)
            )
        pattern["answers"] = answers
        return pattern

    def mark_pattern(self, fingerprint: str, crystallized: Optional[bool] = None, cron_proposed: Optional[bool] = None) -> None:
        with self.store._get_connection() as conn:
            if crystallized is not None:
                conn.execute("UPDATE interaction_patterns SET crystallized = ? WHERE fingerprint = ?;",
                             (1 if crystallized else 0, fingerprint))
            if cron_proposed is not None:
                conn.execute("UPDATE interaction_patterns SET cron_proposed = ? WHERE fingerprint = ?;",
                             (1 if cron_proposed else 0, fingerprint))

    # ------------------------------------------------------------------
    # Crystallized rules
    # ------------------------------------------------------------------
    def upsert_rule(
        self,
        rule_name: str,
        *,
        script_path: str,
        status: str = "candidate",
        pattern_regex: str = "",
        voice_homophones: str = "",
        fingerprint: str = "",
        fingerprint_hash: str = "",
        response_template: str = "",
        source: str = "auto",
    ) -> None:
        if status not in RULE_STATUSES:
            raise ValueError(f"Invalid rule status: {status}")
        now = time.time()
        with self.store._get_connection() as conn:
            conn.execute("""
            INSERT INTO crystallized_rules (rule_name, pattern_regex, voice_homophones, script_path, hit_count, created_at,
                                            status, fingerprint, fingerprint_hash, response_template, fail_count, source, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(rule_name) DO UPDATE SET
                pattern_regex = excluded.pattern_regex,
                voice_homophones = excluded.voice_homophones,
                script_path = excluded.script_path,
                status = excluded.status,
                fingerprint = excluded.fingerprint,
                fingerprint_hash = excluded.fingerprint_hash,
                response_template = excluded.response_template,
                fail_count = 0,
                source = excluded.source,
                updated_at = excluded.updated_at;
            """, (rule_name, pattern_regex, voice_homophones, script_path, now, status, fingerprint,
                  fingerprint_hash, response_template, source, now))

    def get_rule(self, rule_name: str) -> Optional[Dict[str, Any]]:
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM crystallized_rules WHERE rule_name = ?;", (rule_name,))
            row = c.fetchone()
        return dict(row) if row else None

    def list_rules(self, statuses: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        with self.store._get_connection() as conn:
            c = conn.cursor()
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                c.execute(f"SELECT * FROM crystallized_rules WHERE status IN ({placeholders}) ORDER BY id DESC;", tuple(statuses))
            else:
                c.execute("SELECT * FROM crystallized_rules ORDER BY id DESC;")
            return [dict(r) for r in c.fetchall()]

    def set_rule_status(self, rule_name: str, status: str) -> bool:
        if status not in RULE_STATUSES:
            raise ValueError(f"Invalid rule status: {status}")
        with self.store._get_connection() as conn:
            cur = conn.execute(
                "UPDATE crystallized_rules SET status = ?, updated_at = ?, fail_count = CASE WHEN ? = 'active' THEN 0 ELSE fail_count END WHERE rule_name = ?;",
                (status, time.time(), status, rule_name)
            )
            return cur.rowcount > 0

    def record_rule_hit(self, rule_name: str) -> None:
        with self.store._get_connection() as conn:
            conn.execute("UPDATE crystallized_rules SET hit_count = hit_count + 1 WHERE rule_name = ?;", (rule_name,))

    def record_rule_failure(self, rule_name: str) -> int:
        with self.store._get_connection() as conn:
            conn.execute("UPDATE crystallized_rules SET fail_count = fail_count + 1, updated_at = ? WHERE rule_name = ?;",
                         (time.time(), rule_name))
            c = conn.cursor()
            c.execute("SELECT fail_count FROM crystallized_rules WHERE rule_name = ?;", (rule_name,))
            row = c.fetchone()
        return int(row["fail_count"]) if row else 0

    def count_rules(self, status: Optional[str] = None) -> int:
        with self.store._get_connection() as conn:
            c = conn.cursor()
            if status:
                c.execute("SELECT COUNT(*) FROM crystallized_rules WHERE status = ?;", (status,))
            else:
                c.execute("SELECT COUNT(*) FROM crystallized_rules;")
            return int(c.fetchone()[0])

    # ------------------------------------------------------------------
    # Evolution history
    # ------------------------------------------------------------------
    def log_tool_evolution(self, tool_name: str, action: str, version: int, summary: str) -> None:
        with self.store._get_connection() as conn:
            conn.execute(
                "INSERT INTO tool_evolution_history (tool_name, action, version, diff_summary, created_at) VALUES (?, ?, ?, ?, ?);",
                (tool_name, action, version, summary, time.time())
            )

    def log_skill_evolution(self, skill_name: str, version: str, summary: str) -> None:
        with self.store._get_connection() as conn:
            conn.execute(
                "INSERT INTO skill_evolution_history (skill_name, version, patch_summary, created_at) VALUES (?, ?, ?, ?);",
                (skill_name, version, summary, time.time())
            )

    @staticmethod
    def _safe_json_list(raw: Any) -> List[Any]:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []
