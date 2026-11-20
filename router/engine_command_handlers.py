"""In-Engine Internal CLI/TUI Command Execution & Multi-Turn WeChat Interactive Handlers."""
import asyncio
import json
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from bridge.drivers.base_driver import extract_engine_json_result, resolve_binary, run_cli_capture
from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
from router.workspace_navigator import FORBIDDEN_DIR_NAMES, SKIP_LS_NAMES, WorkspaceNavigator

MAX_OUTPUT_CHARS = 3200
_SAFE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")
_SECRET_KEY_RE = re.compile(r"(token|secret|password|api_key|apikey|cookie|credential|private_key)", re.IGNORECASE)

INTERNAL_ENGINE_COMMANDS = frozenset({
    "resume", "new", "fork", "rename", "title", "rewind", "btw",
    "models", "model", "mode", "effort", "agents", "agent", "add-dir", "sandbox",
    "mcp", "plugin", "plugins", "remote-control", "changelog", "install", "update",
    "usage", "credits", "skills", "hooks", "permissions", "config", "settings",
    "memory", "doctor", "acp", "mic-serve",
    "diff", "open", "codesearch", "context", "copy", "artifact", "projects",
    "tasks", "feedback", "statusline", "keybindings", "voice", "record", "logout",
})

MODE_OPTIONS = ["auto", "plan", "accept-edits", "reset"]
EFFORT_OPTIONS = ["low", "medium", "high", "xhigh", "max", "reset"]
REMOTE_CONTROL_OPTIONS = ["status", "start", "stop"]
SANDBOX_OPTIONS = ["on", "off"]
CONFIG_MENU_MAP = {
    "1": "model",
    "2": "mode",
    "3": "effort",
    "4": "agent",
    "5": "sandbox",
    "6": "add-dir",
}

TUI_GUIDANCE_NOTES: Dict[str, str] = {
    "voice": "🎙️ 微信端已原生支持语音：进入引擎后直接发送微信语音消息（开启语音转文字）即可，无需桌面麦克风。",
    "record": "🎙️ 微信端请直接按住发送微信语音消息，无需本地录音设备。",
    "keybindings": "⌨️ 终端快捷键仅在本地 TTY 界面生效；微信端请直接发送 `/<命令>` 或通过 `/help` 查看指令。",
    "logout": "🔐 为避免远程误注销宿主机 CLI 凭证，登出操作请在宿主机本地终端执行原生 CLI `/logout`。",
}


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    txt = (text or "").strip()
    return txt if len(txt) <= limit else txt[:limit] + "\n...（输出过长已截断）"


def _mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if _SECRET_KEY_RE.search(str(k)) and v else _mask_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_secrets(item) for item in obj]
    return obj


def _outcome(
    reply: str,
    *,
    tier: str,
    active_engine: str,
    ws: str,
    handled: bool = True,
    effective_query: str = "",
    skip_conv: bool = False,
) -> Dict[str, Any]:
    return {
        "handled": handled,
        "reply": reply,
        "tier": tier,
        "preferred_engine": active_engine,
        "workspace_dir": ws,
        "effective_query": effective_query,
        "skip_conv": skip_conv,
    }


