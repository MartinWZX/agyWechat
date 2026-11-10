"""L0 Deterministic Script: Wind Control & Rate Limit Dashboard."""
from typing import Any
from memory.core_state_store import CoreStateStore


def run_dashboard(db_path: str = "memory/agent_core.db") -> str:
    store = CoreStateStore(db_path)
    stats = store.get_stats()
    engines = stats.get("engines", {})
    lines = ["【🛡️ 多引擎风控与运行看板】"]
    for eng, data in engines.items():
        state = data.get("state", "UNKNOWN")
        calls = data.get("total_calls_today", 0)
        fo = data.get("failover_count", 0)
        icon = "🟢" if state == "CLOSED" else ("🟡" if "429" in state else "🔴")
        lines.append(f"{icon} {eng.upper()}: 状态={state} | 今日调用={calls}次 | 切换={fo}次")

    offload = round(stats.get("offload_rate", 0.0), 1)
    lines.append(f"\n📊 纯代码卸载率 (L0+Cron): {offload}% (目标 >= 90%)")
    lines.append(f"🎙️ 微信语音交互数: {stats.get('voice_messages', 0)} 条")
    return "\n".join(lines)


def run(db_path: str = "memory/agent_core.db", **kwargs: Any) -> str:
    return run_dashboard(db_path=db_path)


if __name__ == "__main__":
    print(run_dashboard())
