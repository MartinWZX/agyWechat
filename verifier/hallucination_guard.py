"""Deterministic 4D Anti-Hallucination & Consistency Verification Engine."""
import ast
import fnmatch
import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sandbox.runner import SandboxExecutionError, run_code_sync

MAX_CACHED_FILE_BYTES = 2 * 1024 * 1024
HASH_CHUNK_BYTES = 65536


class UnderExecutionHallucinationError(Exception):
    """Raised when agent claims to have created/modified files or commands but physical state didn't change."""
    def __init__(self, message: str = "Under-execution detected"):
        super().__init__(message)


class OverExecutionHallucinationError(Exception):
    """Raised when agent modified undeclared files outside allowed scope."""
    def __init__(self, message: str = "Over-execution detected"):
        super().__init__(message)


class LogicalContradictionError(Exception):
    """Raised when output violates the 4-stage contract or contradicts raw data anchors."""
    def __init__(self, message: str = "Logical contradiction detected"):
        super().__init__(message)


class ShadowDryRunError(Exception):
    """Raised when crystallized script fails syntax, anti-mock check, or out-of-process sandbox dry-run."""
    def __init__(self, message: str = "Shadow dry-run failed"):
        super().__init__(message)


def _has_python_ast_stubs(source: str) -> Optional[str]:
    """Check Python source via AST for prohibited `pass` or `...` Ellipsis stubs."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"AST syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Pass):
            return f"line {node.lineno}: prohibited 'pass' stub"
        if isinstance(node, ast.Constant) and node.value is Ellipsis:
            return f"line {node.lineno}: prohibited '...' (Ellipsis) stub"
    return None


class WorkspaceSnapshotDiffer:
    """Computes SHA-256 physical filesystem snapshots to detect under/over execution."""

    def __init__(self, workspace_dir: str, ignore_patterns: Optional[List[str]] = None):
        self.workspace_dir = workspace_dir
        self.ignore_patterns = ignore_patterns or [
            "*.pyc", "__pycache__", ".git", "*.db*", "*.log", "*.tmp",
            "tool_execution_trace.jsonl", ".pytest_cache", "venv", ".venv",
            ".context_tokens.json",
        ]
        self._pre_file_cache: Dict[str, bytes] = {}

    def _should_ignore(self, rel_path: str) -> bool:
        parts = rel_path.replace("\\", "/").split("/")
        for pat in self.ignore_patterns:
            if fnmatch.fnmatch(rel_path, pat):
                return True
            if any(fnmatch.fnmatch(part, pat) for part in parts):
                return True
        return False

    def take_snapshot(self) -> Dict[str, str]:
        """Take a streaming SHA-256 snapshot of workspace files with bounded byte cache."""
        snapshot: Dict[str, str] = {}
        self._pre_file_cache.clear()
        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [
                d for d in dirs
                if not self._should_ignore(os.path.relpath(os.path.join(root, d), self.workspace_dir))
            ]
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), self.workspace_dir)
                if self._should_ignore(rel_path):
                    continue
                full_path = os.path.join(root, file)
                try:
                    size = os.path.getsize(full_path)
                    hasher = hashlib.sha256()
                    chunks: List[bytes] = []
                    cache_bytes = size <= MAX_CACHED_FILE_BYTES
                    with open(full_path, "rb") as f:
                        while True:
                            buf = f.read(HASH_CHUNK_BYTES)
                            if not buf:
                                break
                            hasher.update(buf)
                            if cache_bytes:
                                chunks.append(buf)
                    snapshot[rel_path] = hasher.hexdigest()
                    if cache_bytes:
                        self._pre_file_cache[rel_path] = b"".join(chunks)
                except OSError:
                    continue
        return snapshot

    def compute_diff(self, pre_snap: Dict[str, str], post_snap: Dict[str, str]) -> Dict[str, Set[str]]:
        """Compute sets of created, modified, and deleted files between two snapshots."""
        created = set(post_snap.keys()) - set(pre_snap.keys())
        deleted = set(pre_snap.keys()) - set(post_snap.keys())
        modified = {
            f for f in (set(pre_snap.keys()) & set(post_snap.keys()))
            if pre_snap[f] != post_snap[f]
        }
        return {"created": created, "modified": modified, "deleted": deleted}

    def audit_manifest(
        self,
        pre_snap: Dict[str, str],
        post_snap: Dict[str, str],
        manifest: Dict[str, Any],
        allowed_scope: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """Audit agent execution manifest against physical reality."""
        diff = self.compute_diff(pre_snap, post_snap)
        claimed_created = set(manifest.get("files_created", []))
        claimed_modified = set(manifest.get("files_modified", []))

        for cf in claimed_created:
            if cf not in diff["created"]:
                raise UnderExecutionHallucinationError(
                    f"Claimed to create '{cf}', but file was not created physically!"
                )
            self._verify_no_stubs_or_placeholders(cf, "create")

        for mf in claimed_modified:
            if mf not in diff["modified"]:
                raise UnderExecutionHallucinationError(
                    f"Claimed to modify '{mf}', but no physical hash change detected!"
                )
            self._verify_no_stubs_or_placeholders(mf, "modify")

        claimed_deleted = set(manifest.get("files_deleted", []))
        for df in claimed_deleted:
            if df not in diff["deleted"]:
                raise UnderExecutionHallucinationError(
                    f"Claimed to delete '{df}', but file was not deleted physically!"
                )

        actual_all_changed = diff["created"] | diff["modified"] | diff["deleted"]
        claimed_all = claimed_created | claimed_modified | claimed_deleted
        undeclared_changes = actual_all_changed - claimed_all

        if allowed_scope is not None:
            out_of_scope = {
                p for p in claimed_all
                if not any(p == s or p.startswith(s.rstrip("/") + "/") for s in allowed_scope)
            }
            if out_of_scope:
                self.rollback_undeclared(pre_snap, out_of_scope)
                raise OverExecutionHallucinationError(
                    f"Agent modified files outside allowed_scope: {out_of_scope}. Rolled back."
                )

        if undeclared_changes:
            self.rollback_undeclared(pre_snap, undeclared_changes)
            raise OverExecutionHallucinationError(
                f"Agent made undeclared changes to: {undeclared_changes}. Automatically rolled back."
            )

        return True, "Physical snapshot audit passed: 100% action-speech consistency."

    def _verify_no_stubs_or_placeholders(self, rel_path: str, action: str) -> None:
        full_path = os.path.join(self.workspace_dir, rel_path)
        if not os.path.isfile(full_path):
            return
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if re.search(r"\b(?:TODO|FIXME)\b", content):
            raise UnderExecutionHallucinationError(
                f"Claimed {action} file '{rel_path}' contains lazy TODO/FIXME placeholders!"
            )
        if rel_path.endswith(".py"):
            stub_issue = _has_python_ast_stubs(content)
            if stub_issue:
                raise UnderExecutionHallucinationError(
                    f"Claimed {action} Python file '{rel_path}' contains stub ({stub_issue})!"
                )

    def rollback_undeclared(self, pre_snap: Dict[str, str], undeclared_files: Set[str]) -> None:
        """Deterministically revert undeclared files to restore initial state."""
        for f in undeclared_files:
            full_path = os.path.join(self.workspace_dir, f)
            if f in pre_snap and f in self._pre_file_cache:
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as restore_f:
                    restore_f.write(self._pre_file_cache[f])
            elif f not in pre_snap and os.path.exists(full_path):
                os.remove(full_path)


class LogicalConsistencyVerifier:
    """Verifies mandatory 4-stage output contract and anchor-to-conclusion polarity/thresholds."""

    HEALTHY_STATUSES = {"NORMAL", "OK", "HEALTHY", "PASS", "PASSED", "GREEN"}
    CRITICAL_STATUSES = {"CRITICAL", "CRITICAL_FAILURE", "DOWN", "FATAL", "ERROR", "SPIKE_DETECTED"}
    FAILURE_CLAIMS = ("CRITICAL_FAILURE", "FATAL_OUTAGE", "SYSTEM_DOWN", "满载超时", "全面崩溃", "系统宕机")
    ALL_HEALTHY_CLAIMS = ("ALL_SYSTEMS_NORMAL", "100% 正常无异常", "状态完全正常", "无任何异常")

    @classmethod
    def verify(cls, raw_manifest: Dict[str, Any], output_text: str) -> Tuple[bool, str]:
        facts_match = re.search(r"<facts_anchor>(.*?)</facts_anchor>", output_text, re.DOTALL)
        causal_match = re.search(r"<causal_chain>(.*?)</causal_chain>", output_text, re.DOTALL)
        conclusion_match = re.search(r"<final_conclusion>(.*?)</final_conclusion>", output_text, re.DOTALL)

        if not (facts_match and causal_match and conclusion_match):
            raise LogicalContradictionError(
                "Output does not conform to mandatory anchor-bound contract (<facts_anchor>, <causal_chain>, <final_conclusion>)."
            )

        causal_text = causal_match.group(1).strip()
        conclusion_text = conclusion_match.group(1).strip()
        conclusion_upper = conclusion_text.upper()

        anchors = raw_manifest.get("anchors", {})
        for anchor_id, val in anchors.items():
            if anchor_id not in causal_text and anchor_id not in facts_match.group(1):
                continue
            if not isinstance(val, dict):
                continue

            status = str(val.get("status", "")).upper()
            if status in cls.HEALTHY_STATUSES:
                if any(claim in conclusion_upper or claim in conclusion_text for claim in cls.FAILURE_CLAIMS):
                    raise LogicalContradictionError(
                        f"Anchor {anchor_id} status is {status}, but conclusion asserts critical failure!"
                    )
            elif status in cls.CRITICAL_STATUSES:
                if any(claim in conclusion_upper or claim in conclusion_text for claim in cls.ALL_HEALTHY_CLAIMS):
                    raise LogicalContradictionError(
                        f"Anchor {anchor_id} status is {status}, but conclusion claims all systems are healthy!"
                    )

            value = val.get("value")
            max_threshold = val.get("threshold_max", val.get("max"))
            if isinstance(value, (int, float)) and isinstance(max_threshold, (int, float)):
                if value <= max_threshold and ("EXCEEDED_THRESHOLD" in conclusion_upper or "超限告警" in conclusion_text):
                    raise LogicalContradictionError(
                        f"Anchor {anchor_id} value ({value}) <= threshold ({max_threshold}), but conclusion claims threshold breach!"
                    )

        return True, "Logical consistency verified."


class ShadowDryRunValidator:
    """Validates generated scripts for zero-stub AST, security policy, and out-of-process sandbox dry-run."""

    @staticmethod
    def validate_script(
        script_code: str,
        test_inputs: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Tuple[bool, str]:
        stub_issue = _has_python_ast_stubs(script_code)
        if stub_issue:
            raise ShadowDryRunError(f"Script failed AST check: {stub_issue}")

        try:
            result = run_code_sync(
                script_code,
                entrypoint=None,
                inputs=test_inputs,
                timeout=timeout,
                allow_network=False,
                filename="<shadow_dry_run>",
            )
        except SandboxExecutionError as e:
            raise ShadowDryRunError(f"Sandbox security rejection: {e}") from e

        if not result.ok:
            raise ShadowDryRunError(f"Out-of-process sandbox dry-run failed: {result.error_summary()}")

        return True, "Shadow dry-run passed in isolated child sandbox."
