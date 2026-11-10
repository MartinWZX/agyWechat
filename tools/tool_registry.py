"""Deterministic Tool Registry with Sandboxed Dynamic Tools, Persistent Snapshots & Trace Audit."""
import ast
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import closing
from typing import Any, Callable, Dict, List, Optional, Tuple

from sandbox.approval_guard import ApprovalGuard, SandboxSecurityError
from sandbox.runner import SandboxExecutionError, run_code_sync
from tools.audit_tools import run_ast_contract_audit, run_git_zero_leakage_audit
from verifier.hallucination_guard import ShadowDryRunError, ShadowDryRunValidator


def _locate_entrypoint_in_ast(tree: ast.Module, tool_name: str) -> Optional[Tuple[str, str]]:
    """Locate target function name and docstring in AST without executing untrusted code."""
    funcs: Dict[str, ast.FunctionDef] = {}
    first_name: Optional[str] = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            funcs[node.name] = node
            if first_name is None:
                first_name = node.name
    chosen = funcs.get(tool_name) or funcs.get(f"tool_{tool_name}") or (funcs.get(first_name) if first_name else None)
    if chosen is None:
        return None
    doc = ast.get_docstring(chosen) or f"Dynamic sandboxed tool '{tool_name}'"
    return chosen.name, doc


def _make_sandboxed_tool(tool_name: str, entrypoint: str, code: str, doc: str) -> Callable:
    """Create a host-safe callable wrapper that executes dynamic tool code out-of-process."""
    def _runner(**kwargs: Any) -> Any:
        res = run_code_sync(
            code,
            entrypoint=entrypoint,
            kwargs=kwargs,
            timeout=10.0,
            allow_network=False,
            filename=f"<tool:{tool_name}>",
        )
        if not res.ok:
            raise SandboxExecutionError(f"Tool '{tool_name}' failed in sandbox: {res.error_summary()}")
        return res.result

    _runner.__name__ = entrypoint
    _runner.__doc__ = doc
    return _runner


