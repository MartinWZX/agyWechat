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
• `/agy` 或 `/new` - 启动 AGY 会话，进入独占态
• `/help` - 显示本帮助手册

【AGY 独占态 (AGY_MODE)】
• *直接输入任意文本* - 100% 透传至 AGY 终端交互
• `/ctrlc` 或 `/stop` - 中断当前任务 (SIGINT)
• `/new` - 终止旧进程，重启全新 AGY 会话
• `/exit` 或 `/kill` - 退出会话，安全返回 HOST_MODE
• `/help` - 显示本帮助手册
"""


class HostModeRouter:
    """Handles commands while system is in HOST_MODE."""

    def __init__(self, manager: SingletonAGYManager, settings: Settings):
        self.manager = manager
        self.settings = settings

    async def handle(self, message: str) -> str:
        tokens = message.strip().split()
        if not tokens:
            return "请输入有效命令。输入 /help 查看帮助。"

        cmd = tokens[0].lower()
        args = tokens[1:]

        if cmd == "/pwd":
            return f"📂 当前工作目录：`{self.manager.GLOBAL_CWD}`"

        if cmd == "/help":
            return HELP_TEXT

        if cmd == "/ls":
            return self._handle_ls(args)

        if cmd == "/cd":
            return self._handle_cd(args)

        if cmd in ("/agy", "/new"):
            try:
                await self.manager.spawn(cwd=self.manager.GLOBAL_CWD)
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
            path_arg = Path(args[0])
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

        path_arg = Path(args[0])
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

    async def handle(self, message: str) -> Optional[str]:
        stripped = message.strip()

        if stripped in ("/exit", "/kill"):
            await self.manager.terminate()
            return "🛑 AGY 进程已终止，已安全回到 `HOST_MODE`。"

        if stripped == "/new":
            await self.manager.restart()
            return f"🔄 AGY 会话已重启 (新 PID: {self.manager.pid})，继续处于 `AGY_MODE`。"

        if stripped in ("/ctrlc", "/stop"):
            await self.manager.send_ctrl_c()
            return "⚡ 已向 AGY 终端发送 Ctrl+C (SIGINT) 中断信号。"

        if stripped == "/help":
            return HELP_TEXT

        if stripped.split()[0].lower() in ("/pwd", "/ls", "/cd"):
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
            return await self.host_router.handle(message)
        else:
            return await self.agy_router.handle(message)