def _agy_home_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def _load_host_agy_conversations(workspace_dir: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Query host `agy` conversation_summaries.db when present, prioritizing current workspace."""
    db_path = _agy_home_dir() / "conversation_summaries.db"
    if not db_path.is_file():
        return []
    norm_ws = str(Path(workspace_dir).resolve())
    rows_out: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris "
                "FROM conversation_summaries "
                "WHERE COALESCE(killed, 0) = 0 "
                "ORDER BY last_modified_time DESC LIMIT 40;"
            )
            matched: List[Dict[str, Any]] = []
            others: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                cid = str(r["conversation_id"] or "").strip()
                if not cid:
                    continue
                uris = str(r["workspace_uris"] or "")
                item = {
                    "id": cid,
                    "title": str(r["title"] or r["preview"] or "").strip()[:60],
                    "turn_count": int(r["step_count"] or 0),
                    "updated_at": str(r["last_modified_time"] or ""),
                    "source": "host_agy",
                }
                if norm_ws and norm_ws in uris:
                    matched.append(item)
                else:
                    others.append(item)
            rows_out = (matched if matched else others)[:limit]
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return rows_out


def collect_resumable_conversations(
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Merge gateway-tracked threads and native host CLI conversation summaries without duplicates."""
    seen: set[str] = set()
    candidates: List[Dict[str, Any]] = []

    def _add(item: Dict[str, Any], is_current: bool = False) -> None:
        cid = str(item.get("id") or "").strip()
        if not cid or cid in seen:
            return
        seen.add(cid)
        candidates.append({
            "id": cid,
            "title": str(item.get("title") or "").strip(),
            "turn_count": int(item.get("turn_count") or 0),
            "source": str(item.get("source") or "gateway"),
            "is_current": is_current,
        })

    cur_cid = str(meta.get("conversation_id") or "").strip()
    if cur_cid:
        _add({
            "id": cur_cid,
            "title": str(meta.get("title") or ""),
            "turn_count": int(meta.get("turn_count") or 0),
            "source": "current",
        }, is_current=True)

    for item in meta.get("conversations") or []:
        if isinstance(item, dict):
            _add(item)

    by_eng = meta.get("conversations_by_engine")
    if isinstance(by_eng, dict):
        for item in by_eng.get(active_engine) or []:
            if isinstance(item, dict):
                _add(item)

    if active_engine == "agy" and len(candidates) < 10:
        for item in _load_host_agy_conversations(ws, limit=10 - len(candidates)):
            _add(item)

    return candidates[:10]


async def _probe_models_list(drv: Any, ws: str) -> Tuple[List[Dict[str, str]], str]:
    """Query `<binary> models` to obtain live available models."""
    resolved = resolve_binary(getattr(drv, "binary_path", "")) if drv else None
    if not resolved:
        return [], ""
    rc, out, err = await run_cli_capture([resolved, "models"], ws, timeout=10.0)
    raw = (out or "").strip()
    if rc != 0 or not raw or raw.startswith("{"):
        return [], (err or out or "").strip()
    models: List[Dict[str, str]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.lower().startswith("fetching "):
            continue
        parts = s.split("\t", 1) if "\t" in s else s.split(None, 1)
        mid = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else ""
        if mid:
            models.append({"id": mid, "label": label})
    return models, raw


async def _probe_agents_list(drv: Any, ws: str) -> List[str]:
    """Discover available subagents from CLI subcommand and local `.agents/` directories."""
    agents: List[str] = []
    resolved = resolve_binary(getattr(drv, "binary_path", "")) if drv else None
    if resolved:
        rc, out, _ = await run_cli_capture([resolved, "agents"], ws, timeout=6.0)
        if rc == 0 and out.strip():
            for line in out.splitlines():
                tok = line.strip().split()[0] if line.strip() else ""
                if tok and not tok.lower().startswith(("no ", "available", "usage")):
                    agents.append(tok)
    for base in (Path(ws) / ".agents", _agy_home_dir() / "agents"):
        if base.is_dir():
            for p in sorted(base.iterdir()):
                if p.suffix in (".md", ".yaml", ".json") or p.is_dir():
                    name = p.stem if p.is_file() else p.name
                    if name not in agents:
                        agents.append(name)
    for builtin in ("self", "research", "image-generator"):
        if builtin not in agents:
            agents.append(builtin)
    return agents[:20]


def _list_workspace_files(ws: str, limit: int = 25) -> List[str]:
    root = Path(ws).resolve()
    files: List[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name in SKIP_LS_NAMES or entry.name in FORBIDDEN_DIR_NAMES:
            continue
        if entry.is_file():
            files.append(entry.name)
        elif entry.is_dir():
            for sub in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                if sub.name in SKIP_LS_NAMES or sub.name in FORBIDDEN_DIR_NAMES:
                    continue
                if sub.is_file():
                    files.append(f"{entry.name}/{sub.name}")
                    if len(files) >= limit:
                        return files
        if len(files) >= limit:
            break
    return files


def _read_safe_file(ws: str, rel_or_path: str) -> str:
    root = Path(ws).resolve()
    raw = (rel_or_path or "").strip()
    if not raw or "\x00" in raw:
        return "⛔ 请提供合法的文件路径。"
    cand = Path(raw).expanduser()
    resolved = (cand if cand.is_absolute() else (root / cand)).resolve()
    if resolved != root and root not in resolved.parents:
        agy_brain = (_agy_home_dir() / "brain").resolve()
        if agy_brain not in resolved.parents:
            return "⛔ 安全限制：仅允许读取当前绑定工作区（或会话产物目录）内的文件。"
    if any(part in FORBIDDEN_DIR_NAMES for part in resolved.parts):
        return f"⛔ 安全限制：禁止访问凭据敏感目录 `{resolved}`。"
    if not resolved.is_file():
        return f"⚠️ 文件不存在：`{resolved}`"
    content = resolved.read_text(encoding="utf-8", errors="ignore")
    label = str(resolved.relative_to(root)) if root in resolved.parents else resolved.name
    return f"📄 `{label}`：\n```\n{_clip(content, 2800)}\n```"


def _list_artifacts(ws: str, conv_id: str) -> List[str]:
    results: List[str] = []
    root = Path(ws).resolve()
    for p in sorted(root.rglob("*.md")):
        if any(part in SKIP_LS_NAMES or part in FORBIDDEN_DIR_NAMES for part in p.parts):
            continue
        results.append(str(p.relative_to(root)))
        if len(results) >= 15:
            break
    if conv_id:
        brain_dir = _agy_home_dir() / "brain" / conv_id
        if brain_dir.is_dir():
            for p in sorted(brain_dir.glob("*.md")):
                results.append(str(p))
    return results[:20]


def _list_git_projects(ws: str) -> List[str]:
    root = Path(ws).resolve()
    repos: List[str] = []
    for base in (root, root.parent):
        if base.is_dir():
            for p in sorted(base.iterdir()):
                if p.is_dir() and (p / ".git").exists():
                    s = str(p.resolve())
                    if s not in repos:
                        repos.append(s)
    return repos[:20]


def _scan_skills(ws: str) -> List[Tuple[str, str]]:
    skills: List[Tuple[str, str]] = []
    seen: set[str] = set()
    candidates = [
        ("workspace", Path(ws) / ".agents" / "skills"),
        ("workspace", Path(ws) / ".claude" / "skills"),
        ("user", _agy_home_dir() / "skills"),
        ("builtin", _agy_home_dir() / "builtin" / "skills"),
    ]
    for scope, base in candidates:
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.is_dir() and (p / "SKILL.md").is_file() and p.name not in seen:
                seen.add(p.name)
                skills.append((p.name, scope))
    return skills


def _read_settings_summary(ws: str) -> str:
    sections: List[str] = []
    paths = [
        ("宿主全局 settings.json", _agy_home_dir() / "settings.json"),
        ("工作区 .gemini/settings.json", Path(ws) / ".gemini" / "settings.json"),
        ("工作区 .claude/settings.json", Path(ws) / ".claude" / "settings.json"),
    ]
    for label, p in paths:
        if p.is_file():
            try:
                parsed = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                masked = _mask_secrets(parsed)
                sections.append(f"• {label}：\n```json\n{_clip(json.dumps(masked, ensure_ascii=False, indent=2), 900)}\n```")
            except (OSError, ValueError):
                sections.append(f"• {label}：存在（非标准 JSON）")
    return "\n".join(sections) if sections else "（未检测到宿主或工作区 settings.json 配置文件）"


async def _run_subcmd_with_args(
    drv: Any,
    subcmd: str,
    args_str: str,
    ws: str,
    active_engine: str,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    resolved = resolve_binary(getattr(drv, "binary_path", "")) if drv else None
    if not resolved:
        return _outcome(
            f"⚠️ 当前未找到 `{active_engine}` 可执行文件。",
            tier=f"T0_ENGINE_SUBCMD:{active_engine}:{subcmd}",
            active_engine=active_engine,
            ws=ws,
        )
    if "\x00" in (args_str or ""):
        return _outcome(
            "⛔ 参数包含非法空字节。",
            tier=f"T0_ENGINE_SUBCMD:{active_engine}:{subcmd}",
            active_engine=active_engine,
            ws=ws,
        )
    try:
        tokens = shlex.split(args_str) if args_str.strip() else []
    except ValueError as exc:
        return _outcome(
            f"⛔ 参数解析失败：{exc}",
            tier=f"T0_ENGINE_SUBCMD:{active_engine}:{subcmd}",
            active_engine=active_engine,
            ws=ws,
        )
    argv = [resolved, subcmd, *tokens]
    rc, out, err = await run_cli_capture(argv, ws, timeout=timeout)
    detail = _clip((out or err or "").strip())
    header = f"【⚙️ {active_engine.upper()} · `{' '.join([subcmd, *tokens])}`】"
    if rc == 0:
        return _outcome(
            f"{header}\n{detail or '✅ 执行完成（无标准输出）。'}",
            tier=f"T0_ENGINE_SUBCMD:{active_engine}:{subcmd}",
            active_engine=active_engine,
            ws=ws,
        )
    return _outcome(
        f"⚠️ [{active_engine.upper()}] 子命令返回 (exit={rc}):\n{detail or '执行失败'}",
        tier=f"T0_ENGINE_SUBCMD:{active_engine}:{subcmd}",
        active_engine=active_engine,
        ws=ws,
    )


async def _handle_resume_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    session_repo: EngineSessionRepository,
    cached_candidates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    candidates = (
        list(cached_candidates)
        if cached_candidates
        else await asyncio.to_thread(collect_resumable_conversations, active_engine, ws, meta)
    )
    arg = args_str.strip()
    if not arg:
        if not candidates:
            return _outcome(
                "ℹ️ 暂无可恢复的历史会话线程（请先在当前引擎对话至少 1 轮后再使用 `/resume`）。",
                tier=f"T0_ENGINE_CMD:{active_engine}:resume",
                active_engine=active_engine,
                ws=ws,
            )
        session_repo.set_pending_interaction(user_id, {"command": "resume", "candidates": candidates})
        lines = [f"【🕰️ {active_engine.upper()} · 可恢复的历史会话】"]
        for i, item in enumerate(candidates, 1):
            cur_tag = " [当前]" if item.get("is_current") else ""
            title_tag = f" | {item['title']}" if item.get("title") else ""
            lines.append(f"{i}. `{item['id'][:16]}...` (轮/步数:{item['turn_count']}{cur_tag}){title_tag}")
        lines.append("")
        lines.append("👉 请直接回复【序号】（如 `1`）或【会话 ID 前缀】切换恢复（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:resume", active_engine=active_engine, ws=ws)

    chosen: Optional[Dict[str, Any]] = None
    low = arg.lower()
    if low in ("continue", "latest", "last", "-c"):
        if candidates:
            chosen = candidates[1] if len(candidates) > 1 and candidates[0].get("is_current") else candidates[0]
    elif arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(candidates):
            chosen = candidates[idx]
    else:
        for item in candidates:
            if item["id"].lower().startswith(low) or low in item["id"].lower():
                chosen = item
                break
        if chosen is None and re.match(r"^[a-zA-Z0-9._-]{4,}$", arg):
            chosen = {"id": arg, "title": "", "turn_count": 0}

    if chosen is None:
        return _outcome(
            f"⚠️ 未找到匹配的会话 `{arg}`。发送 `/resume` 查看可恢复会话列表。",
            tier=f"T0_ENGINE_CMD:{active_engine}:resume",
            active_engine=active_engine,
            ws=ws,
        )

    session_repo.activate_conversation(
        user_id,
        chosen["id"],
        title=str(chosen.get("title") or ""),
        turn_count=int(chosen.get("turn_count") or 0),
    )
    title_hint = f"（{chosen['title']}）" if chosen.get("title") else ""
    return _outcome(
        f"✅ [{active_engine.upper()}] 已恢复并绑定会话线程 `{chosen['id']}`{title_hint}。\n"
        "👉 下一条消息将自动携带该原生会话上下文继续执行。",
        tier=f"T0_ENGINE_CMD:{active_engine}:resume",
        active_engine=active_engine,
        ws=ws,
    )


async def _handle_model_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    drv: Any,
    session_repo: EngineSessionRepository,
    cached_options: Optional[List[str]] = None,
) -> Dict[str, Any]:
    arg = args_str.strip()
    cfg = meta.get("config") or {}
    current = str(cfg.get("model") or "默认 (inherit)")

    if not arg:
        models, _ = await _probe_models_list(drv, ws)
        option_ids = [m["id"] for m in models[:25]]
        session_repo.set_pending_interaction(user_id, {"command": "model", "options": option_ids})
        lines = [f"【🎛️ {active_engine.upper()} · /model】（当前：{current}）"]
        if models:
            for i, m in enumerate(models[:25], 1):
                lbl = f" — {m['label']}" if m.get("label") else ""
                lines.append(f"{i}. `{m['id']}`{lbl}")
            lines.append("")
            lines.append("👉 请回复【序号】、【模型 ID】或 `reset` 恢复默认（回复 `/cancel` 取消）。")
        else:
            lines.append("👉 请直接回复【目标模型 ID】或 `reset` 恢复默认（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:model", active_engine=active_engine, ws=ws)

    low = arg.lower()
    if low in ("reset", "default", "clear", "inherit"):
        session_repo.update_session_config(user_id, "model", "")
        return _outcome(f"✅ [{active_engine.upper()}] 已恢复默认模型 (`inherit`)。", tier=f"T0_ENGINE_CMD:{active_engine}:model", active_engine=active_engine, ws=ws)

    options = list(cached_options or [])
    if arg.isdigit():
        if not options:
            models, _ = await _probe_models_list(drv, ws)
            options = [m["id"] for m in models[:25]]
        idx = int(arg) - 1
        if 0 <= idx < len(options):
            target = options[idx]
        else:
            return _outcome(f"⛔ 序号 `{arg}` 超出可用模型范围，请重新选择或直接输入模型 ID。", tier=f"T0_ENGINE_CMD:{active_engine}:model", active_engine=active_engine, ws=ws)
    else:
        target = arg.split()[0].strip()

    if not _SAFE_TOKEN_RE.match(target):
        return _outcome(f"⛔ 非法的模型名称格式：`{target}`。", tier=f"T0_ENGINE_CMD:{active_engine}:model", active_engine=active_engine, ws=ws)

    session_repo.update_session_config(user_id, "model", target)
    return _outcome(
        f"✅ [{active_engine.upper()}] 当前会话模型已切换为：`{target}`。",
        tier=f"T0_ENGINE_CMD:{active_engine}:model",
        active_engine=active_engine,
        ws=ws,
    )


def _handle_enum_flag_cmd(
    cmd_name: str,
    options: List[str],
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    session_repo: EngineSessionRepository,
) -> Dict[str, Any]:
    arg = args_str.strip()
    cfg = meta.get("config") or {}
    current = str(cfg.get(cmd_name) or "默认")
    if not arg:
        session_repo.set_pending_interaction(user_id, {"command": cmd_name, "options": options})
        lines = [f"【⚙️ {active_engine.upper()} · /{cmd_name}】（当前：{current}）"]
        for i, opt in enumerate(options, 1):
            lines.append(f"{i}. `{opt}`")
        lines.append("")
        lines.append("👉 请回复【序号】或【选项名】（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:{cmd_name}", active_engine=active_engine, ws=ws)

    chosen = arg.split()[0].strip().lower()
    if chosen.isdigit():
        idx = int(chosen) - 1
        if 0 <= idx < len(options):
            chosen = options[idx]
        else:
            return _outcome(f"⛔ 序号 `{arg}` 无效，可选：{', '.join(options)}", tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}", active_engine=active_engine, ws=ws)

    if chosen in ("reset", "default", "clear", "off") and (cmd_name != "sandbox" or chosen != "off"):
        session_repo.update_session_config(user_id, cmd_name, "")
        return _outcome(f"✅ [{active_engine.upper()}] 已恢复默认 `/{cmd_name}`。", tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}", active_engine=active_engine, ws=ws)

    if chosen not in options:
        return _outcome(f"⛔ 无效选项 `{chosen}`，可选：{', '.join(options)}", tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}", active_engine=active_engine, ws=ws)

    if cmd_name == "sandbox":
        val: Any = chosen == "on"
        session_repo.update_session_config(user_id, "sandbox", val)
        return _outcome(f"✅ [{active_engine.upper()}] `--sandbox` 已{'开启' if val else '关闭'}。", tier=f"T0_ENGINE_CMD:{active_engine}:sandbox", active_engine=active_engine, ws=ws)

    session_repo.update_session_config(user_id, cmd_name, chosen)
    return _outcome(
        f"✅ [{active_engine.upper()}] 当前会话 `/{cmd_name}` 已设置为：`{chosen}`。",
        tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}",
        active_engine=active_engine,
        ws=ws,
    )


async def _handle_agent_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    drv: Any,
    session_repo: EngineSessionRepository,
    cached_options: Optional[List[str]] = None,
) -> Dict[str, Any]:
    arg = args_str.strip()
    cfg = meta.get("config") or {}
    current = str(cfg.get("agent") or "默认主智能体")
    if not arg:
        agents = await _probe_agents_list(drv, ws)
        session_repo.set_pending_interaction(user_id, {"command": "agent", "options": agents})
        lines = [f"【🤖 {active_engine.upper()} · /agent】（当前：{current}）"]
        for i, ag in enumerate(agents, 1):
            lines.append(f"{i}. `{ag}`")
        lines.append("")
        lines.append("👉 请回复【序号】、【智能体名称】或 `reset` 恢复默认（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:agent", active_engine=active_engine, ws=ws)

    low = arg.lower()
    if low in ("reset", "default", "clear"):
        session_repo.update_session_config(user_id, "agent", "")
        return _outcome(f"✅ [{active_engine.upper()}] 已恢复默认主智能体。", tier=f"T0_ENGINE_CMD:{active_engine}:agent", active_engine=active_engine, ws=ws)

    options = list(cached_options or [])
    if arg.isdigit():
        if not options:
            options = await _probe_agents_list(drv, ws)
        idx = int(arg) - 1
        if 0 <= idx < len(options):
            target = options[idx]
        else:
            return _outcome(f"⛔ 序号 `{arg}` 无效。", tier=f"T0_ENGINE_CMD:{active_engine}:agent", active_engine=active_engine, ws=ws)
    else:
        target = arg.split()[0].strip()

    if not _SAFE_TOKEN_RE.match(target):
        return _outcome(f"⛔ 非法的智能体名称：`{target}`。", tier=f"T0_ENGINE_CMD:{active_engine}:agent", active_engine=active_engine, ws=ws)

    session_repo.update_session_config(user_id, "agent", target)
    return _outcome(f"✅ [{active_engine.upper()}] 当前会话 `--agent` 已设置为：`{target}`。", tier=f"T0_ENGINE_CMD:{active_engine}:agent", active_engine=active_engine, ws=ws)


def _handle_add_dir_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    session_repo: EngineSessionRepository,
    workspace_nav: WorkspaceNavigator,
) -> Dict[str, Any]:
    arg = args_str.strip()
    if not arg:
        dirs = (meta.get("config") or {}).get("add_dirs") or []
        session_repo.set_pending_interaction(user_id, {"command": "add-dir"})
        body = "\n".join(f"• `{d}`" for d in dirs) if dirs else "（当前未附加额外目录）"
        return _outcome(
            f"【📂 {active_engine.upper()} · 附加工作目录 (`--add-dir`)】\n{body}\n\n"
            "👉 请直接回复要添加的【目录路径】，或回复 `clear` 清空附加目录（回复 `/cancel` 取消）。",
            tier=f"T0_ENGINE_INTERACT:{active_engine}:add-dir",
            active_engine=active_engine,
            ws=ws,
        )
    if arg.lower() in ("clear", "reset"):
        session_repo.update_add_dirs(user_id, clear=True)
        return _outcome("✅ 已清空当前会话的所有 `--add-dir` 附加目录。", tier=f"T0_ENGINE_CMD:{active_engine}:add-dir", active_engine=active_engine, ws=ws)
    ok, resolved, err = workspace_nav.validate_path(user_id, arg)
    if not ok or resolved is None:
        return _outcome(err, tier=f"T0_ENGINE_CMD:{active_engine}:add-dir", active_engine=active_engine, ws=ws)
    dirs = session_repo.update_add_dirs(user_id, add_dir=str(resolved))
    return _outcome(
        "✅ 已更新附加工作目录：\n" + "\n".join(f"• `{d}`" for d in dirs),
        tier=f"T0_ENGINE_CMD:{active_engine}:add-dir",
        active_engine=active_engine,
        ws=ws,
    )


