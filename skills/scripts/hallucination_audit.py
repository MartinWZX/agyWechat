"""L0 Deterministic Script: Hallucination Audit Report."""
from memory.core_state_store import CoreStateStore

def run_audit() -> str:
    store = CoreStateStore()
    stats = store.get_stats()
    h_stats = stats.get("hallucinations", {})
    lines = ["【🔍 全链路反幻觉审计记录】"]
    if not h_stats:
        lines.append("暂无幻觉拦截事件，系统物理状态 100% 言行合一。")
    else:
        for h_type, cnt in h_stats.items():
            lines.append(f"• 拦截类型 [{h_type}]: {cnt} 次")
    return "\n".join(lines)

if __name__ == "__main__":
    print(run_audit())
