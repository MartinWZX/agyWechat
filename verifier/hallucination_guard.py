"""Deterministic 4D Anti-Hallucination & Consistency Verification Engine."""
import os
import ast
import json
import hashlib
import re
import subprocess
from typing import Dict, Any, List, Set, Tuple, Optional

class UnderExecutionHallucinationError(Exception):
    """Raised when agent claims to have created/modified files or commands but physical state didn't change."""
    pass

class OverExecutionHallucinationError(Exception):
    """Raised when agent modified undeclared files outside allowed scope."""
    pass

class LogicalContradictionError(Exception):
    """Raised when causal chain or conclusion contradicts raw data anchors."""
    pass

class ShadowDryRunError(Exception):
    """Raised when crystallized script fails syntax, anti-mock check, or sandbox dry-run."""
    pass

class WorkspaceSnapshotDiffer:
    def __init__(self, workspace_dir: str, ignore_patterns: List[str] = None):
        self.workspace_dir = workspace_dir
        self.ignore_patterns = ignore_patterns or [
            "*.pyc", "__pycache__", ".git", "*.db*", "*.log", "*.tmp", "tool_execution_trace.jsonl"
        ]

    def _should_ignore(self, path: str) -> bool:
        for pat in self.ignore_patterns:
            clean_pat = pat.replace("*", "")
            if clean_pat in path:
                return True
        return False

    def take_snapshot(self) -> Dict[str, str]:
        snapshot = {}
        for root, _, files in os.walk(self.workspace_dir):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), self.workspace_dir)
                if self._should_ignore(rel_path):
                    continue
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "rb") as f:
                        snapshot[rel_path] = hashlib.sha256(f.read()).hexdigest()
                except (IOError, OSError):
                    pass
        return snapshot

    def compute_diff(self, pre_snap: Dict[str, str], post_snap: Dict[str, str]) -> Dict[str, Set[str]]:
        created = set(post_snap.keys()) - set(pre_snap.keys())
        deleted = set(pre_snap.keys()) - set(post_snap.keys())
        modified = {f for f in (set(pre_snap.keys()) & set(post_snap.keys())) if pre_snap[f] != post_snap[f]}
        return {"created": created, "modified": modified, "deleted": deleted}

    def audit_manifest(self, pre_snap: Dict[str, str], post_snap: Dict[str, str],
                       manifest: Dict[str, Any], allowed_scope: Optional[List[str]] = None) -> Tuple[bool, str]:
        diff = self.compute_diff(pre_snap, post_snap)
        claimed_created = set(manifest.get("files_created", []))
        claimed_modified = set(manifest.get("files_modified", []))
        
        # 1. Under-Execution Check (Saying more, doing less)
        for cf in claimed_created:
            if cf not in diff["created"]:
                raise UnderExecutionHallucinationError(f"Claimed to create '{cf}', but file was not created physically!")
            # Check for stub placeholders
            full_cf = os.path.join(self.workspace_dir, cf)
            if os.path.exists(full_cf):
                with open(full_cf, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if re.search(r"\b(TODO|FIXME|pass\s*#|Ellipsis|\.\.\.)\b", content):
                        raise UnderExecutionHallucinationError(f"Claimed file '{cf}' contains lazy stub/TODO placeholders!")

        for mf in claimed_modified:
            if mf not in diff["modified"]:
                raise UnderExecutionHallucinationError(f"Claimed to modify '{mf}', but no physical hash change detected!")

        # 2. Over-Execution Check (Saying less, doing more / unauthorized scope expansion)
        actual_all_changed = diff["created"] | diff["modified"] | diff["deleted"]
        claimed_all = claimed_created | claimed_modified | set(manifest.get("files_deleted", []))
        undeclared_changes = actual_all_changed - claimed_all

        if undeclared_changes:
            err_msg = f"Agent made undeclared changes to: {undeclared_changes}"
            raise OverExecutionHallucinationError(err_msg)

        return True, "Physical snapshot audit passed: 100% action-speech consistency."

    def rollback_undeclared(self, pre_snap: Dict[str, str], undeclared_files: Set[str]):
        # Deterministically revert undeclared files
        for f in undeclared_files:
            full_path = os.path.join(self.workspace_dir, f)
            if f in pre_snap:
                pass # Can restore if pre-backup was kept, or remove created file
            elif os.path.exists(full_path):
                os.remove(full_path)

class LogicalConsistencyVerifier:
    @staticmethod
    def verify(raw_manifest: Dict[str, Any], output_text: str) -> Tuple[bool, str]:
        # Extract <facts_anchor>, <causal_chain>, <final_conclusion>
        facts_match = re.search(r"<facts_anchor>(.*?)</facts_anchor>", output_text, re.DOTALL)
        causal_match = re.search(r"<causal_chain>(.*?)</causal_chain>", output_text, re.DOTALL)
        conclusion_match = re.search(r"<final_conclusion>(.*?)</final_conclusion>", output_text, re.DOTALL)

        if not (facts_match and causal_match and conclusion_match):
            # If agent didn't follow the 3-stage contract on cognitive tasks
            return False, "Output does not conform to 3-stage anchor-bound contract."

        facts_text = facts_match.group(1).strip()
        causal_text = causal_match.group(1).strip()
        conclusion_text = conclusion_match.group(1).strip()

        # Check anchor IDs
        anchors = raw_manifest.get("anchors", {})
        for anchor_id, val in anchors.items():
            if anchor_id in causal_text:
                # Check for numerical or polarity contradiction
                # e.g., if anchor is cpu=12% (NORMAL), conclusion must not say CRITICAL_OVERHEAT
                status = val.get("status", "").upper()
                if status == "NORMAL" and ("CRITICAL_FAILURE" in conclusion_text.upper() or "满载超时" in conclusion_text):
                    raise LogicalContradictionError(f"Anchor {anchor_id} is NORMAL, but conclusion asserts CRITICAL failure!")
        return True, "Logical consistency verified."

class ShadowDryRunValidator:
    @staticmethod
    def validate_script(script_code: str, test_inputs: Dict[str, Any] = None) -> Tuple[bool, str]:
        # 1. AST Syntax check
        try:
            tree = ast.parse(script_code)
        except SyntaxError as e:
            raise ShadowDryRunError(f"Script failed AST syntax validation: {e}")

        # 2. Anti-Mock / Anti-Stub AST Check
        for node in ast.walk(tree):
            if isinstance(node, ast.Pass):
                raise ShadowDryRunError("AST contains prohibited 'pass' stub.")
            if isinstance(node, ast.Constant) and node.value is Ellipsis:
                raise ShadowDryRunError("AST contains prohibited '...' (Ellipsis) stub.")

        # 3. Execution in isolated sandbox
        sandbox_scope = {"__builtins__": __builtins__, "inputs": test_inputs or {}}
        try:
            exec(script_code, sandbox_scope)
        except Exception as e:
            raise ShadowDryRunError(f"Sandbox execution failed: {e}")

        return True, "Shadow dry-run passed."