def _handle_rewind_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    session_repo: EngineSessionRepository,
) -> Dict[str, Any]:
    arg = args_str.strip()
    since_ts = float(meta.get("session_started_at") or 0.0)
    exchanges = session_repo.get_session_exchanges(user_id, active_engine, since_ts=since_ts, limit=10)
    if not arg:
        if not exchanges:
            return _outcome("ℹ️ 当前会话暂无可回退的历史轮次。", tier=f"T0_ENGINE_CMD:{active_engine}:rewind", active_engine=active_engine, ws=ws)
        session_repo.set_pending_interaction(user_id, {"command": "rewind", "max_turns": len(exchanges)})
        lines = [f"【⏪ {active_engine.upper()} · /rewind】（共 {len(exchanges)} 轮）"]
        for i, ex in enumerate(exchanges, 1):
            preview = ex["prompt"].replace("\n", " ")[:48]
            lines.append(f"{i}. {preview}")
        lines.append("")
        lines.append("👉 请回复要【回退的轮数】（如 `1` 表示撤销最近 1 轮，回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:rewind", active_engine=active_engine, ws=ws)

    if not arg.isdigit() or int(arg) <= 0:
        return _outcome("⛔ 请输入正整数轮数（例如 `1`）。", tier=f"T0_ENGINE_CMD:{active_engine}:rewind", active_engine=active_engine, ws=ws)
    res = session_repo.rewind_session_turns(user_id, int(arg))
    return _outcome(
        f"⏪ [{active_engine.upper()}] 已从本地会话记录回退 {res['removed']} 轮（当前轮数：{res['turn_count']}）。\n"
        "💡 若需从全新原生上下文重新开始，请使用 `/new` 或 `/clear`。",
        tier=f"T0_ENGINE_CMD:{active_engine}:rewind",
        active_engine=active_engine,
        ws=ws,
    )


