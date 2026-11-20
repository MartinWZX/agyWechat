"""SQLite WAL Mode Core State Store for Working Sessions and Message History."""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from memory.sqlite_utils import connect as sqlite_connect

_KEEP_TABLES = ("working_sessions", "messages")
_DEPRECATED_WS_COLUMNS = ("engine_commands_json", "discovered_commands_json")


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class CoreStateStore:
    """Lightweight SQLite WAL state store owning strictly two tables: `working_sessions` and `messages`."""

    def __init__(self, db_path: Union[str, Path] = "memory/agent_core.db"):
        self.db_path = str(db_path)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Open a self-closing WAL connection (see memory/sqlite_utils.ClosingConnection)."""
        return sqlite_connect(self.db_path, timeout=30.0)

    @staticmethod
    def _ensure_columns(cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]) -> None:
        """Idempotent additive schema migration for missing columns."""
        cursor.execute(f"PRAGMA table_info({_quote_ident(table)});")
        existing = {row[1] for row in cursor.fetchall()}
        for col_name, col_def in columns.items():
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(col_name)} {col_def};")

    @staticmethod
    def _purge_legacy_schema(conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> bool:
        """Drop all legacy zombie tables/FTS virtual tables/triggers/views and obsolete columns."""
        cleaned = False

        # Drop legacy triggers & views first so FTS/table drops never trigger dangling references.
        for obj_type, drop_kw in (("trigger", "TRIGGER"), ("view", "VIEW")):
            cursor.execute("SELECT name FROM sqlite_master WHERE type = ?;", (obj_type,))
            for (name,) in cursor.fetchall():
                if not str(name).startswith("sqlite_"):
                    cursor.execute(f"DROP {drop_kw} IF EXISTS {_quote_ident(name)};")
                    cleaned = True

        # Drop legacy VIRTUAL TABLEs (e.g., episodic_memories_fts) before regular tables so shadow tables go cleanly.
        cursor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT IN (?, ?) AND name NOT LIKE 'sqlite_%';",
            _KEEP_TABLES,
        )
        for name, sql in cursor.fetchall():
            if sql and "VIRTUAL TABLE" in str(sql).upper():
                cursor.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)};")
                cleaned = True

        # Drop any remaining non-core tables regardless of foreign keys.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN (?, ?) AND name NOT LIKE 'sqlite_%';",
            _KEEP_TABLES,
        )
        remaining = [row[0] for row in cursor.fetchall()]
        if remaining:
            cursor.execute("PRAGMA foreign_keys = OFF;")
            for name in remaining:
                cursor.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)};")
                cleaned = True
            cursor.execute("PRAGMA foreign_keys = ON;")

        # Drop deprecated command-discovery columns from existing working_sessions tables (SQLite >= 3.35).
        cursor.execute("PRAGMA table_info(working_sessions);")
        ws_cols = {row[1] for row in cursor.fetchall()}
        for col_name in _DEPRECATED_WS_COLUMNS:
            if col_name in ws_cols:
                cursor.execute(f"ALTER TABLE working_sessions DROP COLUMN {_quote_ident(col_name)};")
                cleaned = True

        # Check whether leftover sqlite_sequence remains after legacy AUTOINCREMENT tables were dropped.
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence';")
        has_seq = cursor.fetchone() is not None
        if has_seq:
            cleaned = True

        conn.commit()
        if cleaned:
            conn.execute("VACUUM;")
        return cleaned

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS working_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at REAL,
                updated_at REAL,
                active_engine TEXT DEFAULT '',
                engine_meta_json TEXT DEFAULT '{}',
                workspace_dir TEXT DEFAULT ''
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

            self._ensure_columns(cursor, "messages", {
                "msg_type": "TEXT DEFAULT 'text'",
                "raw_content": "TEXT DEFAULT ''",
                "normalized_content": "TEXT DEFAULT ''",
                "is_voice": "INTEGER DEFAULT 0",
                "reply_content": "TEXT DEFAULT ''",
                "engine_used": "TEXT DEFAULT 'NONE'",
                "offloaded": "INTEGER DEFAULT 0",
            })
            self._ensure_columns(cursor, "working_sessions", {
                "active_engine": "TEXT DEFAULT ''",
                "engine_meta_json": "TEXT DEFAULT '{}'",
                "workspace_dir": "TEXT DEFAULT ''",
            })
            self._purge_legacy_schema(conn, cursor)

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
        offloaded: bool = False,
    ) -> None:
        """Persist an inbound/outbound WeChat message exchange."""
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO messages (
                msg_id, session_id, user_id, msg_type, raw_content, normalized_content,
                is_voice, reply_content, engine_used, offloaded, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                msg_id,
                session_id,
                user_id,
                msg_type,
                raw_content,
                normalized_content,
                1 if is_voice else 0,
                reply_content,
                engine_used,
                1 if offloaded else 0,
                time.time(),
            ))
            conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        """Return real aggregated message counts and per-engine historical counts."""
        with self._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*), SUM(is_voice) FROM messages;")
            row = c.fetchone()
            total_msgs = int(row[0] or 0)
            voice_msgs = int(row[1] or 0)

            c.execute(
                "SELECT LOWER(engine_used) AS eng, COUNT(*) AS cnt FROM messages "
                "WHERE LOWER(engine_used) IN ('agy', 'pi', 'claude', 'omp') "
                "GROUP BY LOWER(engine_used);"
            )
            eng_counts = {str(r["eng"]): int(r["cnt"] or 0) for r in c.fetchall()}

        return {
            "total_messages": total_msgs,
            "voice_messages": voice_msgs,
            "engine_message_counts": eng_counts,
        }

    def export_engine_audit(
        self,
        engine_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Aggregate fine-grained per-engine command audit metrics and recent execution records."""
        target_engine = (engine_id or "").strip().lower() or None
        valid_engines = ("agy", "pi", "claude", "omp")
        if target_engine and target_engine not in valid_engines:
            raise ValueError(f"Unsupported engine '{target_engine}'")

        engines_scope = (target_engine,) if target_engine else valid_engines
        effective_limit = max(1, min(int(limit), 1000))

        with self._get_connection() as conn:
            c = conn.cursor()
            engine_summaries: Dict[str, Dict[str, Any]] = {}
            for eid in engines_scope:
                where_parts = ["LOWER(engine_used) = ?"]
                params: List[Any] = [eid]
                if user_id:
                    where_parts.append("user_id = ?")
                    params.append(user_id)
                where_sql = " AND ".join(where_parts)
                c.execute(
                    f"SELECT "
                    f"COUNT(*) AS total_calls, "
                    f"COALESCE(SUM(CASE WHEN is_voice = 1 THEN 1 ELSE 0 END), 0) AS voice_calls, "
                    f"COALESCE(SUM(CASE WHEN is_voice = 0 THEN 1 ELSE 0 END), 0) AS text_calls, "
                    f"COALESCE(SUM(CASE WHEN LTRIM(normalized_content) LIKE '/%' THEN 1 ELSE 0 END), 0) AS slash_calls, "
                    f"COUNT(DISTINCT user_id) AS unique_users, "
                    f"MAX(created_at) AS last_active_at "
                    f"FROM messages WHERE {where_sql};",
                    tuple(params),
                )
                agg = c.fetchone()
                engine_summaries[eid] = {
                    "total_calls": int(agg["total_calls"] or 0),
                    "voice_calls": int(agg["voice_calls"] or 0),
                    "text_calls": int(agg["text_calls"] or 0),
                    "slash_calls": int(agg["slash_calls"] or 0),
                    "unique_users": int(agg["unique_users"] or 0),
                    "last_active_at": agg["last_active_at"],
                }

            rec_where = [
                "LOWER(engine_used) = ?"
                if target_engine
                else "LOWER(engine_used) IN ('agy', 'pi', 'claude', 'omp')"
            ]
            rec_params: List[Any] = [target_engine] if target_engine else []
            if user_id:
                rec_where.append("user_id = ?")
                rec_params.append(user_id)
            rec_params.append(effective_limit)

            c.execute(
                f"SELECT msg_id, session_id, user_id, msg_type, normalized_content, is_voice, "
                f"reply_content, engine_used, created_at "
                f"FROM messages WHERE {' AND '.join(rec_where)} "
                f"ORDER BY created_at DESC LIMIT ?;",
                tuple(rec_params),
            )
            records = []
            for r in c.fetchall():
                cmd_text = str(r["normalized_content"] or "")
                reply_text = str(r["reply_content"] or "")
                records.append({
                    "msg_id": r["msg_id"],
                    "session_id": r["session_id"],
                    "user_id": r["user_id"],
                    "engine_used": str(r["engine_used"] or "").lower(),
                    "msg_type": r["msg_type"],
                    "is_voice": bool(r["is_voice"]),
                    "command": cmd_text,
                    "is_slash_command": cmd_text.lstrip().startswith("/"),
                    "reply_length": len(reply_text),
                    "reply_preview": reply_text[:200],
                    "created_at": r["created_at"],
                })

            sess_sql = "SELECT user_id, active_engine, workspace_dir, updated_at FROM working_sessions"
            sess_params: List[Any] = []
            if user_id:
                sess_sql += " WHERE user_id = ?"
                sess_params.append(user_id)
            sess_sql += " ORDER BY updated_at DESC;"
            c.execute(sess_sql, tuple(sess_params))
            active_sessions = [
                {
                    "user_id": s["user_id"],
                    "active_engine": s["active_engine"] or "",
                    "workspace_dir": s["workspace_dir"] or "",
                    "updated_at": s["updated_at"],
                }
                for s in c.fetchall()
            ]

        return {
            "generated_at": time.time(),
            "filter_engine": target_engine or "all",
            "filter_user": user_id or "all",
            "engine_summaries": engine_summaries,
            "active_sessions": active_sessions,
            "record_count": len(records),
            "records": records,
        }

    def export_engine_audit_to_file(
        self,
        workspace_dir: Union[str, Path],
        engine_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[Path, Dict[str, Any]]:
        """Persist fine-grained per-engine command audit JSON into `<workspace_dir>/.wechat_audit/`."""
        audit = self.export_engine_audit(engine_id=engine_id, user_id=user_id, limit=limit)
        audit_dir = Path(workspace_dir).resolve() / ".wechat_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        tag = (engine_id or "all").strip().lower() or "all"
        out_path = audit_dir / f"engine_audit_{tag}.json"
        out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_path, audit

    def close(self) -> bool:
        """Flush SQLite WAL frames into the main database file."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        return True
