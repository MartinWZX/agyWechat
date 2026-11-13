"""Engine Session Repository: owns per-user workspace, active engine state & conversation tracking."""
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

from memory.core_state_store import CoreStateStore


class EngineSessionRepository:
    """Persistence layer for Two-State Engine FSM (`working_sessions` table)."""

    def __init__(self, store: CoreStateStore):
        self.store = store

    @staticmethod
    def _session_id(user_id: str) -> str:
        return f"session_{user_id}"

    @staticmethod
    def _safe_json_list(raw: Any) -> List[Dict[str, Any]]:
        if not raw:
            return []
        try:
            val = json.loads(str(raw))
            return [x for x in val if isinstance(x, dict)] if isinstance(val, list) else []
        except ValueError:
            return []

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
        """Return persisted workspace_dir, active engine, command specs, command_map, and metadata."""
        sid = self._session_id(user_id)
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT active_engine, engine_commands_json, engine_meta_json, workspace_dir "
                "FROM working_sessions WHERE session_id = ?;",
                (sid,),
            )
            row = c.fetchone()
        if not row:
            return {"active_engine": "", "commands": [], "command_map": {}, "meta": {}, "workspace_dir": ""}
        active = str(row["active_engine"] or "").strip().lower()
        commands = self._safe_json_list(row["engine_commands_json"])
        command_map = {
            str(item.get("name", "")).lstrip("/").lower(): item
            for item in commands
            if item.get("name")
        }
        return {
            "active_engine": active,
            "commands": commands,
            "command_map": command_map,
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
        """Persist user's selected working directory without clearing existing session metadata."""
        sid = self._session_id(user_id)
        resolved = str(Path(workspace_dir).resolve())
        now = time.time()
        with self.store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO working_sessions
                    (session_id, user_id, created_at, updated_at, active_engine, engine_commands_json, engine_meta_json, workspace_dir)
                VALUES (?, ?, ?, ?, '', '[]', '{}', ?)
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
        commands: List[Dict[str, Any]],
        meta: Dict[str, Any],
    ) -> None:
        """Persist active engine entry & fresh conversation metadata while keeping selected workspace_dir."""
        sid = self._session_id(user_id)
        now = time.time()
        clean_meta = dict(meta)
        clean_meta.setdefault("conversation_id", "")
        clean_meta.setdefault("turn_count", 0)
        clean_meta.setdefault("session_started_at", now)
        cmd_json = json.dumps(commands, ensure_ascii=False)
        meta_json = json.dumps(clean_meta, ensure_ascii=False)
        with self.store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO working_sessions
                    (session_id, user_id, created_at, updated_at, active_engine, engine_commands_json, engine_meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    active_engine = excluded.active_engine,
                    engine_commands_json = excluded.engine_commands_json,
                    engine_meta_json = excluded.engine_meta_json;
                """,
                (sid, user_id, now, now, engine_id.lower(), cmd_json, meta_json),
            )
            conn.commit()

    def exit_engine(self, user_id: str) -> str:
        """Clear active engine and mapped commands while preserving selected workspace_dir."""
        prev = self.get_session_state(user_id).get("active_engine", "")
        sid = self._session_id(user_id)
        now = time.time()
        with self.store._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO working_sessions
                    (session_id, user_id, created_at, updated_at, active_engine, engine_commands_json, engine_meta_json)
                VALUES (?, ?, ?, ?, '', '[]', '{}')
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    active_engine = '',
                    engine_commands_json = '[]',
                    engine_meta_json = '{}';
                """,
                (sid, user_id, now, now),
            )
            conn.commit()
        return prev

    def update_session_config(self, user_id: str, key: str, value: str) -> Dict[str, str]:
        """Update a session-scoped engine CLI config parameter (e.g., model/mode/effort)."""
        sid = self._session_id(user_id)
        now = time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            cfg = meta.get("config")
            if not isinstance(cfg, dict):
                cfg = {}
            norm_key = key.strip().lower()
            norm_val = value.strip()
            if not norm_val or norm_val.lower() in ("default", "reset", "clear"):
                cfg.pop(norm_key, None)
            else:
                cfg[norm_key] = norm_val
            meta["config"] = cfg
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return {str(k): str(v) for k, v in cfg.items()}

    def update_conversation_state(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        reset: bool = False,
    ) -> Dict[str, Any]:
        """Persist or reset native CLI conversation_id, history list, and turn count."""
        sid = self._session_id(user_id)
        now = time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            history = list(meta.get("conversations") or [])
            if reset:
                meta["conversation_id"] = ""
                meta["turn_count"] = 0
                meta["title"] = ""
                meta["session_started_at"] = now
            else:
                if conversation_id:
                    meta["conversation_id"] = str(conversation_id).strip()
                meta["turn_count"] = int(meta.get("turn_count") or 0) + 1
                cid = str(meta.get("conversation_id") or "").strip()
                if cid:
                    history = [h for h in history if isinstance(h, dict) and h.get("id") != cid]
                    history.insert(0, {
                        "id": cid,
                        "title": str(meta.get("title") or ""),
                        "turn_count": meta["turn_count"],
                        "updated_at": now,
                    })
                    meta["conversations"] = history[:10]
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return meta

    def set_session_title(self, user_id: str, title: str) -> Dict[str, Any]:
        """Persist human-readable title for the current engine conversation."""
        sid = self._session_id(user_id)
        now = time.time()
        clean_title = title.strip()[:80]
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            meta["title"] = clean_title
            cid = str(meta.get("conversation_id") or "").strip()
            for item in meta.get("conversations") or []:
                if isinstance(item, dict) and item.get("id") == cid:
                    item["title"] = clean_title
                    item["updated_at"] = now
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return meta

    def fork_session(self, user_id: str, new_title: str = "") -> Dict[str, Any]:
        """Bookmark current thread into `conversations` and reset active conversation_id for a branch."""
        sid = self._session_id(user_id)
        now = time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            prev_cid = str(meta.get("conversation_id") or "").strip()
            history = list(meta.get("conversations") or [])
            if prev_cid:
                history = [h for h in history if isinstance(h, dict) and h.get("id") != prev_cid]
                history.insert(0, {
                    "id": prev_cid,
                    "title": str(meta.get("title") or ""),
                    "turn_count": int(meta.get("turn_count") or 0),
                    "updated_at": now,
                })
                meta["conversations"] = history[:10]
            meta["conversation_id"] = ""
            meta["turn_count"] = 0
            meta["title"] = new_title.strip()[:80]
            meta["session_started_at"] = now
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return {"prev_id": prev_cid, "meta": meta}

    def resume_conversation(self, user_id: str, selector: str) -> Optional[Dict[str, Any]]:
        """Switch active conversation_id back to a saved entry by 1-based index or ID prefix."""
        sid = self._session_id(user_id)
        now = time.time()
        sel = selector.strip()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            history = [h for h in (meta.get("conversations") or []) if isinstance(h, dict) and h.get("id")]
            chosen: Optional[Dict[str, Any]] = None
            if sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(history):
                    chosen = history[idx]
            else:
                for item in history:
                    if str(item["id"]).lower().startswith(sel.lower()):
                        chosen = item
                        break
            if not chosen:
                return None
            meta["conversation_id"] = str(chosen["id"])
            meta["turn_count"] = int(chosen.get("turn_count") or 0)
            meta["title"] = str(chosen.get("title") or "")
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return chosen

    def update_add_dirs(
        self, user_id: str, add_dir: Optional[str] = None, clear: bool = False
    ) -> List[str]:
        """Manage repeatable `--add-dir` extra directories in session config."""
        sid = self._session_id(user_id)
        now = time.time()
        with self.store._get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
            row = c.fetchone()
            meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
            cfg = meta.get("config")
            if not isinstance(cfg, dict):
                cfg = {}
            dirs: List[str] = [] if clear else [str(d) for d in (cfg.get("add_dirs") or []) if str(d).strip()]
            if not clear and add_dir:
                norm_dir = str(Path(add_dir).resolve())
                if norm_dir not in dirs:
                    dirs.append(norm_dir)
            cfg["add_dirs"] = dirs
            meta["config"] = cfg
            conn.execute(
                "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                (now, json.dumps(meta, ensure_ascii=False), sid),
            )
            conn.commit()
        return dirs