async def _handle_open_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    session_repo: EngineSessionRepository,
    cached_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    arg = args_str.strip()
    if not arg:
        files = await asyncio.to_thread(_list_workspace_files, ws, 25)
        if not files:
            return _outcome(f"📂 当前工作区 `{ws}` 暂无可读文件。", tier=f"T0_ENGINE_CMD:{active_engine}:open", active_engine=active_engine, ws=ws)
        session_repo.set_pending_interaction(user_id, {"command": "open", "files": files})
        lines = [f"【📄 {active_engine.upper()} · 选择要查看的文件】(`{ws}`)"]
        for i, fname in enumerate(files, 1):
            lines.append(f"{i}. `{fname}`")
        lines.append("")
        lines.append("👉 请回复【文件序号】或【相对文件路径】（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:open", active_engine=active_engine, ws=ws)

    target = arg
    if arg.isdigit():
        files = list(cached_files or await asyncio.to_thread(_list_workspace_files, ws, 25))
        idx = int(arg) - 1
        if 0 <= idx < len(files):
            target = files[idx]
        else:
            return _outcome(f"⛔ 文件序号 `{arg}` 无效。", tier=f"T0_ENGINE_CMD:{active_engine}:open", active_engine=active_engine, ws=ws)
    reply = await asyncio.to_thread(_read_safe_file, ws, target)
    return _outcome(reply, tier=f"T0_ENGINE_CMD:{active_engine}:open", active_engine=active_engine, ws=ws)


