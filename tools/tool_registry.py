"""Deterministic Tool Registry with Version Snapshots & Execution Trace Audit Log."""
import os
import json
import time
import shutil
import ast
from typing import Callable, Dict, Any, List, Optional, Tuple
from verifier.hallucination_guard import ShadowDryRunValidator, ShadowDryRunError


class ToolRegistry:
    """Self-evolving deterministic tool registry with versioned rollback."""
    _instance = None
    _tools: Dict[str, Callable] = {}
    _tool_versions: Dict[str, int] = {}
    _trace_path = "tools/tool_execution_trace.jsonl"
    _versions_dir = "tools/versions"

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ToolRegistry, cls).__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, name: str):
        """Decorator to register a deterministic Python tool."""
        def decorator(func: Callable):
            cls._tools[name] = func
            cls._tool_versions[name] = cls._tool_versions.get(name, 1)
            return func
        return decorator

    @classmethod
    def set_trace_path(cls, path: str):
        cls._trace_path = path

    @classmethod
    def get_tool(cls, name: str) -> Optional[Callable]:
        return cls._tools.get(name)

    @classmethod
    def execute(cls, name: str, **kwargs) -> Any:
        func = cls._tools.get(name)
        if not func:
            raise ValueError(f"Tool '{name}' not found in registry")
        start_time = time.time()
        try:
            result = func(**kwargs)
            duration = time.time() - start_time
            cls._record_trace(name, kwargs, result, duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start_time
            cls._record_trace(name, kwargs, str(e), duration, success=False)
            raise

    @classmethod
    def _record_trace(cls, tool_name: str, args: Dict[str, Any], result: Any, duration: float, success: bool):
        entry = {
            "timestamp": time.time(),
            "tool_name": tool_name,
            "args": args,
            "success": success,
            "duration": round(duration, 4),
            "result_preview": str(result)[:300]
        }
        os.makedirs(os.path.dirname(cls._trace_path), exist_ok=True)
        with open(cls._trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def get_trace_count(cls) -> int:
        if not os.path.exists(cls._trace_path):
            return 0
        with open(cls._trace_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    @classmethod
    def list_tools(cls) -> List[str]:
        return sorted(list(cls._tools.keys()))

    @classmethod
    def list_tool_signatures(cls) -> List[Dict[str, str]]:
        sigs = []
        for name, func in cls._tools.items():
            doc = func.__doc__ or "Deterministic Python tool"
            sigs.append({
                "name": name,
                "version": f"v{cls._tool_versions.get(name, 1)}",
                "doc": doc.strip()
            })
        return sigs

    @classmethod
    def apply_mutation_patch(cls, action: str, tool_name: str, tool_code: str) -> Tuple[bool, str]:
        """Apply tool mutation patch with AST check, shadow dry-run, and version snapshot."""
        # 1. AST Validation
        try:
            tree = ast.parse(tool_code)
        except SyntaxError as e:
            return False, f"Tool code AST syntax error: {e}"

        # 2. Shadow dry-run validation
        try:
            ShadowDryRunValidator.validate_script(tool_code)
        except ShadowDryRunError as e:
            return False, f"Tool shadow dry-run failed: {e}"

        # 3. Snapshot current version if updating
        os.makedirs(cls._versions_dir, exist_ok=True)
        current_v = cls._tool_versions.get(tool_name, 0)
        next_v = current_v + 1

        version_path = os.path.join(cls._versions_dir, f"{tool_name}_v{next_v}.py")
        with open(version_path, "w", encoding="utf-8") as f:
            f.write(tool_code)

        # 4. Dynamically compile and hot-register
        scope: Dict[str, Any] = {}
        exec(tool_code, scope)
        target_func = scope.get(tool_name) or scope.get(f"tool_{tool_name}")
        if not target_func and scope:
            # Pick first callable function
            for k, v in scope.items():
                if callable(v) and not k.startswith("__"):
                    target_func = v
                    break

        if not target_func:
            return False, f"Could not locate callable function for tool '{tool_name}' in code."

        cls._tools[tool_name] = target_func
        cls._tool_versions[tool_name] = next_v
        return True, f"Successfully {action}ed tool '{tool_name}' to version v{next_v}."

    @classmethod
    def rollback(cls, tool_name: str) -> Tuple[bool, str]:
        """Rollback tool to previous version snapshot."""
        current_v = cls._tool_versions.get(tool_name, 1)
        if current_v <= 1:
            return False, f"No previous version available for tool '{tool_name}'."
        prev_v = current_v - 1
        version_path = os.path.join(cls._versions_dir, f"{tool_name}_v{prev_v}.py")
        if not os.path.exists(version_path):
            return False, f"Snapshot for v{prev_v} not found at {version_path}."

        with open(version_path, "r", encoding="utf-8") as f:
            code = f.read()

        scope: Dict[str, Any] = {}
        exec(code, scope)
        target_func = scope.get(tool_name) or scope.get(f"tool_{tool_name}")
        if not target_func:
            return False, "Failed to compile previous version."

        cls._tools[tool_name] = target_func
        cls._tool_versions[tool_name] = prev_v
        return True, f"Rolled back tool '{tool_name}' to version v{prev_v}."


# Global registry singleton alias
registry = ToolRegistry()


# ==========================================
# Pre-registered Deterministic Tools
# ==========================================
@ToolRegistry.register("read_file")
def tool_read_file(path: str) -> str:
    """Read full contents of a file within the workspace."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@ToolRegistry.register("write_file")
def tool_write_file(path: str, content: str) -> str:
    """Write text content to a file within the workspace."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully wrote {len(content)} bytes to {path}"


@ToolRegistry.register("system_metrics")
def tool_system_metrics() -> Dict[str, Any]:
    """Retrieve load average and disk utilization."""
    load1, load5, load15 = os.getloadavg()
    total_b, used_b, free_b = shutil.disk_usage("/")
    return {
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "load_15m": round(load15, 2),
        "disk_used_percent": round((used_b / total_b) * 100, 2),
        "disk_free_gb": round(free_b / (1024 ** 3), 2)
    }


@ToolRegistry.register("db_query_stats")
def tool_db_query_stats(db_path: str = "memory/agent_core.db") -> Dict[str, Any]:
    """Query statistics from local SQLite database."""
    import sqlite3
    if not os.path.exists(db_path):
        return {"error": "Database file not found"}
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages;")
    msg_count = c.fetchone()[0]
    conn.close()
    return {"messages_count": msg_count}
