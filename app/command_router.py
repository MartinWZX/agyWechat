"""Dual-state command router for HOST_MODE and AGY_MODE."""

import os
from pathlib import Path
from typing import Optional
from app.config import Settings, get_settings
from app.singleton_agy import SingletonAGYManager, SystemState, get_agy_manager


HELP_TEXT = """🤖 *WeChat-AGY Bridge 帮助手册*

【宿主导航态 (HOST_MODE)】
• `/pwd` - 查看当前工作目录
• `/ls [路径]` - 查看目录文件清单
• `/cd <路径>` - 切换工作目录 (沙箱限制内)
• `/status` - 查看服务与会话状态
• `/agy` 或 `/new` - 启动 AGY 会话，进入独占态
• `/help` - 显示本帮助手册

【AGY 独占态 (AGY_MODE)】
• *直接输入任意文本* - 100% 透传至 AGY 终端交互（代码编写、需求问答、调试等）
• `/ctrlc` 或 `/stop` - 中断当前任务 (SIGINT)
• `/new` - 终止旧进程，重启全新 AGY 会话
• `/exit` 或 `/quit` 或 `/kill` - 退出会话，安全返回 HOST_MODE
• `/help` - 显示本帮助手册

【Google AGY 智能体原生命令 (独占态下 100% 直通调用)】
• `/clear` - 清空当前上下文并开启新轮次
• `/diff` - 查看代码修改与差异
• `/effort` - 设置深度思考等级 (low/medium/high/xhigh/max)
• `/skills` - 列出并查看可用的技能扩展
• `/mcp` - 管理与查看 MCP 服务配置
• `/tasks` - 查看后台运行的任务列表
• `/usage` (或 `/quota`) - 查看模型配额与 Token 消耗
• `/agents` - 列出可用的定制智能体
• `/artifact` - 查看并审阅生成的工件
• `/btw` - 在不打断当前任务的情况下侧面追问
• 智能体技能指令（如 `/goal`, `/plan`, `/browser`, `/teamwork` 等均原生支持）
"""