async def _handle_codesearch_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    session_repo: EngineSessionRepository,
) -> Dict[str, Any]:
    q = args_str.strip()
    if not q:
        session_repo.set_pending_interaction(user_id, {"command": "codesearch"})
        return _outcome(
            f"【🔍 {active_engine.upper()} · 工作区代码检索】\n👉 请直接回复要搜索的【关键词或正则】（回复 `/cancel` 取消）。",
            tier=f"T0_ENGINE_INTERACT:{active_engine}:codesearch",
            active_engine=active_engine,
            ws=ws,
        )
    if "\x00" in q:
        return _outcome("⛔ 搜索词包含非法空字节。", tier=f"T0_ENGINE_CMD:{active_engine}:codesearch", active_engine=active_engine, ws=ws)
    rg_bin = shutil.which("rg")
    argv = (
        [rg_bin, "-n", "--max-count=30", "--no-heading", "--", q, "."]
        if rg_bin
        else ["grep", "-rn", "-m", "30", "--exclude-dir=.git", "--exclude-dir=venv", "--exclude-dir=node_modules", "--", q, "."]
    )
    rc, out, err = await run_cli_capture(argv, ws, timeout=8.0)
    if rc == 1 and not out.strip():
        return _outcome(f"🔍 未在 `{ws}` 中搜索到 `{q}`。", tier=f"T0_ENGINE_CMD:{active_engine}:codesearch", active_engine=active_engine, ws=ws)
    return _outcome(_clip(f"🔍 `{q}` 搜索结果：\n{(out or err).strip()}"), tier=f"T0_ENGINE_CMD:{active_engine}:codesearch", active_engine=active_engine, ws=ws)


async def _handle_diff_cmd(args_str: str, active_engine: str, ws: str) -> Dict[str, Any]:
    rc, st, _ = await run_cli_capture(["git", "status", "--short"], ws, timeout=6.0)
    if rc != 0:
        return _outcome(f"ℹ️ `{ws}` 不是 Git 仓库或无法读取差异。", tier=f"T0_ENGINE_CMD:{active_engine}:diff", active_engine=active_engine, ws=ws)
    extra: List[str] = []
    if args_str.strip():
        try:
            tokens = shlex.split(args_str.strip())
            forbidden = ("-c", "--exec", "--ext-diff", "--output")
            for tok in tokens:
                if tok.startswith(forbidden):
                    return _outcome(f"⛔ 安全限制：`/diff` 不支持参数 `{tok}`。", tier=f"T0_ENGINE_CMD:{active_engine}:diff", active_engine=active_engine, ws=ws)
            extra = tokens
        except ValueError as exc:
            return _outcome(f"⛔ 参数解析失败：{exc}", tier=f"T0_ENGINE_CMD:{active_engine}:diff", active_engine=active_engine, ws=ws)
    _, stat, _ = await run_cli_capture(["git", "diff", "--stat", *extra], ws, timeout=6.0)
    _, diff_txt, _ = await run_cli_capture(["git", "diff", *extra], ws, timeout=6.0)
    parts = [f"【🔍 {active_engine.upper()} · Git 工作区差异】", st.strip() or "（工作区干净）"]
    if stat.strip():
        parts.append(stat.strip())
    if diff_txt.strip() and "--stat" not in extra:
        parts.append(diff_txt.strip())
    return _outcome(_clip("\n\n".join(parts)), tier=f"T0_ENGINE_CMD:{active_engine}:diff", active_engine=active_engine, ws=ws)


async def _handle_artifact_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    session_repo: EngineSessionRepository,
    cached_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    arg = args_str.strip()
    conv_id = str(meta.get("conversation_id") or "").strip()
    if not arg:
        files = await asyncio.to_thread(_list_artifacts, ws, conv_id)
        if not files:
            return _outcome("📑 当前工作区及会话目录暂未发现 Markdown 产物文档。", tier=f"T0_ENGINE_CMD:{active_engine}:artifact", active_engine=active_engine, ws=ws)
        session_repo.set_pending_interaction(user_id, {"command": "artifact", "files": files})
        lines = [f"【📑 {active_engine.upper()} · 文档与产物列表】"]
        for i, f in enumerate(files, 1):
            lines.append(f"{i}. `{f}`")
        lines.append("\n👉 请回复【序号】或【文件路径】查看内容（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:artifact", active_engine=active_engine, ws=ws)

    target = arg
    if arg.isdigit():
        files = list(cached_files or await asyncio.to_thread(_list_artifacts, ws, conv_id))
        idx = int(arg) - 1
        if 0 <= idx < len(files):
            target = files[idx]
        else:
            return _outcome(f"⛔ 产物序号 `{arg}` 无效。", tier=f"T0_ENGINE_CMD:{active_engine}:artifact", active_engine=active_engine, ws=ws)
    return _outcome(await asyncio.to_thread(_read_safe_file, ws, target), tier=f"T0_ENGINE_CMD:{active_engine}:artifact", active_engine=active_engine, ws=ws)


