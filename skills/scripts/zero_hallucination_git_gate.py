"""Level 0 Deterministic Script: Zero-Hallucination AST & Git Zero-Leakage Gate."""
from typing import Any, Dict, List, Optional
from tools.tool_registry import tool_ast_contract_audit, tool_git_zero_leakage_audit


def run(
    workspace_dir: str = ".",
    target_files: Optional[List[str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Execute both Git staged zero-leakage gate and Python AST contract audit."""
    git_report = tool_git_zero_leakage_audit(workspace_dir=workspace_dir)
    ast_report = tool_ast_contract_audit(paths=target_files or [])
    return {
        "passed": bool(git_report["passed"] and ast_report["passed"]),
        "git_gate": git_report,
        "ast_audit": ast_report,
    }
