"""Formatting and terminal output sanitization helpers for EngineSessionRouter."""
import re
from typing import Any, Dict, List

from bridge.engine_command_discovery import EngineCommandSpec, EngineDiscoveryResult

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
NON_ENGINE_HELP_TEXT = (
    "WeChat-AGY 系统帮助手册【非引擎默认状态 · 查看与工作目录控制】：\n"
    "1. 工作目录：/workspace <路径>（指定工作目录）、/pwd（查看当前目录）、/ls [子目录]（浏览目录）\n"
    "2. 系统状态：/status（运行看板）、/engines（引擎就绪列表）\n"
    "3. 安全审计：/security（五维防御看板）、/audit（幻觉审计）、/rules（演进看板）\n"
    "4. 进入引擎：/agy | /pi | /claude | /omp（进入后锁定至当前工作目录，并实时同步该 CLI 全量命令）\n"
    "5. 退出状态：在引擎内发送 /exit 返回本状态并保留当前工作目录。"
)


def clean_cli_output(raw: str) -> str:
    """Resolve carriage-return spinner frames and strip ANSI escape sequences."""
    cleaned_lines: List[str] = []
    for line in (raw or "").split("\n"):
        if "\r" in line:
            parts = [ANSI_RE.sub("", seg).strip() for seg in line.split("\r")]
            parts = [p for p in parts if p]
            text = parts[-1] if parts else ""
        else:
            text = ANSI_RE.sub("", line).rstrip()
        if text:
            cleaned_lines.append(text)
    return "\n".join(cleaned_lines).strip()


def format_engines_summary(rows: List[Dict[str, Any]]) -> str:
    """Format configured CLI engine availability summary for Non-Engine /engines."""
    lines = ["【🔌 可用 CLI 引擎列表（非引擎状态仅查看）】"]
    for row in rows:
        eid = row["engine_id"]
        inst = "🟢 已就绪" if row["installed"] else "⚪ 未安装"
        health = "正常" if row["healthy"] else f"受限({row['reason']})"
        lines.append(f"• /{eid} — {inst} | 状态={health} | 二进制={row['binary']}")
    lines.append("\n👉 发送 /agy、/pi、/claude 或 /omp 进入指定引擎并实时探测完整命令表。")
    return "\n".join(lines)


def format_unknown_engine_command_reply(
    active_engine: str, cmd_name: str, commands: List[Dict[str, Any]]
) -> str:
    """Format clear hint when an unknown `/command` is sent inside an active engine."""
    preview = ", ".join(f"/{c['name']}" for c in commands[:20])
    if len(commands) > 20:
        preview += f" 等共 {len(commands)} 个"
    return (
        f"⛔ 当前引擎 [{active_engine.upper()}] 的动态命令映射中不存在 '/{cmd_name}'（引擎内所有 /命令 仅限该引擎自身）。\n"
        f"📋 当前可用：{preview}\n💡 发送 /help 查看完整清单，或发送 /exit 退出引擎并回到非引擎状态。"
    )


def format_entry_summary(discovery: EngineDiscoveryResult, workspace_dir: str = "") -> str:
    """Render WeChat entry summary displaying full CLI command parity (zero hidden subset)."""
    eid = discovery.engine_id.upper()
    ver = f"版本: {discovery.version} | " if discovery.version else ""
    src = ", ".join(discovery.discovery_sources)
    caps = discovery.session_capabilities or {}
    native_hint = (
        "支持原生多轮对话延续（发送 /clear 开启新会话，/resume 切换历史线程）"
        if caps.get("supports_native_session")
        else "上下文由网关延续（发送 /clear 清空会话历史）"
    )

    groups = [
        ("🤖 AI 核心与技能工作流", "ai_core"),
        ("⚙️ 配置、模型与扩展管理", "config"),
        ("💬 会话生命周期控制", "session"),
        ("🧰 工作区与交互工具", "tools"),
    ]
    lines = [f"✅ 已进入 [{eid}] 引擎会话（{ver}探测来源: {src}）"]
    if workspace_dir:
        lines.append(f"📂 当前工作目录：`{workspace_dir}`")
    lines.append(f"💬 会话机制：{native_hint}")
    lines.append(f"📋 已与本地 CLI 全量同步 {len(discovery.commands)} 个唯一命令（无冗余别名）：")
    for label, cat in groups:
        slashes = [c.slash for c in discovery.commands if c.category == cat]
        if slashes:
            lines.append(f"• {label} ({len(slashes)})：{', '.join(slashes)}")
    if discovery.models:
        lines.append(f"• 🧠 实时可用模型：{len(discovery.models)} 个（/models 查看列表，/model 切换）")
    lines.append("\n💡 可直接发送自然语言编程需求，或发送 /help 查看每条命令说明；发送 /exit 退出引擎。")
    return "\n".join(lines)


def format_in_engine_help(
    engine_id: str, commands: List[Dict[str, Any]], meta: Dict[str, Any], workspace_dir: str = ""
) -> str:
    """Render detailed dynamic command menu for an active engine session."""
    ver = meta.get("version") or "unknown"
    src = ", ".join(meta.get("sources") or []) or "live"
    cfg = meta.get("config") or {}
    cfg_parts = [f"{k}={v}" for k, v in cfg.items() if v]
    cfg_summary = ", ".join(cfg_parts) or "默认"
    conv_id = str(meta.get("conversation_id") or "")
    turns = int(meta.get("turn_count") or 0)
    ws_line = workspace_dir or meta.get("workspace_dir") or "."

    lines = [
        f"【📘 {engine_id.upper()} 引擎全量命令清单】（版本: {ver} | 来源: {src}）",
        f"📂 工作目录：`{ws_line}`",
        f"🎛️ 会话配置：{cfg_summary} | 轮数：{turns}" + (f" (ID:{conv_id[:8]})" if conv_id else ""),
    ]

    groups = [
        ("🤖 AI 核心与技能工作流", "ai_core"),
        ("⚙️ 配置、模型与扩展管理", "config"),
        ("💬 会话控制", "session"),
        ("🧰 工作区与交互工具", "tools"),
        ("📘 帮助", "help"),
    ]
    for title, cat in groups:
        matched = [EngineCommandSpec.from_dict(c) for c in commands if (c.get("category") or "ai_core") == cat]
        if not matched:
            continue
        lines.append(f"\n{title} ({len(matched)})：")
        for spec in matched:
            desc = f" — {spec.description}" if spec.description else ""
            lines.append(f"• {spec.slash}{desc}")

    lines.append("\n🚪 发送 /clear 清空开启新会话，或发送 /exit 退出引擎回到工作目录选择状态。")
    return "\n".join(lines)


def format_session_config_prompt(active_engine: str, cmd_name: str, current_val: str, options: List[str]) -> str:
    """Render current value and available options for /model, /mode, or /effort."""
    opts_text = "\n• ".join(options) if options else "未从实时探测枚举到固定候选项"
    prefix = f"可选值：\n• {opts_text}" if options else opts_text
    return (
        f"【⚙️ {active_engine.upper()} · /{cmd_name} 会话配置】\n"
        f"当前设置：{current_val}\n{prefix}\n\n"
        f"👉 用法：/{cmd_name} <值> 设置，或 /{cmd_name} reset 恢复默认。"
    )
