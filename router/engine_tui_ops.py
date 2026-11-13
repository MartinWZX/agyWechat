"""Interactive TUI & Workspace Operation Handlers for Active Engine Sessions."""
import asyncio
import json
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional, Tuple

from memory.core_state_store import CoreStateStore
from memory.engine_session_repository import EngineSessionRepository
from router.workspace_navigator import FORBIDDEN_DIR_NAMES, WorkspaceNavigator

MAX_OP_CHARS = 3000

TUI_ONLY_NOTES: Dict[str, str] = {
    "voice": "🎙️ 微信端已原生支持语音：直接向本公众号/微信号发送微信语音消息即可，网关会自动语音转写并执行。",
    "record": "🎙️ 微信端请直接按住发送微信语音消息，无需桌面麦克风录音。",
    "rewind": "⏪ 无头会话不支持按单条文本回退文件；可发送 /resume 选择历史会话节点，或用 /clear 开启新会话。",
    "feedback": "📝 如对本次引擎回答不满意，可直接在会话中回复纠正说明或发送 /learn 沉淀规范。",
    "keybindings": "⌨️ 终端快捷键仅在本地 CLI 交互界面生效；微信端请直接发送 /命令。",
    "statusline": "📊 终端底部状态栏仅在本地 TTY 生效；在微信端可发送 /context 查看当前完整会话状态。",
    "title": "🪟 终端窗口标题仅对本地 TTY 有效；如需重命名当前微信对话线程，请发送 /rename <名称>。",
    "logout": "🔐 为防止误清理本地认证凭据，登出操作请在宿主机终端执行 `agy` 后使用 `/logout`。",
}


async def _run_proc(argv: List[str], cwd: str, timeout: float = 12.0) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", errors="ignore"),
            err.decode("utf-8", errors="ignore"),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Timed out after {timeout}s"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OP_CHARS else text[:MAX_OP_CHARS] + "\n...（输出过长已截断）"


def _handle_add_dir(
    user_id: str, args_str: str, meta: Dict[str, Any], session_repo: EngineSessionRepository, workspace_nav: WorkspaceNavigator
) -> Dict[str, Any]:
    arg = args_str.strip()
    if not arg:
        dirs = (meta.get("config") or {}).get("add_dirs") or []
        body = "\n".join(f"• {d}" for d in dirs) if dirs else "（未添加额外目录）"
        return {"handled": True, "reply": f"📂 当前附加目录 (`--add-dir`)：\n{body}\n👉 用法：/add-dir <路径> 添加，/add-dir clear 清空。"}
    if arg.lower() in ("clear", "reset"):
        session_repo.update_add_dirs(user_id, clear=True)
        return {"handled": True, "reply": "✅ 已清空当前会话的所有 `--add-dir` 附加目录。"}
    ok, resolved, err = workspace_nav.validate_path(user_id, arg)
    if not ok or resolved is None:
        return {"handled": True, "reply": err}
    dirs = session_repo.update_add_dirs(user_id, add_dir=str(resolved))
    return {"handled": True, "reply": "✅ 已附加工作目录：\n" + "\n".join(f"• `{d}`" for d in dirs)}


def _handle_resume(user_id: str, args_str: str, meta: Dict[str, Any], session_repo: EngineSessionRepository) -> Dict[str, Any]:
    history = [h for h in (meta.get("conversations") or []) if isinstance(h, dict) and h.get("id")]
    if not args_str.strip():
        if not history:
            return {"handled": True, "reply": "暂无可恢复的历史会话线程（请先对话至少 1 轮后再用 /resume 查看或切换）。"}
        lines = ["【🕰️ 可恢复的会话线程列表】"]
        for i, item in enumerate(history, 1):
            label = f" | {item['title']}" if item.get("title") else ""
            lines.append(f"{i}. `{str(item['id'])[:12]}...` (轮数:{item.get('turn_count', 0)}{label})")
        lines.append("👉 发送 `/resume <序号或ID前缀>` 切换回去继续该对话。")
        return {"handled": True, "reply": "\n".join(lines)}
    chosen = session_repo.resume_conversation(user_id, args_str)
    if not chosen:
        return {"handled": True, "reply": f"⚠️ 未找到匹配 '{args_str}' 的历史会话。发送 /resume 查看列表。"}
    return {"handled": True, "reply": f"✅ 已切换至会话线程 `{chosen['id']}`（历史轮数: {chosen.get('turn_count', 0)}），下一条消息将继续上下文。"}


