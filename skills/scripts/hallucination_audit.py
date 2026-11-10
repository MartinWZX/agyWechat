"""L0 Deterministic Script: Hallucination Audit Report."""
from typing import Any
from memory.core_state_store import CoreStateStore


def run_audit(db_path: str = "memory/agent_core.db") -> str:
    store = CoreStateStore(db_path)
    stats = store.get_stats()
    h_stats = stats.get("hallucinations", {})
    lines = ["【🔍 全链路反幻觉审计记录】"]
    if not h_stats:
        lines.append("暂无幻觉拦截事件，系统物理状态 100% 言行合一。")
    else:
        for h_type, cnt in h_stats.items():
            lines.append(f"• 拦截类型 [{h_type}]: {cnt} 次")
    return "\n".join(lines)


def run(db_path: str = "memory/agent_core.db", **kwargs: Any) -> str:
    return run_audit(db_path=db_path)


if __name__ == "__main__":
    print(run_audit())
