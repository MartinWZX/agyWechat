"""Engine Session Repository: owns per-user workspace, active engine state & native conversation_id."""
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional

from memory.core_state_store import CoreStateStore


_INTERNAL_CONTROL_COMMANDS = frozenset({
    "exit", "quit", "clear", "help", "cancel",
    "resume", "conversation", "new", "fork", "branch", "rename", "title", "rewind", "undo", "btw",
    "models", "model", "mode", "effort", "agents", "agent", "add-dir", "sandbox", "project",
    "mcp", "plugin", "plugins", "remote-control", "changelog", "install", "update",
    "usage", "quota", "credits", "skills", "hooks", "permissions", "config", "settings",
    "memory", "doctor", "acp", "mic-serve",
    "diff", "open", "codesearch", "cs", "context", "copy", "artifact", "projects",
    "tasks", "feedback", "statusline", "keybindings", "voice", "record", "logout",
})

_INTERNAL_REPLY_PREFIXES = (
    "【",
    "✅ [",
    "✅ 已",
    "✅ 反馈已记录",
    "⛔ ",
    "⚠️ [AGY] 子命令",
    "⚠️ [PI] 子命令",
    "⚠️ [CLAUDE] 子命令",
    "⚠️ [OMP] 子命令",
    "⚠️ 当前未找到",
    "⚠️ 未找到",
    "⚠️ 文件不存在",
    "⚠️ 暂无",
    "ℹ️ ",
    "🆕 ",
    "🌿 ",
    "⏪ ",
    "📄 `",
    "🔍 `",
    "🔍 未在",
    "📋 最近一条回复原文",
    "📋 最近一次 `/btw`",
    "📋 第 ",
    "🎙️ 微信端",
    "⌨️ 终端快捷键",
    "🔐 为避免",
)


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

    @staticmethod
    def _archive_active_thread(
        meta: Dict[str, Any], now: float, workspace_dir: str = "", active_engine: str = ""
    ) -> None:
        """Upsert current conversation_id into `conversations` and `conversations_by_engine`."""
        cid = str(meta.get("conversation_id") or "").strip()
        if not cid:
            return
        entry = {
            "id": cid,
            "title": str(meta.get("title") or "").strip(),
            "turn_count": int(meta.get("turn_count") or 0),
            "updated_at": now,
            "workspace_dir": workspace_dir or str(meta.get("workspace_dir") or ""),
            "engine_id": active_engine,
        }
        history = [
            h for h in (meta.get("conversations") or [])
            if isinstance(h, dict) and h.get("id") and h.get("id") != cid
        ]
        history.insert(0, entry)
        meta["conversations"] = history[:20]

        if active_engine:
            by_eng = meta.get("conversations_by_engine")
            if not isinstance(by_eng, dict):
                by_eng = {}
            eng_hist = [
                h for h in (by_eng.get(active_engine) or [])
                if isinstance(h, dict) and h.get("id") and h.get("id") != cid
            ]
            eng_hist.insert(0, entry)
            by_eng[active_engine] = eng_hist[:20]
            meta["conversations_by_engine"] = by_eng

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
        """Persist active engine entry & fresh conversation metadata while preserving saved engine threads."""
        sid = self._session_id(user_id)
        now = time.time()
        eng_low = engine_id.lower()
        clean_meta = dict(meta or {})
        clean_meta.setdefault("conversation_id", "")
        clean_meta.setdefault("turn_count", 0)
        clean_meta.setdefault("session_started_at", now)
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                prev_meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                prev_by_eng = prev_meta.get("conversations_by_engine")
                if isinstance(prev_by_eng, dict):
                    clean_meta["conversations_by_engine"] = prev_by_eng
                    if not clean_meta.get("conversations") and isinstance(prev_by_eng.get(eng_low), list):
                        clean_meta["conversations"] = list(prev_by_eng[eng_low])
                meta_json = json.dumps(clean_meta, ensure_ascii=False)
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
                    (sid, user_id, now, now, eng_low, meta_json),
                )
                conn.commit()

    def exit_engine(self, user_id: str) -> str:
        """Clear active engine while preserving selected workspace_dir and per-engine conversation history."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            state = self.get_session_state(user_id)
            prev = state.get("active_engine", "")
            meta = dict(state.get("meta") or {})
            self._archive_active_thread(
                meta, now, workspace_dir=state.get("workspace_dir", ""), active_engine=prev
            )
            preserved: Dict[str, Any] = {}
            if isinstance(meta.get("conversations_by_engine"), dict):
                preserved["conversations_by_engine"] = meta["conversations_by_engine"]
            preserved_json = json.dumps(preserved, ensure_ascii=False)
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO working_sessions
                        (session_id, user_id, created_at, updated_at, active_engine, engine_meta_json)
                    VALUES (?, ?, ?, ?, '', ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        active_engine = '',
                        engine_meta_json = excluded.engine_meta_json;
                    """,
                    (sid, user_id, now, now, preserved_json),
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
                c.execute(
                    "SELECT active_engine, engine_meta_json, workspace_dir FROM working_sessions WHERE session_id = ?;",
                    (sid,),
                )
                row = c.fetchone()
                active_eng = str(row["active_engine"] or "").strip().lower() if row else ""
                ws_dir = str(row["workspace_dir"] or "").strip() if row else ""
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                if reset:
                    self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                    meta["conversation_id"] = ""
                    meta["turn_count"] = 0
                    meta["title"] = ""
                    meta["session_started_at"] = now
                    meta.pop("pending_interaction", None)
                else:
                    if conversation_id:
                        meta["conversation_id"] = str(conversation_id).strip()
                    meta["turn_count"] = int(meta.get("turn_count") or 0) + 1
                    self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return meta

    def set_pending_interaction(self, user_id: str, payload: Dict[str, Any]) -> None:
        """Persist interactive command prompt state so the next WeChat reply completes the operation."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                meta["pending_interaction"] = dict(payload)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()

    def clear_pending_interaction(self, user_id: str) -> None:
        """Clear any pending interactive command prompt state."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                if "pending_interaction" in meta:
                    meta.pop("pending_interaction", None)
                    conn.execute(
                        "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                        (now, json.dumps(meta, ensure_ascii=False), sid),
                    )
                    conn.commit()

    def update_session_config(self, user_id: str, key: str, value: Any) -> Dict[str, Any]:
        """Set or clear a session CLI override flag (`model`, `mode`, `effort`, `agent`, `sandbox`)."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                cfg = meta.get("config")
                if not isinstance(cfg, dict):
                    cfg = {}
                if value is None or value is False or (isinstance(value, str) and not value.strip()):
                    cfg.pop(key, None)
                else:
                    cfg[key] = value.strip() if isinstance(value, str) else value
                meta["config"] = cfg
                meta.pop("pending_interaction", None)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return meta

    def update_add_dirs(
        self, user_id: str, add_dir: Optional[str] = None, clear: bool = False
    ) -> List[str]:
        """Manage repeatable `--add-dir` extra directories in session config."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
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
                meta.pop("pending_interaction", None)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return dirs

    def set_session_title(self, user_id: str, title: str) -> Dict[str, Any]:
        """Persist human-readable session title and sync matching thread entry."""
        sid = self._session_id(user_id)
        now = time.time()
        clean_title = (title or "").strip()[:80]
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT active_engine, engine_meta_json, workspace_dir FROM working_sessions WHERE session_id = ?;",
                    (sid,),
                )
                row = c.fetchone()
                active_eng = str(row["active_engine"] or "").strip().lower() if row else ""
                ws_dir = str(row["workspace_dir"] or "").strip() if row else ""
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                meta["title"] = clean_title
                meta.pop("pending_interaction", None)
                self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return meta

    def fork_session(self, user_id: str, new_title: str = "") -> Dict[str, Any]:
        """Bookmark current thread into `conversations` and start a clean forked branch."""
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT active_engine, engine_meta_json, workspace_dir FROM working_sessions WHERE session_id = ?;",
                    (sid,),
                )
                row = c.fetchone()
                active_eng = str(row["active_engine"] or "").strip().lower() if row else ""
                ws_dir = str(row["workspace_dir"] or "").strip() if row else ""
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                prev_cid = str(meta.get("conversation_id") or "").strip()
                self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                meta["forked_from"] = prev_cid
                meta["conversation_id"] = ""
                meta["turn_count"] = 0
                meta["title"] = new_title.strip()[:80]
                meta["session_started_at"] = now
                meta.pop("pending_interaction", None)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return {"prev_id": prev_cid, "meta": meta}

    def activate_conversation(
        self, user_id: str, conv_id: str, title: str = "", turn_count: int = 0
    ) -> Dict[str, Any]:
        """Switch active conversation_id to a selected resumable thread."""
        sid = self._session_id(user_id)
        now = time.time()
        clean_cid = conv_id.strip()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT active_engine, engine_meta_json, workspace_dir FROM working_sessions WHERE session_id = ?;",
                    (sid,),
                )
                row = c.fetchone()
                active_eng = str(row["active_engine"] or "").strip().lower() if row else ""
                ws_dir = str(row["workspace_dir"] or "").strip() if row else ""
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                meta["conversation_id"] = clean_cid
                meta["title"] = title.strip()[:80]
                meta["turn_count"] = int(turn_count or 0)
                meta["session_started_at"] = now
                meta.pop("pending_interaction", None)
                self._archive_active_thread(meta, now, workspace_dir=ws_dir, active_engine=active_eng)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return meta

    def set_last_btw_reply(self, user_id: str, reply: str) -> None:
        """Persist latest /btw answer in session metadata without mutating main conversation_id."""
        clean = (reply or "").strip()
        if not clean:
            return
        sid = self._session_id(user_id)
        now = time.time()
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT engine_meta_json FROM working_sessions WHERE session_id = ?;", (sid,))
                row = c.fetchone()
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                meta["last_btw_reply"] = clean
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()

    def get_session_exchanges(
        self,
        user_id: str,
        active_engine: str,
        since_ts: float = 0.0,
        limit: int = 20,
        only_ai_turns: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return recent message turns recorded for this user and active engine."""
        with self.store._get_connection() as conn:
            c = conn.cursor()
            fetch_limit = max(int(limit) * 6, 120) if only_ai_turns else int(limit)
            c.execute(
                "SELECT msg_id, normalized_content, reply_content, created_at "
                "FROM messages "
                "WHERE user_id = ? AND LOWER(engine_used) = LOWER(?) AND created_at >= ? "
                "AND normalized_content NOT IN ('/exit', '/quit', '/clear', '/help') "
                "ORDER BY created_at DESC LIMIT ?;",
                (user_id, active_engine, float(since_ts or 0.0), fetch_limit),
            )
            rows = list(reversed(c.fetchall()))

        if not only_ai_turns:
            return [
                {
                    "msg_id": r["msg_id"],
                    "prompt": str(r["normalized_content"] or ""),
                    "reply": str(r["reply_content"] or ""),
                    "created_at": float(r["created_at"] or 0.0),
                }
                for r in rows[-int(limit):]
            ]

        filtered: List[Dict[str, Any]] = []
        pending_interactive = False
        pending_btw_prompt = False
        for r in rows:
            prompt = str(r["normalized_content"] or "").strip()
            reply = str(r["reply_content"] or "").strip()
            low = prompt.lower()
            cmd_tok = prompt.split(None, 1)[0][1:].lower() if prompt.startswith("/") else ""
            asks_followup = ("`/cancel` 取消" in reply) or ("/cancel 取消" in reply) or ("请重新" in reply and "取消" in reply)

            if cmd_tok == "btw":
                pending_interactive = False
                if len(prompt.split(None, 1)) == 1:
                    pending_btw_prompt = True
                continue
            if low in ("cancel", "取消", "/cancel"):
                pending_btw_prompt = False
                pending_interactive = False
                continue
            if pending_btw_prompt and not prompt.startswith("/"):
                pending_btw_prompt = False
                pending_interactive = False
                continue
            if cmd_tok in _INTERNAL_CONTROL_COMMANDS:
                pending_btw_prompt = False
                pending_interactive = asks_followup
                continue
            if pending_interactive:
                pending_interactive = asks_followup
                continue
            if reply.startswith(_INTERNAL_REPLY_PREFIXES):
                pending_interactive = asks_followup
                continue
            filtered.append({
                "msg_id": r["msg_id"],
                "prompt": prompt,
                "reply": reply,
                "created_at": float(r["created_at"] or 0.0),
            })
        return filtered[-int(limit):]

    def rewind_session_turns(self, user_id: str, remove_count: int = 1) -> Dict[str, Any]:
        """Remove the last `remove_count` message turns from session log and adjust turn_count."""
        sid = self._session_id(user_id)
        now = time.time()
        n = max(1, int(remove_count))
        with self._lock:
            with self.store._get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT active_engine, engine_meta_json FROM working_sessions WHERE session_id = ?;",
                    (sid,),
                )
                row = c.fetchone()
                active_eng = str(row["active_engine"] or "").strip().lower() if row else ""
                meta = self._safe_json_dict(row["engine_meta_json"] if row else None)
                since_ts = float(meta.get("session_started_at") or 0.0)
            ai_exchanges = self.get_session_exchanges(
                user_id, active_eng, since_ts=since_ts, limit=max(n, 50), only_ai_turns=True
            )
            targets = ai_exchanges[-n:]
            removed_ids = [item["msg_id"] for item in targets]
            with self.store._get_connection() as conn:
                for mid in removed_ids:
                    conn.execute("DELETE FROM messages WHERE msg_id = ?;", (mid,))
                cur_turns = int(meta.get("turn_count") or 0)
                meta["turn_count"] = max(0, cur_turns - len(removed_ids))
                meta.pop("pending_interaction", None)
                conn.execute(
                    "UPDATE working_sessions SET updated_at = ?, engine_meta_json = ? WHERE session_id = ?;",
                    (now, json.dumps(meta, ensure_ascii=False), sid),
                )
                conn.commit()
        return {"removed": len(removed_ids), "turn_count": meta["turn_count"]}
