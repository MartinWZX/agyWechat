"""Context-Anchored Rate-Limit (429) and Lockdown (403) Error Classifier.

Prevents catastrophic false-positive circuit-breaker trips when normal CLI stdout
happens to contain numbers like '4290 lines' or 'port 4030'.
"""
import json
import re
from enum import Enum
from typing import Optional

ANCHORED_429_RE = re.compile(
    r"(?:"
    r"\bHTTP[/ ]\d(?:\.\d)?[\"']?\s+429\b"
    r"|\"(?:status|code|http_status)\"\s*:\s*429\b"
    r"|\b(?:status[_\s-]*code|error[_\s-]*code|http[_\s-]*error)[\s:=]+429\b"
    r"|\brate_limit_error\b"
    r"|\brate\s+limit\s+exceeded\b"
    r"|\bRESOURCE_EXHAUSTED\b"
    r"|Resource has been exhausted"
    r"|\bToo Many Requests\b"
    r"|\bquota\s+exceeded\b"
    r")",
    re.IGNORECASE,
)

ANCHORED_403_RE = re.compile(
    r"(?:"
    r"\bHTTP[/ ]\d(?:\.\d)?[\"']?\s+403\b"
    r"|\"(?:status|code|http_status)\"\s*:\s*403\b"
    r"|\b(?:status[_\s-]*code|error[_\s-]*code|http[_\s-]*error)[\s:=]+403\b"
    r"|\bPERMISSION_DENIED\b"
    r"|Verify your account(?:\s+to\s+continue)?"
    r"|\bToS\s+Violation\b"
    r"|\bAccount\s+(?:suspended|disabled|banned)\b"
    r")",
    re.IGNORECASE,
)


class FailureKind(str, Enum):
    NONE = "NONE"
    RATE_LIMIT_429 = "RATE_LIMIT_429"
    LOCKDOWN_403 = "LOCKDOWN_403"
    EXEC_ERROR = "EXEC_ERROR"


def _extract_structured_error_text(stdout: str) -> str:
    """Extract error message fields if CLI returned a JSON error payload on stdout."""
    stripped = stdout.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return ""
    try:
        data = json.loads(stripped)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("is_error") is True or "error" in data:
        return json.dumps(data.get("error") or data, ensure_ascii=False)
    return ""


def classify_cli_failure(returncode: int, stdout: str, stderr: str) -> FailureKind:
    """Classify CLI subprocess result using stderr and structured error payloads only."""
    diagnostic_surface = stderr or ""
    if returncode != 0:
        structured = _extract_structured_error_text(stdout or "")
        if structured:
            diagnostic_surface = f"{diagnostic_surface}\n{structured}"

    if ANCHORED_403_RE.search(diagnostic_surface):
        return FailureKind.LOCKDOWN_403
    if ANCHORED_429_RE.search(diagnostic_surface):
        return FailureKind.RATE_LIMIT_429

    if returncode != 0:
        return FailureKind.EXEC_ERROR
    return FailureKind.NONE


def classify_stream_chunk(chunk: str) -> Optional[FailureKind]:
    """Inspect a live PTY stream chunk for anchored 429/403 error signatures."""
    if ANCHORED_403_RE.search(chunk):
        return FailureKind.LOCKDOWN_403
    if ANCHORED_429_RE.search(chunk):
        return FailureKind.RATE_LIMIT_429
    return None
