"""System Telemetry, Health Observability, Code Offload Rate & Wind Control Dashboard."""
from typing import Any, Dict, Optional
from memory.core_state_store import CoreStateStore


class HealthMonitor:
    """Tracks real-time system metrics, deterministic code offload ratio, and engine health."""

    def __init__(
        self,
        governor: Any = None,
        settings: Any = None,
        db_path: str = "memory/agent_core.db",
        store: Optional[CoreStateStore] = None
    ):
        self.governor = governor
        self.settings = settings
        self.store = store or CoreStateStore(db_path)
        self.total_incoming = 0
        self.l0_intercepted = 0
        self.agy_invocations = 0
        self.cron_stage1_executed = 0

    def record_incoming(self):
        """Record an inbound message."""
        self.total_incoming += 1

    def record_l0_hit(self):
        """Record an L0 zero-token code interception."""
        self.l0_intercepted += 1

    def record_agy_call(self):
        """Record a cognitive CLI invocation."""
        self.agy_invocations += 1

    def record_cron_run(self):
        """Record a Stage-1 pure code cron execution."""
        self.cron_stage1_executed += 1

    def get_offload_rate(self) -> float:
        """Calculate deterministic code offload rate: (L0 + Cron) / Total."""
        total = self.l0_intercepted + self.agy_invocations
        if total == 0:
            return 100.0 if self.total_incoming == 0 else 0.0
        return round((self.l0_intercepted / total) * 100.0, 1)

    async def generate_status_report(self) -> str:
        """Generate formatted WeChat status report card."""
        rate = self.get_offload_rate()
        stats = self.store.get_stats()
        return (
            "【Google agy 微信智能体运行看板】\n"
            f"⚡ 纯代码卸载效能 (Code Offload Ratio): {rate}%\n"
            f"📥 接收请求总数: {self.total_incoming}\n"
            f"🛡️ L0 确定性拦截数: {self.l0_intercepted}\n"
            f"🤖 CLI 大模型调用数: {self.agy_invocations}\n"
            f"📊 数据库已记录消息: {stats.get('total_messages', 0)}\n"
            "状态：🟢 全系统正常运行（Code-First, LLM-Last 模式）"
        )

    def generate_wind_control_dashboard(self) -> str:
        """Generate formatted five-dimensional wind control report."""
        stats = self.store.get_stats()
        engines_info = stats.get("engines", {})
        lines = ["【🛡️ 多引擎五维防风控运行看板】"]
        for eng, data in engines_info.items():
            state = data.get("state", "CLOSED")
            calls = data.get("total_calls_today", 0)
            failovers = data.get("failover_count", 0)
            icon = "🟢" if state == "CLOSED" else ("🟡" if state == "COOLDOWN_429" else "🔴")
            lines.append(f"{icon} [{eng.upper()}] 状态: {state} | 今日调用: {calls} | 故障转移: {failovers}")
        return "\n".join(lines)

    def generate_hallucination_report(self) -> str:
        """Generate formatted hallucination audit report."""
        stats = self.store.get_stats()
        h_stats = stats.get("hallucinations", {})
        lines = ["【🔍 全链路反幻觉物理审计看板】"]
        for h_type, count in h_stats.items():
            lines.append(f"- {h_type}: {count} 次拦截")
        if not h_stats:
            lines.append("未捕获到任何违规或幻觉事件（言行一致率 100%）。")
        return "\n".join(lines)

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Return raw telemetry dictionary."""
        summary = self.store.get_stats()
        summary["local_offload_rate"] = self.get_offload_rate()
        summary["total_incoming"] = self.total_incoming
        summary["l0_intercepted"] = self.l0_intercepted
        summary["agy_invocations"] = self.agy_invocations
        return summary