async def _handle_projects_cmd(
    user_id: str,
    args_str: str,
    active_engine: str,
    ws: str,
    session_repo: EngineSessionRepository,
    workspace_nav: WorkspaceNavigator,
    cached_projects: Optional[List[str]] = None,
) -> Dict[str, Any]:
    arg = args_str.strip()
    if not arg:
        repos = await asyncio.to_thread(_list_git_projects, ws)
        if not repos:
            return _outcome(f"📁 `{ws}` 及其父目录下暂未发现 Git 项目。", tier=f"T0_ENGINE_CMD:{active_engine}:projects", active_engine=active_engine, ws=ws)
        session_repo.set_pending_interaction(user_id, {"command": "projects", "projects": repos})
        lines = [f"【📁 {active_engine.upper()} · 可切换 Git 项目】（当前：`{ws}`）"]
        for i, r in enumerate(repos, 1):
            cur = " [当前]" if r == str(Path(ws).resolve()) else ""
            lines.append(f"{i}. `{r}`{cur}")
        lines.append("\n👉 请直接回复【项目序号】或【路径】切换当前会话工作目录（回复 `/cancel` 取消）。")
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:projects", active_engine=active_engine, ws=ws)

    target = arg
    if arg.isdigit():
        repos = list(cached_projects or await asyncio.to_thread(_list_git_projects, ws))
        idx = int(arg) - 1
        if 0 <= idx < len(repos):
            target = repos[idx]
        else:
            return _outcome(f"⛔ 项目序号 `{arg}` 无效。", tier=f"T0_ENGINE_CMD:{active_engine}:projects", active_engine=active_engine, ws=ws)

    ok, resolved, err = workspace_nav.validate_path(user_id, target)
    if not ok or resolved is None:
        return _outcome(err, tier=f"T0_ENGINE_CMD:{active_engine}:projects", active_engine=active_engine, ws=ws)
    saved = session_repo.set_workspace_dir(user_id, str(resolved))
    return _outcome(
        f"✅ [{active_engine.upper()}] 当前工作目录已切换至：`{saved}`。",
        tier=f"T0_ENGINE_CMD:{active_engine}:projects",
        active_engine=active_engine,
        ws=saved,
    )


async def _handle_usage_cmd(active_engine: str, ws: str, meta: Dict[str, Any], drv: Any) -> Dict[str, Any]:
    resolved = resolve_binary(getattr(drv, "binary_path", "")) if drv else None
    if resolved and active_engine in ("agy", "claude"):
        argv = [resolved, "--output-format", "json", "-p", "/usage"]
        rc, out, _ = await run_cli_capture(argv, ws, timeout=8.0)
        if rc == 0 and out.strip():
            txt, _ = extract_engine_json_result(out)
            if txt.strip():
                return _outcome(
                    f"【📊 {active_engine.upper()} · 用量与配额】\n{_clip(txt.strip())}",
                    tier=f"T0_ENGINE_CMD:{active_engine}:usage",
                    active_engine=active_engine,
                    ws=ws,
                )
    turns = int(meta.get("turn_count") or 0)
    cid = str(meta.get("conversation_id") or "尚未创建")
    return _outcome(
        f"【📊 {active_engine.upper()} · 会话统计】\n• 当前会话 ID：`{cid}`\n• 累计轮数：{turns}",
        tier=f"T0_ENGINE_CMD:{active_engine}:usage",
        active_engine=active_engine,
        ws=ws,
    )