def _read_workspace_file(ws: str, raw_rel: str) -> str:
    target_root = Path(ws).resolve()
    rel = raw_rel.strip()
    if not rel:
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target_root.iterdir() if p.name not in FORBIDDEN_DIR_NAMES)
        return f"📂 `{target_root}` 文件：\n" + "\n".join(f"• {e}" for e in entries[:30]) + "\n👉 用法：/open <相对文件路径>"
    cand = (target_root / rel).resolve()
    if cand != target_root and target_root not in cand.parents:
        return "⛔ 安全限制：仅允许打开当前工作目录内的文件。"
    if any(part in FORBIDDEN_DIR_NAMES for part in cand.parts) or not cand.is_file():
        return f"⚠️ 文件不存在或不可读取：`{cand}`"
    return f"📄 `{rel}`：\n```\n{_clip(cand.read_text(encoding='utf-8', errors='ignore'))}\n```"


def _list_artifacts_or_projects(ws: str, kind: str) -> str:
    root = Path(ws).resolve()
    if kind == "projects":
        parent = root.parent if root.parent.exists() else root
        repos = [p.name for p in sorted(parent.iterdir()) if p.is_dir() and (p / ".git").exists()]
        cur = [p.name for p in sorted(root.iterdir()) if p.is_dir() and (p / ".git").exists()]
        return f"📁 当前目录内 Git 项目：{', '.join(cur) or '无'}\n📁 上级目录可用项目：{', '.join(repos[:20]) or '无'}\n👉 退出引擎（/exit）后用 /workspace <路径> 切换。"
    docs = [str(p.relative_to(root)) for p in sorted(root.rglob("*.md"))[:20] if ".git" not in p.parts]
    return "📑 工作区文档/产物清单：\n" + ("\n".join(f"• {d}" for d in docs) if docs else "（未发现 Markdown/产物文件）")


