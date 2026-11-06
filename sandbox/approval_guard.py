"""External Sandbox Guard for dangerously-skip-permissions & L1-L5 Approvals."""
import ast
import asyncio
import os
import re
import time
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple, Union


class SandboxSecurityError(Exception):
    """Raised when an unauthorized or dangerous shell/python operation is detected."""
    def __init__(self, message: str = "Sandbox security violation"):
        super().__init__(message)


class PermissionLevel(IntEnum):
    """Five-tier permission levels."""
    L1_READ = 1
    L2_LOCAL_WRITE = 2
    L3_SAFE_EXEC = 3
    L4_LOCAL_WRITE = 4
    L5_DANGEROUS = 5


class ApprovalGuard:
    """Multi-tiered external sandbox and interactive approval manager."""

    DANGEROUS_COMMANDS = [
        r"\brm\s+-rf\s+/",
        r"\bmkfs\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bdd\s+if=",
        r"\bchmod\s+-R\s+777\s+/",
        r"~/.ssh",
        r"~/.aws",
        r"~/.config/gcloud",
        r"/etc/shadow",
        r"/etc/passwd",
    ]

    PROHIBITED_CALLS = [
        "os.system",
        "subprocess.Popen",
        "eval",
        "exec",
        "shutil.rmtree",
    ]

    def __init__(self, store: Any = None, settings: Any = None):
        self.store = store
        self.settings = settings
        self._pending_futures: Dict[str, asyncio.Future] = {}
        self._pending_metadata: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def scan_shell_command(cls, cmd_str: str) -> Tuple[bool, str]:
        """Scan command string against dangerous regex blacklist."""
        for pattern in cls.DANGEROUS_COMMANDS:
            if re.search(pattern, cmd_str):
                raise SandboxSecurityError(f"Prohibited high-risk command pattern detected: {pattern}")
        return True, "Shell command safe."

    @classmethod
    def scan_python_ast(cls, script_content: str) -> Tuple[bool, str]:
        """Static AST security check for prohibited function calls."""
        try:
            tree = ast.parse(script_content)
        except SyntaxError as e:
            raise SandboxSecurityError(f"AST syntax parsing error: {e}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in ["eval"]:
                    raise SandboxSecurityError(f"Prohibited built-in '{node.func.id}' call detected.")
                if isinstance(node.func, ast.Attribute):
                    val = node.func.value
                    if isinstance(val, ast.Name) and val.id == "os" and node.func.attr == "system":
                        raise SandboxSecurityError("Prohibited 'os.system' execution detected.")
        return True, "AST security scan passed."

    @classmethod
    def inspect_script_code(cls, script_content: str) -> Tuple[bool, List[str]]:
        """Inspect script and return boolean success and error message list."""
        errors: List[str] = []
        try:
            tree = ast.parse(script_content)
        except SyntaxError as e:
            return False, [f"AST syntax error: {e}"]

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in ["eval", "exec"]:
                    errors.append(f"Prohibited call: {node.func.id}")
                elif isinstance(node.func, ast.Attribute):
                    val = node.func.value
                    if isinstance(val, ast.Name):
                        full_call = f"{val.id}.{node.func.attr}"
                        if full_call in cls.PROHIBITED_CALLS:
                            errors.append(f"Prohibited call: {full_call}")

        return (len(errors) == 0), errors

    @classmethod
    def get_permission_level(cls, action_type: str, details: str = "") -> int:
        """Categorize action into L1-L5."""
        det_lower = details.lower()
        if "drop table" in det_lower or "delete_all" in det_lower:
            return PermissionLevel.L5_DANGEROUS.value
        if "restart" in det_lower or "publish" in det_lower or "cron_register" in det_lower or "modify" in det_lower:
            return PermissionLevel.L4_LOCAL_WRITE.value
        if "exec_cmd" in action_type:
            return PermissionLevel.L3_SAFE_EXEC.value
        if "write_file" in action_type:
            return PermissionLevel.L2_LOCAL_WRITE.value
        return PermissionLevel.L1_READ.value

    def get_scrubbed_env(self) -> Dict[str, str]:
        """Strip sensitive host credentials before launching subprocess."""
        scrubbed = os.environ.copy()
        blacklist = [
            "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN", "SSH_AUTH_SOCK"
        ]
        for key in blacklist:
            scrubbed.pop(key, None)
        return scrubbed

    async def request_permission(
        self,
        user_id: str,
        permission_level: Union[PermissionLevel, int],
        description: str,
        timeout_seconds: float = 30.0
    ) -> Tuple[bool, str]:
        """Request permission. L1-L3 auto-approve; L4-L5 wait for admin approval."""
        level_val = int(permission_level)
        if level_val <= PermissionLevel.L3_SAFE_EXEC:
            return True, f"Auto-approved for level {level_val} under dangerously-skip-permissions."

        action_id = f"act_{int(time.time() * 1000)}_{os.urandom(2).hex()}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending_futures[action_id] = future
        self._pending_metadata[action_id] = {
            "user_id": user_id,
            "level": level_val,
            "description": description,
            "created_at": time.time()
        }

        try:
            approved = await asyncio.wait_for(future, timeout=timeout_seconds)
            return approved, f"Action {action_id} approved by administrator."
        except asyncio.TimeoutError:
            return False, f"Action {action_id} timed out waiting for administrator confirmation."
        finally:
            self._pending_futures.pop(action_id, None)
            self._pending_metadata.pop(action_id, None)

    async def handle_approval_response(self, user_id: str, message_content: str) -> str:
        """Handle administrator approval message (e.g. '确认 <action_id>')."""
        admin_users = getattr(self.settings, "ADMIN_USERS", ["admin_user", "*"])
        if "*" not in admin_users and user_id not in admin_users:
            return f"权限不足：用户 '{user_id}' 不是管理员，无法审批高危操作。"

        match = re.search(r"(?:#?确认|approve)\s+([a-zA-Z0-9_]+)", message_content.strip())
        if not match:
            return "无效审批指令。格式：'确认 <action_id>' 或 '#确认 <action_id>'。"

        action_id = match.group(1).strip()
        future = self._pending_futures.get(action_id)
        if not future or future.done():
            return f"操作 ID '{action_id}' 不存在或已过期。"

        future.set_result(True)
        return f"已由管理员 {user_id} 批准执行操作 {action_id}。"