async def dispatch_internal_engine_command(
    cmd_name: str,
    args_str: str,
    *,
    user_id: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    drv: Any,
    store: CoreStateStore,
    session_repo: EngineSessionRepository,
    workspace_nav: WorkspaceNavigator,
    pending_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute an internal engine CLI/TUI command or initiate multi-turn WeChat interactive prompt."""
    ctx = pending_ctx or {}

    if cmd_name == "resume":
        return await _handle_resume_cmd(
            user_id,
            args_str,
            active_engine,
            ws,
            meta,
            session_repo,
            cached_candidates=ctx.get("candidates"),
        )

    if cmd_name == "new":
        session_repo.update_conversation_state(user_id, reset=True)
        if args_str.strip():
            session_repo.set_session_title(user_id, args_str.strip())
        return _outcome(
            f"🆕 [{active_engine.upper()}] 已归档旧线程并开启全新会话（工作区：`{ws}`）。",
            tier=f"T0_ENGINE_CMD:{active_engine}:new",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "fork":
        res = session_repo.fork_session(user_id, new_title=args_str)
        prev = res.get("prev_id") or "无"
        return _outcome(
            f"🌿 [{active_engine.upper()}] 已从线程 `{prev}` 分叉开启新分支会话。\n👉 随时可发送 `/resume` 切回原分支。",
            tier=f"T0_ENGINE_CMD:{active_engine}:fork",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name in ("rename", "title"):
        if not args_str.strip():
            session_repo.set_pending_interaction(user_id, {"command": "rename"})
            cur_title = str(meta.get("title") or "未命名")
            return _outcome(
                f"【🏷️ {active_engine.upper()} · 会话重命名】（当前：{cur_title}）\n👉 请直接回复【新会话标题】（回复 `/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:rename",
                active_engine=active_engine,
                ws=ws,
            )
        updated = session_repo.set_session_title(user_id, args_str)
        return _outcome(
            f"✅ [{active_engine.upper()}] 当前会话标题已更新为：`{updated.get('title')}`。",
            tier=f"T0_ENGINE_CMD:{active_engine}:rename",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "rewind":
        return _handle_rewind_cmd(user_id, args_str, active_engine, ws, meta, session_repo)

    if cmd_name == "btw":
        q = args_str.strip()
        if not q:
            session_repo.set_pending_interaction(user_id, {"command": "btw"})
            return _outcome(
                f"【💬 {active_engine.upper()} · /btw 临时插问】\n👉 请直接回复【临时问题】（不会污染当前主会话 `conversation_id`，回复 `/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:btw",
                active_engine=active_engine,
                ws=ws,
            )
        session_repo.clear_pending_interaction(user_id)
        return _outcome(
            "",
            tier=f"T3_ENGINE_BTW:{active_engine}",
            active_engine=active_engine,
            ws=ws,
            handled=False,
            effective_query=q,
            skip_conv=True,
        )

    if cmd_name == "models":
        models, raw = await _probe_models_list(drv, ws)
        if models:
            lines = [f"【🧠 {active_engine.upper()} · 可用模型列表】"]
            for m in models:
                lbl = f" — {m['label']}" if m.get("label") else ""
                lines.append(f"• `{m['id']}`{lbl}")
            lines.append("\n👉 发送 `/model` 可交互式切换当前会话模型。")
            return _outcome("\n".join(lines), tier=f"T0_ENGINE_CMD:{active_engine}:models", active_engine=active_engine, ws=ws)
        return _outcome(raw or f"ℹ️ `{active_engine}` 未返回模型清单，可发送 `/model <模型名>` 手动设置。", tier=f"T0_ENGINE_CMD:{active_engine}:models", active_engine=active_engine, ws=ws)

    if cmd_name == "model":
        return await _handle_model_cmd(user_id, args_str, active_engine, ws, meta, drv, session_repo, cached_options=ctx.get("options"))

    if cmd_name == "mode":
        return _handle_enum_flag_cmd("mode", MODE_OPTIONS, user_id, args_str, active_engine, ws, meta, session_repo)

    if cmd_name == "effort":
        return _handle_enum_flag_cmd("effort", EFFORT_OPTIONS, user_id, args_str, active_engine, ws, meta, session_repo)

    if cmd_name == "sandbox":
        return _handle_enum_flag_cmd("sandbox", SANDBOX_OPTIONS, user_id, args_str, active_engine, ws, meta, session_repo)

    if cmd_name == "agents":
        agents = await _probe_agents_list(drv, ws)
        body = "\n".join(f"• `{a}`" for a in agents) if agents else "（仅默认主智能体）"
        return _outcome(
            f"【🤖 {active_engine.upper()} · 可用智能体】\n{body}\n\n👉 发送 `/agent` 可交互式切换 `--agent`。",
            tier=f"T0_ENGINE_CMD:{active_engine}:agents",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "agent":
        return await _handle_agent_cmd(user_id, args_str, active_engine, ws, meta, drv, session_repo, cached_options=ctx.get("options"))

    if cmd_name == "add-dir":
        return _handle_add_dir_cmd(user_id, args_str, active_engine, ws, meta, session_repo, workspace_nav)

    if cmd_name == "mcp":
        if not args_str.strip():
            res = await _run_subcmd_with_args(drv, "mcp", "list", ws, active_engine)
            session_repo.set_pending_interaction(user_id, {"command": "mcp"})
            return _outcome(
                res["reply"] + "\n\n👉 请直接回复 `mcp` 子命令参数（如 `help`、`add ...`、`remove ...`、`enable ...`、`disable ...`，回复 `/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:mcp",
                active_engine=active_engine,
                ws=ws,
            )
        return await _run_subcmd_with_args(drv, "mcp", args_str, ws, active_engine)

    if cmd_name in ("plugin", "plugins"):
        if not args_str.strip():
            res = await _run_subcmd_with_args(drv, "plugin", "list", ws, active_engine)
            session_repo.set_pending_interaction(user_id, {"command": "plugin"})
            return _outcome(
                res["reply"] + "\n\n👉 请直接回复 `plugin` 子命令参数（如 `list`、`enable <名>`、`disable <名>`、`install <源>`、`uninstall <名>`，回复 `/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:plugin",
                active_engine=active_engine,
                ws=ws,
            )
        return await _run_subcmd_with_args(drv, "plugin", args_str, ws, active_engine)

    if cmd_name == "remote-control":
        arg = args_str.strip()
        if not arg:
            res = await _run_subcmd_with_args(drv, "remote-control", "status", ws, active_engine)
            session_repo.set_pending_interaction(user_id, {"command": "remote-control", "options": REMOTE_CONTROL_OPTIONS})
            return _outcome(
                res["reply"] + "\n\n👉 请选择操作：`1. status` | `2. start` | `3. stop`（回复序号或名称，`/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:remote-control",
                active_engine=active_engine,
                ws=ws,
            )
        if arg.isdigit() and 1 <= int(arg) <= len(REMOTE_CONTROL_OPTIONS):
            arg = REMOTE_CONTROL_OPTIONS[int(arg) - 1]
        return await _run_subcmd_with_args(drv, "remote-control", arg, ws, active_engine)

    if cmd_name == "changelog":
        return await _run_subcmd_with_args(drv, "changelog", args_str, ws, active_engine)

    if cmd_name in ("install", "update"):
        eff = args_str.strip() or "--help"
        return await _run_subcmd_with_args(drv, cmd_name, eff, ws, active_engine)

    if cmd_name in ("acp", "mic-serve"):
        return await _run_subcmd_with_args(drv, cmd_name, "--help", ws, active_engine)

    if cmd_name in ("usage", "credits"):
        return await _handle_usage_cmd(active_engine, ws, meta, drv)

    if cmd_name == "skills":
        skills = await asyncio.to_thread(_scan_skills, ws)
        body = "\n".join(f"• `/{name}` ({scope})" for name, scope in skills) if skills else "（未扫描到自定义 Skill）"
        return _outcome(f"【🧩 {active_engine.upper()} · 已发现技能 (Skills)】\n{body}", tier=f"T0_ENGINE_CMD:{active_engine}:skills", active_engine=active_engine, ws=ws)

    if cmd_name in ("config", "settings"):
        arg = args_str.strip()
        if arg in CONFIG_MENU_MAP:
            return await dispatch_internal_engine_command(
                CONFIG_MENU_MAP[arg], "",
                user_id=user_id, active_engine=active_engine, ws=ws, meta=meta, drv=drv,
                store=store, session_repo=session_repo, workspace_nav=workspace_nav,
            )
        if "=" in arg:
            k, v = [p.strip() for p in arg.split("=", 1)]
            if k in ("model", "mode", "effort", "agent", "sandbox"):
                return await dispatch_internal_engine_command(
                    k, v,
                    user_id=user_id, active_engine=active_engine, ws=ws, meta=meta, drv=drv,
                    store=store, session_repo=session_repo, workspace_nav=workspace_nav,
                )
        cfg = meta.get("config") or {}
        settings_txt = await asyncio.to_thread(_read_settings_summary, ws)
        session_repo.set_pending_interaction(user_id, {"command": "config"})
        lines = [
            f"【⚙️ {active_engine.upper()} · 当前会话配置】",
            f"1. model: `{cfg.get('model') or '默认'}`",
            f"2. mode: `{cfg.get('mode') or '默认'}`",
            f"3. effort: `{cfg.get('effort') or '默认'}`",
            f"4. agent: `{cfg.get('agent') or '默认'}`",
            f"5. sandbox: `{'开启' if cfg.get('sandbox') else '关闭'}`",
            f"6. add-dir: `{cfg.get('add_dirs') or '无'}`",
            "",
            settings_txt,
            "",
            "👉 回复【序号 `1-6`】进入对应交互配置，或回复 `model=xxx`（回复 `/cancel` 取消）。",
        ]
        return _outcome("\n".join(lines), tier=f"T0_ENGINE_INTERACT:{active_engine}:config", active_engine=active_engine, ws=ws)

    if cmd_name in ("hooks", "permissions"):
        settings_txt = await asyncio.to_thread(_read_settings_summary, ws)
        return _outcome(
            f"【🛡️ {active_engine.upper()} · /{cmd_name}】\n"
            f"• 权限跳过标志：`{getattr(drv, 'skip_flag', '') or '未启用'}`\n"
            f"{settings_txt}",
            tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "memory":
        rule_files = []
        for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            p = Path(ws) / fname
            if p.is_file():
                rule_files.append(f"• `{fname}` ({p.stat().st_size} bytes)")
        body = "\n".join(rule_files) if rule_files else "（当前工作区无 AGENTS.md/CLAUDE.md/GEMINI.md）"
        return _outcome(
            f"【🧠 {active_engine.upper()} · 工作区规则与记忆】\n{body}\n• 当前会话轮数：{int(meta.get('turn_count') or 0)}",
            tier=f"T0_ENGINE_CMD:{active_engine}:memory",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "doctor":
        resolved = resolve_binary(getattr(drv, "binary_path", "")) if drv else None
        ver = await drv.probe_version(ws) if (drv and resolved) else ""
        rc_git, _, _ = await run_cli_capture(["git", "rev-parse", "--is-inside-work-tree"], ws, timeout=3.0)
        return _outcome(
            "\n".join([
                f"【🩺 {active_engine.upper()} · 环境诊断】",
                f"• 二进制路径：`{resolved or '未找到'}`",
                f"• 版本号：`{ver or '未知'}`",
                f"• 工作目录：`{ws}`（可写：{'是' if Path(ws).is_dir() else '否'}，Git 仓库：{'是' if rc_git == 0 else '否'}）",
                f"• 数据库：`{store.db_path}`",
            ]),
            tier=f"T0_ENGINE_CMD:{active_engine}:doctor",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "diff":
        return await _handle_diff_cmd(args_str, active_engine, ws)

    if cmd_name == "open":
        return await _handle_open_cmd(user_id, args_str, active_engine, ws, session_repo, cached_files=ctx.get("files"))

    if cmd_name == "codesearch":
        return await _handle_codesearch_cmd(user_id, args_str, active_engine, ws, session_repo)

    if cmd_name in ("context", "statusline"):
        cfg = meta.get("config") or {}
        since_ts = float(meta.get("session_started_at") or 0.0)
        exchanges = await asyncio.to_thread(session_repo.get_session_exchanges, user_id, active_engine, since_ts, 50)
        total_chars = sum(len(x["prompt"]) + len(x["reply"]) for x in exchanges)
        return _outcome(
            "\n".join([
                f"【🧠 {active_engine.upper()} · 当前会话上下文】",
                f"• 工作区：`{ws}`",
                f"• 附加目录 (`--add-dir`)：{cfg.get('add_dirs') or '无'}",
                f"• 会话标题：{meta.get('title') or '未命名'} | Thread ID：`{meta.get('conversation_id') or '尚未创建'}`",
                f"• 生效参数：model={cfg.get('model') or '默认'}, mode={cfg.get('mode') or '默认'}, effort={cfg.get('effort') or '默认'}, agent={cfg.get('agent') or '默认'}, sandbox={'开' if cfg.get('sandbox') else '关'}",
                f"• 会话统计：turn_count={int(meta.get('turn_count') or 0)} | 近期消息={len(exchanges)} 条 ({total_chars} chars)",
            ]),
            tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "copy":
        since_ts = float(meta.get("session_started_at") or 0.0)
        exchanges = await asyncio.to_thread(session_repo.get_session_exchanges, user_id, active_engine, since_ts, 5)
        for ex in reversed(exchanges):
            if ex["reply"].strip():
                return _outcome(
                    f"📋 最近一条回复原文（长按即可复制）：\n\n{ex['reply']}",
                    tier=f"T0_ENGINE_CMD:{active_engine}:copy",
                    active_engine=active_engine,
                    ws=ws,
                )
        return _outcome("ℹ️ 当前会话暂无可复制的最近一条 AI 回复。", tier=f"T0_ENGINE_CMD:{active_engine}:copy", active_engine=active_engine, ws=ws)

    if cmd_name == "artifact":
        return await _handle_artifact_cmd(user_id, args_str, active_engine, ws, meta, session_repo, cached_files=ctx.get("files"))

    if cmd_name == "projects":
        return await _handle_projects_cmd(user_id, args_str, active_engine, ws, session_repo, workspace_nav, cached_projects=ctx.get("projects"))

    if cmd_name == "tasks":
        cid = str(meta.get("conversation_id") or "").strip()
        task_file = (_agy_home_dir() / "brain" / cid / "task.md") if cid else None
        if task_file and task_file.is_file():
            txt = await asyncio.to_thread(task_file.read_text, "utf-8", "ignore")
            return _outcome(f"【📋 {active_engine.upper()} · 任务清单】\n{_clip(txt)}", tier=f"T0_ENGINE_CMD:{active_engine}:tasks", active_engine=active_engine, ws=ws)
        return _outcome(
            f"【📋 {active_engine.upper()} · 任务状态】\n• 当前工作区：`{ws}`\n• 已执行轮数：{int(meta.get('turn_count') or 0)}（当前无额外 task.md 文件）",
            tier=f"T0_ENGINE_CMD:{active_engine}:tasks",
            active_engine=active_engine,
            ws=ws,
        )

    if cmd_name == "feedback":
        fb = args_str.strip()
        if not fb:
            session_repo.set_pending_interaction(user_id, {"command": "feedback"})
            return _outcome(
                f"【📝 {active_engine.upper()} · 提交使用反馈】\n👉 请直接回复您的【反馈内容】（回复 `/cancel` 取消）。",
                tier=f"T0_ENGINE_INTERACT:{active_engine}:feedback",
                active_engine=active_engine,
                ws=ws,
            )
        fb_dir = Path(ws).resolve() / ".wechat_audit"
        fb_file = fb_dir / "feedback.jsonl"
        entry = json.dumps({"ts": time.time(), "user_id": user_id, "engine": active_engine, "feedback": fb}, ensure_ascii=False)

        def _write_fb() -> None:
            fb_dir.mkdir(parents=True, exist_ok=True)
            with open(fb_file, "a", encoding="utf-8") as fh:
                fh.write(entry + "\n")

        await asyncio.to_thread(_write_fb)
        session_repo.clear_pending_interaction(user_id)
        return _outcome(f"✅ 反馈已记录至 `{fb_file}`，感谢您的建议！", tier=f"T0_ENGINE_CMD:{active_engine}:feedback", active_engine=active_engine, ws=ws)

    return _outcome(
        TUI_GUIDANCE_NOTES.get(cmd_name, f"ℹ️ 命令 `/{cmd_name}` 仅在本地交互式终端生效。"),
        tier=f"T0_ENGINE_CMD:{active_engine}:{cmd_name}",
        active_engine=active_engine,
        ws=ws,
    )
