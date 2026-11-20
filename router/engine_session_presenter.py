"""Formatting helpers for Pure Two-State EngineSessionRouter."""
from typing import Any, Dict, List, Optional

NON_ENGINE_HELP_TEXT = (
    "【🧭 agyWechat 网关路由状态 · 可用文本命令清单】\n"
    "⚠️ 当前处于网关路由状态：不支持语音控制，仅接受以下纯文本命令：\n"
    "1. 查看帮助说明：/help\n"
    "2. 宿主电脑状态：/status（CPU 负载、内存使用率、磁盘剩余空间、运行时长与已安装引擎）\n"
    "3. 查看四大引擎：/engines（本机安装状态、版本号、可执行路径与当前会话状态）\n"
    "4. 导出引擎审计：/export [引擎名] 或 /engines audit [引擎名]（生成每引擎指令调用明细 JSON）\n"
    "5. 查看当前工作目录：/pwd\n"
    "6. 校验并切换工作目录：/workspace <路径>\n"
    "7. 列出工作目录文件：/ls [子目录]\n"
    "8. 进入 AI 引擎会话：/agy | /pi | /claude | /omp\n"
    "📎 发送图片或文件片段将自动安全落盘至当前绑定工作区的 `.wechat_assets/` 目录。\n"
    "👉 进入任意引擎后，即可解锁微信语音与文字 100% 原生直通（引擎内仅保留 /exit、/clear、/help 三个网关控制命令）。"
)


def format_engines_summary(
    rows: List[Dict[str, Any]],
    active_engine: str = "",
    workspace_dir: str = "",
) -> str:
    """Format four-engine table: engine name | install & version | binary path | session state."""
    lines = ["【🔌 四大 AI 引擎状态清单】"]
    for row in rows:
        eid = row["engine_id"]
        ver = f" ({row['version']})" if row.get("version") else ""
        inst = f"🟢 已安装{ver}" if row.get("installed") else "⚪ 未安装"
        path_str = row.get("resolved") or f"未找到 ({row.get('binary', eid)})"
        sess_status = "🟢 当前会话已进入" if active_engine == eid else "⚪ 待机（未进入）"
        lines.append(
            f"• /{eid} | {inst} | 路径: {path_str} | 会话状态: {sess_status}"
        )

    bind_engine = f"[{active_engine.upper()}] 引擎态" if active_engine else "未进入任何引擎（非引擎网关路由态）"
    lines.append("")
    lines.append(f"🧭 当前全局状态: {bind_engine}")
    if workspace_dir:
        lines.append(f"📁 当前工作目录: {workspace_dir}")
    lines.append("👉 发送 /agy、/pi、/claude 或 /omp 进入对应引擎；发送 /status 查看宿主电脑硬件指标。")
    return "\n".join(lines)


def format_entry_summary(
    engine_id: str,
    version: str = "",
    workspace_dir: str = "",
    conversation_id: str = "",
    supports_native_session: bool = False,
) -> str:
    """Render concise welcome panel immediately upon entering an engine."""
    eid = engine_id.upper()
    ver_str = version or "未知"
    if conversation_id:
        conv_status = f"继续原生线程 `{conversation_id}`"
    elif supports_native_session:
        conv_status = "新会话（尚无 conversation_id，首轮对话将自动生成）"
    else:
        conv_status = "独立直通模式（无固定 conversation_id）"

    lines = [
        f"✅ 已进入 [{eid}] 引擎原生会话（版本: {ver_str}）",
        f"📂 当前工作目录：`{workspace_dir or '.'}`",
        f"🧵 会话状态：{conv_status}",
        "🎛️ 网关控制：/exit（退出）、/clear（清空重开）、/help（查看帮助与引擎内部命令清单）",
        "🛠️ 引擎内部命令：支持 /resume、/new、/fork、/model、/mode、/effort、/agent、/add-dir、/diff、/open、/codesearch、/mcp、/plugin、/usage 等（无参触发交互式询问）。",
        "💡 其余所有自然语言、微信语音及 AI 工作流斜杠命令（如 /plan、/review）均 100% 原生透传 CLI。",
    ]
    return "\n".join(lines)


def format_in_engine_help(
    engine_id: str,
    workspace_dir: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Render minimal in-engine gateway help showing bound engine, workspace, and 3 control commands."""
    info = meta or {}
    ver = str(info.get("version") or "未知")
    conv_id = str(info.get("conversation_id") or "")
    turns = int(info.get("turn_count") or 0)
    ws_line = workspace_dir or str(info.get("workspace_dir") or ".")
    conv_line = f"`{conv_id}`（已对话 {turns} 轮）" if conv_id else f"新会话（已对话 {turns} 轮）"

    cfg = info.get("config") if isinstance(info.get("config"), dict) else {}
    flags_line = ", ".join(
        f"{k}={v}" for k, v in cfg.items() if v and k != "add_dirs"
    ) or "默认"

    return "\n".join([
        f"【📘 {engine_id.upper()} 引擎态 · 控制与内部命令说明】",
        f"⚙️ 当前引擎：{engine_id.upper()}（版本: {ver}）",
        f"📂 绑定工作目录：`{ws_line}`",
        f"🧵 conversation_id：{conv_line}",
        f"🎛️ 会话参数：{flags_line}",
        "",
        "• /exit — 退出当前引擎，回到非引擎网关路由态",
        "• /clear — 清空 conversation_id 开启全新原生对话",
        "• /help — 查看本帮助信息",
        "",
        "引擎内部命令（无参数时支持微信交互式询问，回复 /cancel 可取消）：",
        "• 会话管理：/resume、/new、/fork、/rename、/rewind、/btw、/context、/copy",
        "• 模型与参数：/models、/model、/mode、/effort、/agents、/agent、/add-dir、/sandbox、/config",
        "• 工程与子命令：/diff、/open、/codesearch、/artifact、/projects、/tasks、/mcp、/plugin、/usage、/skills、/hooks、/doctor",
        "",
        "💡 除上述命令外，所有自然语言、微信原生语音（Recognition）及 AI 技能命令（如 /plan、/review）将 100% 直传原生 CLI。",
    ])