class HostModeRouter:
    """Handles commands while system is in HOST_MODE."""

    def __init__(self, manager: SingletonAGYManager, settings: Settings):
        self.manager = manager
        self.settings = settings

    async def handle(self, message: str, user_id: Optional[str] = None) -> str:
        tokens = message.strip().split()
        if not tokens:
            return "请输入有效命令。输入 /help 查看帮助。"

        cmd = tokens[0].lower()
        args = tokens[1:]

        if cmd == "/pwd":
            return f"📂 当前工作目录：`{self.manager.GLOBAL_CWD}`"

        if cmd == "/help":
            return HELP_TEXT

        if cmd == "/status":
            agy_status = f"运行中 (PID: {self.manager.pid})" if self.manager.is_running() else "未运行（就绪）"
            return (
                f"📊 *WeChat-AGY 系统运行状态*\n"
                f"• 当前状态：`HOST_MODE`（宿主导航态）\n"
                f"• 工作目录：`{self.manager.GLOBAL_CWD}`\n"
                f"• AGY 进程：{agy_status}\n"
                f"• 沙箱根目录：`{self.settings.WORKSPACE_ROOT}`\n\n"
                f"💡 输入 `/agy` 即可启动唯一 AI 编程会话。"
            )

        if cmd == "/ls":
            return self._handle_ls(args)

        if cmd == "/cd":
            return self._handle_cd(args)

        if cmd in ("/agy", "/new"):
            try:
                await self.manager.spawn(cwd=self.manager.GLOBAL_CWD, extra_args=args or None, user_id=user_id)
                return (
                    f"🚀 *AGY 会话已启动* (PID: {self.manager.pid})\n"
                    f"📂 工作目录：`{self.manager.GLOBAL_CWD}`\n\n"
                    "已进入 `AGY_MODE`。后续所有输入将直接透传至 AGY 终端。\n"
                    "如需退出请输入 `/exit`，发送中断请输入 `/ctrlc`。"
                )
            except Exception as e:
                return f"❌ 启动 AGY 失败：{e}"

        return (
            f"❓ 未知命令：`{message}`\n"
            "当前处于 `HOST_MODE`（宿主导航态）。\n"
            "输入 `/agy` 启动 AI 编程会话，或输入 `/help` 查看所有命令。"
        )

    def _handle_ls(self, args: list[str]) -> str:
        target_dir = self.manager.GLOBAL_CWD
        if args:
            path_str = " ".join(args).strip("\"'")
            path_arg = Path(path_str)
            target_dir = (self.manager.GLOBAL_CWD / path_arg).resolve() if not path_arg.is_absolute() else path_arg.resolve()

        # Sandbox boundary validation
        try:
            target_dir.relative_to(self.settings.WORKSPACE_ROOT)
        except ValueError:
            return f"❌ 访问越界：目标路径超出沙箱限制 (`{self.settings.WORKSPACE_ROOT}`)"

        if not target_dir.exists():
            return f"❌ 目录不存在：`{target_dir}`"
        if not target_dir.is_dir():
            return f"❌ 路径不是目录：`{target_dir}`"

        try:
            entries = sorted(os.scandir(target_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
            if not entries:
                return f"📁 目录为空：`{target_dir}`"

            lines = [f"📁 目录内容：`{target_dir}`\n"]
            for entry in entries:
                try:
                    if entry.is_dir():
                        lines.append(f"📁 {entry.name}/")
                    elif entry.is_symlink():
                        lines.append(f"🔗 {entry.name}")
                    else:
                        size = entry.stat().st_size
                        size_str = self._format_size(size)
                        lines.append(f"📄 {entry.name} ({size_str})")
                except OSError:
                    lines.append(f"❓ {entry.name}")

            return "\n".join(lines)
        except Exception as e:
            return f"❌ 列出目录失败：{e}"

    def _handle_cd(self, args: list[str]) -> str:
        if not args:
            # cd without arguments resets to WORKSPACE_ROOT
            self.manager.GLOBAL_CWD = self.settings.WORKSPACE_ROOT
            return f"✅ 已返回沙箱根目录：`{self.manager.GLOBAL_CWD}`"

        path_str = " ".join(args).strip("\"'")
        path_arg = Path(path_str)
        target_dir = (self.manager.GLOBAL_CWD / path_arg).resolve() if not path_arg.is_absolute() else path_arg.resolve()

        # Sandbox validation
        try:
            target_dir.relative_to(self.settings.WORKSPACE_ROOT)
        except ValueError:
            return f"❌ 访问越界：目标路径超出沙箱限制 (`{self.settings.WORKSPACE_ROOT}`)"

        if not target_dir.exists():
            return f"❌ 目录不存在：`{target_dir}`"
        if not target_dir.is_dir():
            return f"❌ 目标不是有效目录：`{target_dir}`"

        self.manager.GLOBAL_CWD = target_dir
        return f"✅ 工作目录已切换至：`{self.manager.GLOBAL_CWD}`"

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if num_bytes < 1024:
                return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
            num_bytes /= 1024
        return f"{num_bytes:.1f} TB"


class AgyModeRouter:
    """Handles commands and message passthrough while system is in AGY_MODE."""

    def __init__(self, manager: SingletonAGYManager):
        self.manager = manager

    async def handle(self, message: str, user_id: Optional[str] = None) -> Optional[str]:
        stripped = message.strip()

        if stripped in ("/exit", "/kill", "/quit"):
            await self.manager.terminate()
            return "🛑 AGY 进程已终止，已安全回到 `HOST_MODE`。"

        if stripped in ("/agy", "/start"):
            return (
                f"ℹ️ AGY 独占会话已在运行中 (PID: {self.manager.pid})。\n"
                f"系统已严格保证全局唯一对话进程，所有输入直接送达 AGY 智能体。\n"
                f"• 重启会话：输入 `/new`\n"
                f"• 退出会话：输入 `/exit` 或 `/quit`"
            )

        if stripped == "/new":
            await self.manager.restart(user_id=user_id)
            return f"🔄 AGY 会话已重启 (新 PID: {self.manager.pid})，继续处于 `AGY_MODE`。"

        if stripped in ("/ctrlc", "/stop"):
            await self.manager.send_ctrl_c()
            return "⚡ 已向 AGY 终端发送 Ctrl+C (SIGINT) 中断信号。"

        if stripped == "/help":
            return HELP_TEXT

        tokens = stripped.split()
        if not tokens:
            # User sent empty message or whitespace; treat as Enter to advance terminal
            await self.manager.send_input("\r")
            return None

        if tokens[0].lower() in ("/pwd", "/ls", "/cd"):
            return (
                "⚠️ 当前系统处于 `AGY_MODE`（AGY独占态）。\n"
                "如需执行宿主导航命令 (`/pwd`, `/ls`, `/cd`)，请先输入 `/exit` 退出 AGY。"
            )

        # 100% Passthrough to PTY stdin
        await self.manager.send_input(message)
        return None


class CommandRouter:
    """Top-level command router dispatching based on user authorization and system state."""

    def __init__(
        self,
        manager: Optional[SingletonAGYManager] = None,
        settings: Optional[Settings] = None,
    ):
        self.settings = settings or get_settings()
        self.manager = manager or get_agy_manager(self.settings)
        self.host_router = HostModeRouter(self.manager, self.settings)
        self.agy_router = AgyModeRouter(self.manager)

    async def route(self, user_id: str, message: str) -> Optional[str]:
        """Route user message, returning direct response string or None if passed through."""
        # 1. Authorization check
        if "*" not in self.settings.ALLOWED_USERS and user_id not in self.settings.ALLOWED_USERS:
            return f"⛔ 访问被拒绝：用户 `{user_id}` 未在白名单中。"

        # 2. Input safety limit
        if len(message) > 4096:
            return "❌ 输入超限：单条消息不能超过 4096 字符。"

        # 3. State-based routing
        if self.manager.state == SystemState.HOST_MODE:
            return await self.host_router.handle(message, user_id=user_id)
        else:
            return await self.agy_router.handle(message, user_id=user_id)
