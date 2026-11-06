"""SQLite WAL Mode State Store with Single-Writer Pattern & FTS5."""
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


class CoreStateStore:
    """Core state store with WAL mode, FTS5 full-text search, and dual async/sync API."""

    def __init__(self, db_path: Union[str, Path] = "memory/agent_core.db"):
        self.db_path = str(db_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Sessions & Messages
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS working_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at REAL,
                updated_at REAL
            );""")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY,
                session_id TEXT,
                user_id TEXT,
                msg_type TEXT,
                raw_content TEXT,
                normalized_content TEXT,
                is_voice INTEGER DEFAULT 0,
                reply_content TEXT,
                engine_used TEXT,
                offloaded INTEGER DEFAULT 0,
                created_at REAL
            );""")
            # 2. Episodic Memories with FTS5
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT UNIQUE,
                content TEXT,
                tags TEXT,
                created_at REAL,
                updated_at REAL
            );""")
            cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memories_fts USING fts5(
                memory_key,
                content,
                tags,
                content='episodic_memories',
                content_rowid='id'
            );""")
            cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS episodic_memories_ai AFTER INSERT ON episodic_memories BEGIN
              INSERT INTO episodic_memories_fts(rowid, memory_key, content, tags) VALUES (new.id, new.memory_key, new.content, new.tags);
            END;""")
            cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS episodic_memories_ad AFTER DELETE ON episodic_memories BEGIN
              INSERT INTO episodic_memories_fts(episodic_memories_fts, rowid, memory_key, content, tags) VALUES('delete', old.id, old.memory_key, old.content, old.tags);
            END;""")
            cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS episodic_memories_au AFTER UPDATE ON episodic_memories BEGIN
              INSERT INTO episodic_memories_fts(episodic_memories_fts, rowid, memory_key, content, tags) VALUES('delete', old.id, old.memory_key, old.content, old.tags);
              INSERT INTO episodic_memories_fts(rowid, memory_key, content, tags) VALUES (new.id, new.memory_key, new.content, new.tags);
            END;""")
            # 3. Crystallized rules & interaction patterns
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS crystallized_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_name TEXT UNIQUE,
                pattern_regex TEXT,
                voice_homophones TEXT,
                script_path TEXT,
                hit_count INTEGER DEFAULT 0,
                created_at REAL
            );""")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS interaction_patterns (
                fingerprint TEXT PRIMARY KEY,
                raw_sample TEXT,
                occurrence_count INTEGER DEFAULT 1,
                last_seen REAL,
                crystallized INTEGER DEFAULT 0,
                timestamps_json TEXT DEFAULT '[]'
            );""")
            # 4. Cron Jobs, Tool & Skill Evolution History
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT,
                cron_expr TEXT,
                script_path TEXT,
                target_user TEXT,
                is_active INTEGER DEFAULT 1,
                last_run REAL DEFAULT 0,
                created_at REAL
            );""")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tool_evolution_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT,
                action TEXT, -- create, update, merge, deprecate
                version INTEGER,
                diff_summary TEXT,
                created_at REAL
            );""")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS skill_evolution_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT,
                version TEXT,
                patch_summary TEXT,
                created_at REAL
            );""")
            # 5. Engine Health & Wind Control Logs
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS engine_health_states (
                engine_id TEXT PRIMARY KEY,
                state TEXT,
                cooldown_until REAL DEFAULT 0,
                last_call_timestamp REAL DEFAULT 0,
                total_calls_today INTEGER DEFAULT 0,
                failover_count INTEGER DEFAULT 0
            );""")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS wind_control_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine_id TEXT,
                event_type TEXT,
                details TEXT,
                created_at REAL
            );""")
            # 6. Hallucination Audits
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS hallucination_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                engine_id TEXT,
                hallucination_type TEXT,
                details TEXT,
                reverted INTEGER DEFAULT 0,
                created_at REAL
            );""")

            for engine in ["agy", "pi", "claude", "omp"]:
                cursor.execute("""
                INSERT OR IGNORE INTO engine_health_states (engine_id, state, cooldown_until, last_call_timestamp, total_calls_today, failover_count)
                VALUES (?, 'CLOSED', 0, 0, 0, 0);
                """, (engine,))
            conn.commit()

    # ==========================================
    # Asynchronous API
    # ==========================================
    async def get_or_create_session(self, session_id: str, user_id: str) -> Dict[str, Any]:
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM working_sessions WHERE session_id = ?;", (session_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            cursor.execute(
                "INSERT INTO working_sessions (session_id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?);",
                (session_id, user_id, now, now)
            )
            conn.commit()
            return {"session_id": session_id, "user_id": user_id, "created_at": now, "updated_at": now}

    async def add_message(self, session_id: str, user_id: str, role: str, content: str) -> int:
        now = time.time()
        msg_id = f"msg_{int(now * 1000)}_{os.urandom(2).hex()}"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO messages (msg_id, session_id, user_id, msg_type, raw_content, normalized_content, is_voice, reply_content, engine_used, offloaded, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, '', 'UNKNOWN', 0, ?);
            """, (msg_id, session_id, user_id, role, content, content, now))
            conn.commit()
            return cursor.lastrowid or 1

    async def get_recent_messages(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT msg_id, session_id, user_id, msg_type, raw_content AS content, created_at FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?;",
                (session_id, limit)
            )
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def set_episodic_memory(self, user_id: str, category: str, key: str, value: str):
        now = time.time()
        memory_key = f"{user_id}:{category}:{key}"
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO episodic_memories (memory_key, content, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(memory_key) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at;
            """, (memory_key, value, f"{user_id},{category}", now, now))
            conn.commit()

    async def log_wind_control_call(self, reason: str = "call", duration_ms: float = 0.0, engine_id: str = "agy"):
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, 'CALL', ?, ?);",
                (engine_id, f"{reason} ({duration_ms}ms)", now)
            )
            conn.execute(
                "UPDATE engine_health_states SET last_call_timestamp = ?, total_calls_today = total_calls_today + 1 WHERE engine_id = ?;",
                (now, engine_id)
            )
            conn.commit()

    async def get_sliding_window_counts(self, engine_id: str = "agy") -> Tuple[int, int, float]:
        now = time.time()
        five_min_ago = now - 300
        one_hour_ago = now - 3600
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;",
                (engine_id, five_min_ago)
            )
            c5m = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;",
                (engine_id, one_hour_ago)
            )
            c1h = cursor.fetchone()[0]

            cursor.execute(
                "SELECT last_call_timestamp FROM engine_health_states WHERE engine_id = ?;",
                (engine_id,)
            )
            row = cursor.fetchone()
            last_call = row["last_call_timestamp"] if row else 0.0
            time_since = now - last_call if last_call > 0 else 9999.0

            return c5m, c1h, time_since

    async def get_active_rules(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM crystallized_rules ORDER BY id DESC;")
            return [dict(r) for r in cursor.fetchall()]

    def close(self):
        """No-op or cleanup for connection pool."""
        return True

    # ==========================================
    # Synchronous API
    # ==========================================
    def record_message(
        self,
        msg_id: str,
        session_id: str,
        user_id: str,
        msg_type: str,
        raw_content: str,
        normalized_content: str,
        is_voice: bool,
        reply_content: str,
        engine_used: str,
        offloaded: bool
    ):
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO messages (msg_id, session_id, user_id, msg_type, raw_content, normalized_content, is_voice, reply_content, engine_used, offloaded, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                msg_id, session_id, user_id, msg_type, raw_content, normalized_content,
                1 if is_voice else 0, reply_content, engine_used, 1 if offloaded else 0, time.time()
            ))
            conn.commit()

    async def search_episodic_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                SELECT m.memory_key, m.content, m.tags
                FROM episodic_memories_fts f
                JOIN episodic_memories m ON f.rowid = m.id
                WHERE episodic_memories_fts MATCH ?
                LIMIT ?;
                """, (query, limit))
                results = []
                for row in cursor.fetchall():
                    d = dict(row)
                    key_part = d["memory_key"].split(":")[-1] if ":" in d["memory_key"] else d["memory_key"]
                    d["key"] = key_part
                    results.append(d)
                return results
            except Exception:
                cursor.execute("""
                SELECT memory_key, content, tags FROM episodic_memories
                WHERE content LIKE ? OR memory_key LIKE ? LIMIT ?;
                """, (f"%{query}%", f"%{query}%", limit))
                results = []
                for row in cursor.fetchall():
                    d = dict(row)
                    key_part = d["memory_key"].split(":")[-1] if ":" in d["memory_key"] else d["memory_key"]
                    d["key"] = key_part
                    results.append(d)
                return results

    def log_hallucination(self, task_id: str, engine_id: str, h_type: str, details: str, reverted: bool = False):
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO hallucination_audit_logs (task_id, engine_id, hallucination_type, details, reverted, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (task_id, engine_id, h_type, details, 1 if reverted else 0, time.time()))
            conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*), SUM(offloaded), SUM(is_voice) FROM messages;")
            row = c.fetchone()
            total_msgs = row[0] or 0
            offloaded_msgs = row[1] or 0
            voice_msgs = row[2] or 0

            c.execute("SELECT engine_id, state, total_calls_today, failover_count FROM engine_health_states;")
            engines = {r["engine_id"]: dict(r) for r in c.fetchall()}

            c.execute("SELECT hallucination_type, COUNT(*) FROM hallucination_audit_logs GROUP BY hallucination_type;")
            h_stats = {r[0]: r[1] for r in c.fetchall()}

            return {
                "total_messages": total_msgs,
                "offloaded_messages": offloaded_msgs,
                "voice_messages": voice_msgs,
                "offload_rate": round(offloaded_msgs / max(1, total_msgs) * 100, 2),
                "engines": engines,
                "hallucinations": h_stats,
            }
