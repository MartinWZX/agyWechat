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
    """Query statistics from local SQLite database with guaranteed connection closure."""
    import sqlite3
    from contextlib import closing

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
    import re
    import subprocess

    forbidden_path_re = re.compile(
        r"(^|/)\.env(\..+)?$|\.(pem|key|p12|pfx|crt|keystore|db|sqlite|sqlite3|sql|bak|csv|tsv|xlsx|xls|parquet|feather|pkl|pickle|joblib|h5|jsonl|pt|pth|bin|onnx|safetensors|gguf|log|tfstate|tfvars)$|(^|/)(chroma_db|faiss_index|qdrant_storage|milvus_data|lancedb|\.cursor|\.claude|\.windsurf|\.continue|\.aws|\.kube)/|(^|/)(id_rsa|id_ed25519|dump\.rdb|nohup\.out)|(^|/)(scratch|temp|debug_|test_real_)[^/]*\.py$|(?:^|/)[^/]*(?:credentials|service-account|client_secret)[^/]*\.json$"
    )
    secret_line_re = re.compile(
        r"(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,}|AKIA[0-9A-Z]{16}|xoxb-[0-9A-Za-z-]{20,}|AIza[0-9A-Za-z_-]{35}|BEGIN (?:RSA|OPENSSH|EC|PRIVATE) KEY|(?:postgresql|mysql|redis|mongodb(?:\+srv)?)://[^/\s:@]+:[^/\s@]+@)"
    )

    proc_names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    staged_files: List[str] = [
        line.strip() for line in proc_names.stdout.splitlines() if line.strip()
    ]

    blocked_files: List[str] = []
    uncleaned_notebooks: List[str] = []
    for rel_path in staged_files:
        base_name = os.path.basename(rel_path)
        if base_name == ".env.example" or rel_path.endswith(".tfvars.example"):
            continue
        if rel_path.startswith("tests/fixtures/") and rel_path.endswith(".jsonl"):
            full_fixture = os.path.join(workspace_dir, rel_path)
            if os.path.isfile(full_fixture) and os.path.getsize(full_fixture) < 10240:
                continue
        if forbidden_path_re.search(rel_path):
            blocked_files.append(rel_path)
        if rel_path.endswith(".ipynb"):
            nb_full = os.path.join(workspace_dir, rel_path)
            if os.path.isfile(nb_full):
                try:
                    with open(nb_full, "r", encoding="utf-8") as nb_f:
                        nb_data = json.load(nb_f)
                    for cell in nb_data.get("cells", []):
                        if cell.get("outputs") or cell.get("execution_count") is not None:
                            uncleaned_notebooks.append(rel_path)
                            break
                except (OSError, ValueError):
                    uncleaned_notebooks.append(rel_path)

    proc_diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    secret_hit_lines: List[str] = []
    for idx, line in enumerate(proc_diff.stdout.splitlines(), start=1):
        if line.startswith("+") and not line.startswith("+++"):
            if secret_line_re.search(line):
                secret_hit_lines.append(f"diff_line_{idx}")

    passed = (
        len(blocked_files) == 0
        and len(uncleaned_notebooks) == 0
        and len(secret_hit_lines) == 0
    )
    return {
        "passed": passed,
        "staged_files": staged_files,
        "blocked_files": blocked_files,
        "uncleaned_notebooks": uncleaned_notebooks,
        "secret_hit_lines": secret_hit_lines,
    }


@ToolRegistry.register("ast_contract_audit")
def tool_ast_contract_audit(paths: List[str]) -> Dict[str, Any]:
    """Audit Python files via AST for stubs, mutable defaults, silent excepts, and async blocking calls."""
    violations: List[str] = []
    mutable_types = (ast.List, ast.Dict, ast.Set)
    blocking_http_methods = {"get", "post", "put", "delete", "patch", "head", "options", "request"}

    for file_path in paths:
        if not os.path.isfile(file_path) or not file_path.endswith(".py"):
            continue
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as e:
            violations.append(f"{file_path}:{e.lineno}:syntax_error")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Pass):
                violations.append(f"{file_path}:{node.lineno}:prohibited_pass_stub")
            elif isinstance(node, ast.Constant) and node.value is Ellipsis:
                violations.append(f"{file_path}:{node.lineno}:prohibited_ellipsis_stub")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                all_defaults = list(node.args.defaults) + [
                    d for d in node.args.kw_defaults if d is not None
                ]
                for default_node in all_defaults:
                    if isinstance(default_node, mutable_types):
                        violations.append(f"{file_path}:{node.lineno}:mutable_default_arg:{node.name}")
                if isinstance(node, ast.AsyncFunctionDef):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                            if isinstance(child.func.value, ast.Name):
                                mod_name = child.func.value.id
                                attr_name = child.func.attr
                                if mod_name == "time" and attr_name == "sleep":
                                    violations.append(
                                        f"{file_path}:{child.lineno}:sync_sleep_in_async:{node.name}"
                                    )
                                elif mod_name == "requests" and attr_name in blocking_http_methods:
                                    violations.append(
                                        f"{file_path}:{child.lineno}:sync_requests_in_async:{node.name}"
                                    )
            elif isinstance(node, ast.ExceptHandler):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Return):
                    ret_val = node.body[0].value
                    if ret_val is None or (isinstance(ret_val, ast.Constant) and ret_val.value is None):
                        violations.append(f"{file_path}:{node.lineno}:silent_exception_return_none")

    return {"passed": len(violations) == 0, "violations": violations}

