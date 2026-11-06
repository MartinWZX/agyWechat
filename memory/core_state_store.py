"""SQLite WAL Mode State Store with Single-Writer Pattern & FTS5."""
import os
import sqlite3
import time
from typing import Dict, Any, List, Optional

class CoreStateStore:
    def __init__(self, db_path: str = "memory/agent_core.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
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
            # 3. Crystallized rules & jobs
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
                crystallized INTEGER DEFAULT 0
            );""")
            # 4. Engine Health & Wind Control Logs
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS engine_health_states (
                engine_id TEXT PRIMARY KEY,
                state TEXT, -- CLOSED, COOLDOWN_429, LOCKDOWN_403
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
            # 5. Hallucination Audits
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS hallucination_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                engine_id TEXT,
                hallucination_type TEXT, -- UNDER_EXECUTION, OVER_EXECUTION, SELF_CONTRADICTION, CORRUPTED_ASR
                details TEXT,
                reverted INTEGER DEFAULT 0,
                created_at REAL
            );""")
            # Default engines
            for engine in ["agy", "pi", "claude", "omp"]:
                cursor.execute("""
                INSERT OR IGNORE INTO engine_health_states (engine_id, state, cooldown_until, last_call_timestamp, total_calls_today, failover_count)
                VALUES (?, 'CLOSED', 0, 0, 0, 0);
                """, (engine,))
            conn.commit()

    def record_message(self, msg_id: str, session_id: str, user_id: str, msg_type: str,
                       raw_content: str, normalized_content: str, is_voice: bool,
                       reply_content: str, engine_used: str, offloaded: bool):
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO messages (msg_id, session_id, user_id, msg_type, raw_content, normalized_content, is_voice, reply_content, engine_used, offloaded, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (msg_id, session_id, user_id, msg_type, raw_content, normalized_content, 1 if is_voice else 0, reply_content, engine_used, 1 if offloaded else 0, time.time()))
            conn.commit()

    def search_episodic_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT m.memory_key, m.content, m.tags
            FROM episodic_memories_fts f
            JOIN episodic_memories m ON f.rowid = m.id
            WHERE episodic_memories_fts MATCH ?
            LIMIT ?;
            """, (query, limit))
            return [dict(row) for row in cursor.fetchall()]

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
                "offload_rate": round(offloaded_msgs / max(1, total_msgs), 4),
                "engines": engines,
                "hallucinations": h_stats
            }