async def execute_tui_op(
    cmd_name: str,
    args_str: str,
    user_id: str,
    active_engine: str,
    ws: str,
    meta: Dict[str, Any],
    store: CoreStateStore,
    session_repo: EngineSessionRepository,
    workspace_nav: WorkspaceNavigator,
) -> Dict[str, Any]:
    """Execute interactive TUI/workspace command natively in WeChat."""
    if cmd_name == "add-dir":
        return _handle_add_dir(user_id, args_str, meta, session_repo, workspace_nav)

    if cmd_name == "btw":
        if not args_str.strip():
            return {"handled": True, "reply": "👉 用法：`/btw <附带问题>`（临时插问，不污染主对话线程）。"}
        return {"handled": False, "effective_query": args_str.strip(), "skip_conv": True}

    if cmd_name == "diff":
        rc, st, _ = await _run_proc(["git", "status", "--short"], ws)
        if rc != 0:
            return {"handled": True, "reply": f"ℹ️ `{ws}` 不是 Git 仓库或无法读取差异。"}
        _, stat, _ = await _run_proc(["git", "diff", "--stat"], ws)
        _, diff_txt, _ = await _run_proc(["git", "diff"], ws)
        body = f"【🔍 Git 工作区状态】\n{st.strip() or '（工作区干净）'}\n\n{stat.strip()}\n{diff_txt.strip()}".strip()
        return {"handled": True, "reply": _clip(body)}

    if cmd_name == "codesearch":
        q = args_str.strip()
        if not q or "\x00" in q:
            return {"handled": True, "reply": "👉 用法：`/codesearch <搜索词>`"}
        tool = ["rg", "-n", "--max-count=30", "--no-heading", "--", q, "."] if shutil.which("rg") else ["grep", "-rn", "-m", "30", "--", q, "."]
        rc, out, err = await _run_proc(tool, ws)
        if rc == 1 and not out.strip():
            return {"handled": True, "reply": f"🔍 未在 `{ws}` 中搜索到 `{q}`。"}
        return {"handled": True, "reply": _clip(f"🔍 搜索 `{q}` 结果：\n{(out or err).strip()}")}

    if cmd_name == "context":
        cfg = meta.get("config") or {}
        since = float(meta.get("session_started_at") or 0.0)
        hist = store.get_recent_exchanges(user_id, limit=20, since_ts=since)
        chars = sum(len(x.get("q", "")) + len(x.get("a", "")) for x in hist)
        return {
            "handled": True,
            "reply": (
                f"【🧠 {active_engine.upper()} 当前会话上下文】\n"
                f"• 工作目录：`{ws}`\n• 附加目录：{cfg.get('add_dirs') or '无'}\n"
                f"• 会话标题：{meta.get('title') or '未命名'} | Thread ID：`{meta.get('conversation_id') or '尚未创建'}`\n"
                f"• 生效参数：model={cfg.get('model') or '默认'}, mode={cfg.get('mode') or '默认'}, effort={cfg.get('effort') or '默认'}, agent={cfg.get('agent') or '默认'}\n"
                f"• 统计：轮数={meta.get('turn_count', 0)} | 近期消息={len(hist)} 条 ({chars} chars)"
            ),
        }

    if cmd_name == "copy":
        hist = store.get_recent_exchanges(user_id, limit=1, since_ts=float(meta.get("session_started_at") or 0.0))
        if not hist or not hist[-1].get("a"):
            return {"handled": True, "reply": "当前会话暂无可复制的最近一条 AI 回复。"}
        return {"handled": True, "reply": "📋 最近一条回复原文（长按即可复制）：\n\n" + hist[-1]["a"]}

    if cmd_name == "open":
        txt = await asyncio.to_thread(_read_workspace_file, ws, args_str)
        return {"handled": True, "reply": txt}

    if cmd_name == "rename":
        if not args_str.strip():
            return {"handled": True, "reply": f"当前会话标题：{meta.get('title') or '未命名'}\n👉 用法：`/rename <新标题>`"}
        updated = session_repo.set_session_title(user_id, args_str)
        return {"handled": True, "reply": f"✅ 当前会话已重命名为：{updated.get('title')}"}

    if cmd_name == "fork":
        res = session_repo.fork_session(user_id, new_title=args_str)
        return {"handled": True, "reply": f"🌿 已分叉开启新分支会话（原线程 `{res['prev_id'][:12] or 'none'}` 已保存，可用 `/resume` 切回）。"}

    if cmd_name == "resume":
        return _handle_resume(user_id, args_str, meta, session_repo)

    if cmd_name in ("artifact", "projects"):
        txt = await asyncio.to_thread(_list_artifacts_or_projects, ws, cmd_name)
        return {"handled": True, "reply": txt}

    if cmd_name == "tasks":
        jobs_path = Path(ws) / "scheduler" / "jobs.json"
        if jobs_path.is_file():
            raw = await asyncio.to_thread(jobs_path.read_text, "utf-8", "ignore")
            return {"handled": True, "reply": _clip(f"⏱️ 工作区任务配置 (`scheduler/jobs.json`)：\n{raw}")}
        return {"handled": True, "reply": "⏱️ 当前无进行中的本地后台调度任务。"}

    if cmd_name in TUI_ONLY_NOTES:
        return {"handled": True, "reply": TUI_ONLY_NOTES[cmd_name]}

    return {"handled": True, "reply": json.dumps({"command": cmd_name, "workspace": ws}, ensure_ascii=False)}