class ToolRegistry:
    """Self-evolving deterministic tool registry with persistent versioned rollback."""
    _instance = None
    _tools: Dict[str, Callable] = {}
    _tool_versions: Dict[str, int] = {}
    _tool_dirs: Dict[str, str] = {}
    _trace_path = "tools/tool_execution_trace.jsonl"
    _versions_dir = "tools/versions"

    def __new__(cls, *args: Any, **kwargs: Any) -> "ToolRegistry":
        if not cls._instance:
            cls._instance = super(ToolRegistry, cls).__new__(cls)
        return cls._instance

    @classmethod
    def _manifest_path(cls, versions_dir: Optional[str] = None) -> str:
        return os.path.join(versions_dir or cls._versions_dir, "manifest.json")

    @classmethod
    def _save_manifest(cls, active_map: Dict[str, int], versions_dir: Optional[str] = None) -> None:
        vdir = versions_dir or cls._versions_dir
        os.makedirs(vdir, exist_ok=True)
        with open(cls._manifest_path(vdir), "w", encoding="utf-8") as f:
            json.dump(active_map, f, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def _load_manifest(cls, versions_dir: Optional[str] = None) -> Dict[str, int]:
        mpath = cls._manifest_path(versions_dir)
        if os.path.isfile(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): int(v) for k, v in data.items() if int(v) > 0}
            except (OSError, ValueError):
                return {}
        return {}

    @classmethod
    def load_persisted_tools(cls) -> int:
        """Reload dynamically mutated tools from `tools/versions/` on process startup."""
        if not os.path.isdir(cls._versions_dir):
            return 0
        manifest = cls._load_manifest()
        if not manifest:
            for fname in os.listdir(cls._versions_dir):
                m = re.match(r"^([a-zA-Z0-9_]+)_v(\d+)\.py$", fname)
                if m:
                    tname, ver = m.group(1), int(m.group(2))
                    if ver > manifest.get(tname, 0):
                        manifest[tname] = ver
        loaded = 0
        for tool_name, version in manifest.items():
            if tool_name in cls._tools:
                continue
            vpath = os.path.join(cls._versions_dir, f"{tool_name}_v{version}.py")
            if not os.path.isfile(vpath):
                continue
            try:
                with open(vpath, "r", encoding="utf-8") as f:
                    code = f.read()
                ApprovalGuard.scan_python_ast(code)
                tree = ast.parse(code)
                located = _locate_entrypoint_in_ast(tree, tool_name)
                if located:
                    entry, doc = located
                    cls._tools[tool_name] = _make_sandboxed_tool(tool_name, entry, code, doc)
                    cls._tool_versions[tool_name] = version
                    loaded += 1
            except (OSError, SyntaxError, SandboxSecurityError):
                continue
        return loaded

    @classmethod
    def register(cls, name: str) -> Callable:
        """Decorator to register a builtin deterministic Python tool."""
        def decorator(func: Callable) -> Callable:
            cls._tools[name] = func
            cls._tool_versions[name] = cls._tool_versions.get(name, 1)
            return func
        return decorator

    @classmethod
    def set_trace_path(cls, path: str) -> None:
        cls._trace_path = path

    @classmethod
    def get_tool(cls, name: str) -> Optional[Callable]:
        return cls._tools.get(name)

    @classmethod
    def execute(cls, name: str, **kwargs: Any) -> Any:
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
    def _record_trace(cls, tool_name: str, args: Dict[str, Any], result: Any, duration: float, success: bool) -> None:
        entry = {
            "timestamp": time.time(),
            "tool_name": tool_name,
            "args": args,
            "success": success,
            "duration": round(duration, 4),
            "result_preview": str(result)[:300]
        }
        os.makedirs(os.path.dirname(cls._trace_path) or ".", exist_ok=True)
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
        sigs: List[Dict[str, str]] = []
        for name in sorted(cls._tools.keys()):
            func = cls._tools[name]
            doc = func.__doc__ or "Deterministic Python tool"
            sigs.append({
                "name": name,
                "version": f"v{cls._tool_versions.get(name, 1)}",
                "doc": doc.strip()
            })
        return sigs

    @classmethod
    def apply_mutation_patch(
        cls,
        action: str,
        tool_name: str,
        tool_code: str,
        versions_dir: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Apply tool mutation patch with AST security scan, out-of-process dry-run, and persistent snapshot."""
        vdir = versions_dir or cls._versions_dir
        if action == "deprecate":
            cls._tools.pop(tool_name, None)
            manifest = cls._load_manifest(vdir)
            manifest.pop(tool_name, None)
            cls._save_manifest(manifest, vdir)
            return True, f"Deprecated tool '{tool_name}'."

        try:
            ApprovalGuard.scan_python_ast(tool_code)
            tree = ast.parse(tool_code)
        except (SyntaxError, SandboxSecurityError) as e:
            return False, f"Tool code security/syntax error: {e}"

        try:
            ShadowDryRunValidator.validate_script(tool_code)
        except ShadowDryRunError as e:
            return False, f"Tool shadow dry-run failed: {e}"

        located = _locate_entrypoint_in_ast(tree, tool_name)
        if not located:
            return False, f"Could not locate callable function for tool '{tool_name}' in code."
        entry, doc = located

        os.makedirs(vdir, exist_ok=True)
        current_v = cls._tool_versions.get(tool_name, 0)
        next_v = current_v + 1
        version_path = os.path.join(vdir, f"{tool_name}_v{next_v}.py")
        with open(version_path, "w", encoding="utf-8") as f:
            f.write(tool_code.strip() + "\n")

        cls._tools[tool_name] = _make_sandboxed_tool(tool_name, entry, tool_code, doc)
        cls._tool_versions[tool_name] = next_v
        cls._tool_dirs[tool_name] = vdir
        manifest = cls._load_manifest(vdir)
        manifest[tool_name] = next_v
        cls._save_manifest(manifest, vdir)
        return True, f"Successfully {action}ed tool '{tool_name}' to version v{next_v}."

    @classmethod
    def rollback(cls, tool_name: str, versions_dir: Optional[str] = None) -> Tuple[bool, str]:
        """Rollback tool to previous persistent version snapshot."""
        vdir = versions_dir or cls._tool_dirs.get(tool_name) or cls._versions_dir
        current_v = cls._tool_versions.get(tool_name, 1)
        if current_v <= 1:
            return False, f"No previous version available for tool '{tool_name}'."
        prev_v = current_v - 1
        version_path = os.path.join(vdir, f"{tool_name}_v{prev_v}.py")
        if not os.path.exists(version_path):
            return False, f"Snapshot for v{prev_v} not found at {version_path}."

        with open(version_path, "r", encoding="utf-8") as f:
            code = f.read()
        try:
            ApprovalGuard.scan_python_ast(code)
            tree = ast.parse(code)
        except (SyntaxError, SandboxSecurityError) as e:
            return False, f"Failed to validate previous version: {e}"

        located = _locate_entrypoint_in_ast(tree, tool_name)
        if not located:
            return False, "Failed to locate entrypoint in previous version."
        entry, doc = located

        cls._tools[tool_name] = _make_sandboxed_tool(tool_name, entry, code, doc)
        cls._tool_versions[tool_name] = prev_v
        manifest = cls._load_manifest(vdir)
        manifest[tool_name] = prev_v
        cls._save_manifest(manifest, vdir)
        return True, f"Rolled back tool '{tool_name}' to version v{prev_v}."


registry = ToolRegistry()


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
    """Query statistics from local SQLite database with guaranteed connection closure."""
    import sqlite3
    if not os.path.exists(db_path):
        return {"error": "Database file not found"}
    with closing(sqlite3.connect(db_path)) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM messages;")
        msg_count = c.fetchone()[0]
    return {"messages_count": msg_count}


@ToolRegistry.register("git_zero_leakage_audit")
def tool_git_zero_leakage_audit(workspace_dir: str = ".") -> Dict[str, Any]:
    """Audit Git staged files, notebooks, and added diff lines against zero-leakage security redlines."""
    return run_git_zero_leakage_audit(workspace_dir=workspace_dir)


@ToolRegistry.register("ast_contract_audit")
def tool_ast_contract_audit(paths: List[str]) -> Dict[str, Any]:
    """Audit Python files via AST for stubs, mutable defaults, silent excepts, and async blocking calls."""
    return run_ast_contract_audit(paths=paths)


ToolRegistry.load_persisted_tools()
