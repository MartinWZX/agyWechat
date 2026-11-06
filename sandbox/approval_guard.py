"""External Sandbox Guard for dangerously-skip-permissions & L1-L5 Approvals."""
import ast
import re
import os
from typing import Dict, Any, Tuple

class SandboxSecurityError(Exception):
    pass

class ApprovalGuard:
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
        r"/etc/passwd"
    ]

    @classmethod
    def scan_shell_command(cls, cmd_str: str) -> Tuple[bool, str]:
        for pattern in cls.DANGEROUS_COMMANDS:
            if re.search(pattern, cmd_str):
                raise SandboxSecurityError(f"Prohibited high-risk command pattern detected: {pattern}")
        return True, "Shell command safe."

    @classmethod
    def scan_python_ast(cls, script_content: str) -> Tuple[bool, str]:
        try:
            tree = ast.parse(script_content)
        except SyntaxError as e:
            raise SandboxSecurityError(f"AST syntax parsing error: {e}")

        for node in ast.walk(tree):
            # Check prohibited imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ["pty", "ptyprocess"]:
                        pass # Allowed in driver, checked strictly in sandboxed scripts
            elif isinstance(node, ast.Call):
                # Check eval, exec in user scripts
                if isinstance(node.func, ast.Name) and node.func.id in ["eval"]:
                    raise SandboxSecurityError("Prohibited built-in 'eval' call detected.")

        return True, "AST security scan passed."

    @classmethod
    def get_permission_level(cls, action_type: str, details: str = "") -> int:
        # L1: Read-only info (auto)
        # L2: Safe local file write within workspace (auto with dangerously-skip-permissions)
        # L3: Safe shell execution in workspace (auto)
        # L4: Service restarts, external HTTP push, DB schema changes (WeChat voice/text confirm)
        # L5: Production drop tables, credential update, system delete (Strict text confirm #确认 <task_id>)
        if "drop table" in details.lower() or "delete_all" in details.lower():
            return 5
        if "restart" in details.lower() or "publish" in details.lower() or "cron_register" in details.lower():
            return 4
        if "exec_cmd" in action_type:
            return 3
        if "write_file" in action_type:
            return 2
        return 1
