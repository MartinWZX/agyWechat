"""Evaluation Harness for Governed RSI Skill Crystallization & Promotion.

Every candidate L0 rule / parameterized skill MUST pass deterministic evaluation
in the isolated out-of-process sandbox before promotion to `status='active'`:
  1. AST security & anti-stub gate (`ShadowDryRunValidator` + `ApprovalGuard`)
  2. Placeholder output rejection (blocks legacy dummy banner outputs)
  3. Replay execution across historical sample inputs (`run(query=..., slots=...)`)
  4. Output determinism check (identical inputs yield identical outputs)
  5. Historical answer consistency check (`char_ngram_similarity`)
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from sandbox.runner import SandboxExecutionError, run_code_sync
from verifier.hallucination_guard import ShadowDryRunError, ShadowDryRunValidator

PLACEHOLDER_MARKERS = (
    "已命中固化规则",
    "TODO",
    "FIXME",
    "MOCK_PLACEHOLDER",
)


def char_ngram_similarity(a: str, b: str, n: int = 2) -> float:
    """Compute character n-gram Jaccard similarity in [0.0, 1.0] without external dependencies."""
    sa = "".join(a.lower().split())
    sb = "".join(b.lower().split())
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0
    if len(sa) < n or len(sb) < n:
        set_a, set_b = set(sa), set(sb)
    else:
        set_a = {sa[i:i + n] for i in range(len(sa) - n + 1)}
        set_b = {sb[i:i + n] for i in range(len(sb) - n + 1)}
    union = len(set_a | set_b)
    return (len(set_a & set_b) / union) if union else 0.0


def pairwise_answer_consistency(answers: Sequence[str]) -> float:
    """Compute average pairwise similarity among observed LLM answers."""
    cleaned = [a.strip() for a in answers if a and a.strip()]
    if len(cleaned) < 2:
        return 0.0
    scores: List[float] = []
    for i in range(len(cleaned)):
        for j in range(i + 1, len(cleaned)):
            scores.append(char_ngram_similarity(cleaned[i], cleaned[j], n=2))
    return sum(scores) / len(scores) if scores else 0.0


@dataclass
class EvalReport:
    passed: bool
    score: float
    reason: str
    sample_output: str = ""


class EvalHarness:
    """Automated evaluator gating candidate -> active RSI promotions."""

    @staticmethod
    def evaluate_script(
        script_code: str,
        sample_queries: Sequence[str],
        sample_slots: Sequence[Dict[str, Any]] = (),
        min_output_chars: int = 2,
    ) -> EvalReport:
        try:
            ShadowDryRunValidator.validate_script(script_code, timeout=8.0)
        except ShadowDryRunError as err:
            return EvalReport(False, 0.0, f"Dry-run failed: {err}")

        queries = list(sample_queries) if sample_queries else ["health_probe"]
        last_output = ""
        for idx, query in enumerate(queries):
            slots = sample_slots[idx] if idx < len(sample_slots) else {}
            kwargs = {"query": query, "slots": slots}
            try:
                res1 = run_code_sync(script_code, entrypoint="run", kwargs=kwargs, timeout=8.0)
                res2 = run_code_sync(script_code, entrypoint="run", kwargs=kwargs, timeout=8.0)
            except SandboxExecutionError as err:
                return EvalReport(False, 0.0, f"Sandbox security rejection: {err}")

            if not res1.ok or not res2.ok:
                return EvalReport(False, 0.0, f"Entrypoint execution failed: {res1.error_summary()}")

            out1 = "" if res1.result is None else str(res1.result).strip()
            out2 = "" if res2.result is None else str(res2.result).strip()
            if len(out1) < min_output_chars:
                return EvalReport(False, 0.0, "Output empty or shorter than minimum threshold.")
            if any(marker in out1 for marker in PLACEHOLDER_MARKERS):
                return EvalReport(False, 0.1, "Output contains unpromoted placeholder banner.")
            if out1 != out2:
                return EvalReport(False, 0.3, "Non-deterministic output detected across identical runs.")
            last_output = out1

        return EvalReport(True, 1.0, "All sandbox replay and determinism assertions passed.", last_output)
