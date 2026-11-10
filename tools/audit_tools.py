"""Built-in Deterministic Git Zero-Leakage & Python AST Anti-Hallucination Audit Tools."""
import ast
import json
import os
import re
import subprocess
from typing import Any, Dict, List


def run_git_zero_leakage_audit(workspace_dir: str = ".") -> Dict[str, Any]:
    """Audit Git staged files, notebooks, and added diff lines against zero-leakage security redlines."""
    forbidden_path_re = re.compile(
        r"(^|/)\.env(\..+)?$|\.(pem|key|p12|pfx|crt|keystore|db|sqlite|sqlite3|sql|bak|csv|tsv|xlsx|xls|parquet|feather|pkl|pickle|joblib|h5|jsonl|pt|pth|bin|onnx|safetensors|gguf|log|tfstate|tfvars)$|(^|/)(chroma_db|faiss_index|qdrant_storage|milvus_data|lancedb|\.cursor|\.claude|\.windsurf|\.continue|\.aws|\.kube)/|(^|/)(id_rsa|id_ed25519|dump\.rdb|nohup\.out)|(^|/)(scratch|temp|debug_|test_real_)[^/]*\.py$|(?:^|/)[^/]*(?:credentials|service-account|client_secret)[^/]*\.json$"
    )
    secret_line_re = re.compile(
        r"(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,}|AKIA[0-9A-Z]{16}|xoxb-[0-9A-Za-z-]{20,}|AIza[0-9A-Za-z_-]{35}|BEGIN (?:RSA|OPENSSH|EC|PRIVATE) KEY|(?:postgresql|mysql|redis|mongodb(?:\+srv)?)://[^/\s:@]+:[^/\s@]+@)"
    )

    proc_names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace_dir, capture_output=True, text=True, check=False,
    )
    staged_files = [line.strip() for line in proc_names.stdout.splitlines() if line.strip()]
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
                    if any(c.get("outputs") or c.get("execution_count") is not None for c in nb_data.get("cells", [])):
                        uncleaned_notebooks.append(rel_path)
                except (OSError, ValueError):
                    uncleaned_notebooks.append(rel_path)

    proc_diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        cwd=workspace_dir, capture_output=True, text=True, check=False,
    )
    secret_hit_lines = [
        f"diff_line_{idx}"
        for idx, line in enumerate(proc_diff.stdout.splitlines(), start=1)
        if line.startswith("+") and not line.startswith("+++") and secret_line_re.search(line)
    ]

    passed = not blocked_files and not uncleaned_notebooks and not secret_hit_lines
    return {
        "passed": passed,
        "staged_files": staged_files,
        "blocked_files": blocked_files,
        "uncleaned_notebooks": uncleaned_notebooks,
        "secret_hit_lines": secret_hit_lines,
    }


def run_ast_contract_audit(paths: List[str]) -> Dict[str, Any]:
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
                all_defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                for default_node in all_defaults:
                    if isinstance(default_node, mutable_types):
                        violations.append(f"{file_path}:{node.lineno}:mutable_default_arg:{node.name}")
                if isinstance(node, ast.AsyncFunctionDef):
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                            if isinstance(child.func.value, ast.Name):
                                mod_name, attr_name = child.func.value.id, child.func.attr
                                if mod_name == "time" and attr_name == "sleep":
                                    violations.append(f"{file_path}:{child.lineno}:sync_sleep_in_async:{node.name}")
                                elif mod_name == "requests" and attr_name in blocking_http_methods:
                                    violations.append(f"{file_path}:{child.lineno}:sync_requests_in_async:{node.name}")
            elif isinstance(node, ast.ExceptHandler):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Return):
                    ret_val = node.body[0].value
                    if ret_val is None or (isinstance(ret_val, ast.Constant) and ret_val.value is None):
                        violations.append(f"{file_path}:{node.lineno}:silent_exception_return_none")

    return {"passed": len(violations) == 0, "violations": violations}
