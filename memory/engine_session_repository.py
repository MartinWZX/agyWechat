"""Engine Session Repository: owns per-user workspace, active engine state & native conversation_id."""
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional

from memory.core_state_store import CoreStateStore


class EngineSessionRepository:
    """Minimal persistence layer for Two-State Engine FSM (`working_sessions` table)."""

    def __init__(self, store: CoreStateStore):
        self.store = store
        self._lock = threading.RLock()

    @staticmethod
    def _session_id(user_id: str) -> str:
        return f"session_{user_id}"

    @staticmethod
    def _safe_json_dict(raw: Any) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            val = json.loads(str(raw))
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}

    def get_session_state(self, user_id: str) -> Dict[str, Any]:
        """Return persisted workspace_dir, active engine, and lightweight session metadata."""
        sid = self._session_id(user_id)
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT active_engine, engine_meta_json, workspace_dir "
                "FROM working_sessions WHERE session_id = ?;",
                (sid,),
            )
            row = c.fetchone()
        if not row:
            return {"active_engine": "", "meta": {}, "workspace_dir": ""}
        active = str(row["active_engine"] or "").strip().lower()
        return {
            "active_engine": active,
            "meta": self._safe_json_dict(row["engine_meta_json"]),
            "workspace_dir": str(row["workspace_dir"] or "").strip(),
        }

    def get_workspace_dir(self, user_id: str, default_dir: str) -> str:
        """Return the validated workspace path for `user_id`, falling back to `default_dir`."""
        saved = self.get_session_state(user_id).get("workspace_dir", "")
        if saved and Path(saved).is_dir():
            return str(Path(saved).resolve())
        return str(Path(default_dir).resolve())

    def set_workspace_dir(self, user_id: str, workspace_dir: str) -> str:
        """Persist user's selected working directory without clearing active session state."""
        sid = self._session_id(user_id)
        resolved = str(Path(workspace_dir).resolve())
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO working_sessions
                        (session_id, user_id, created_at, updated_at, active_engine, engine_meta_json, workspace_dir)
                    VALUES (?, ?, ?, ?, '', '{}', ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        workspace_dir = excluded.workspace_dir;
                    """,
                    (sid, user_id, now, now, resolved),
                )
                conn.commit()
        return resolved

    def enter_engine(
        self,
        user_id: str,
        engine_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist active engine entry & fresh conversation metadata while keeping selected workspace_dir."""
        sid = self._session_id(user_id)
        now = time.time()
        clean_meta = dict(meta or {})
        clean_meta.setdefault("conversation_id", "")
        clean_meta.setdefault("turn_count", 0)
        clean_meta.setdefault("session_started_at", now)
        meta_json = json.dumps(clean_meta, ensure_ascii=False)
        with self._lock:
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO working_sessions
                        (session_id, user_id, created_at, updated_at, active_engine, engine_meta_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        active_engine = excluded.active_engine,
                        engine_meta_json = excluded.engine_meta_json;
                    """,
                    (sid, user_id, now, now, engine_id.lower(), meta_json),
                )
                conn.commit()

    def exit_engine(self, user_id: str) -> str:
        """Clear active engine and conversation metadata while preserving selected workspace_dir."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            prev = self.get_session_state(user_id).get("active_engine", "")
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO working_sessions
                        (session_id, user_id, created_at, updated_at, active_engine, engine_meta_json)
                    VALUES (?, ?, ?, ?, '', '{}')
                    ON CONFLICT(session_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        active_engine = '',
                        engine_meta_json = '{}';
                    """,
                    (sid, user_id, now, now),
                )
                conn.commit()
        return prev

    def update_conversation_state(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        reset: bool = False,
    ) -> Dict[str, Any]:
        """Persist or reset native CLI conversation_id and turn count."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                if reset:
                    meta["conversation_id"] = ""
                    meta["turn_count"] = 0
                    meta["session_started_at"] = now
                else:
                    if conversation_id:
                        meta["conversation_id"] = str(conversation_id).strip()
                    meta["turn_count"] = int(meta.get("turn_count") or 0) + 1
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return meta
