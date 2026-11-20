"""Real Host System Telemetry & Pure Two-State Gateway Observability."""
import asyncio
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from memory.core_state_store import CoreStateStore


def _read_proc_meminfo() -> Dict[str, int]:
    """Read MemTotal and MemAvailable (in KiB) from /proc/meminfo on Linux."""
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, rest = line.split(":", 1)
                parts = rest.strip().split()
                if parts and parts[0].isdigit():
                    info[k] = int(parts[0])
    except OSError:
        return {}
    return info


def _collect_host_metrics(workspace_dir: str) -> Dict[str, Any]:
    """Collect real OS CPU load, RAM, and workspace disk utilization."""
    metrics: Dict[str, Any] = {}
    try:
        l1, l5, l15 = os.getloadavg()
        metrics["load_avg"] = (round(l1, 2), round(l5, 2), round(l15, 2))
    except OSError:
        metrics["load_avg"] = None

    mem = _read_proc_meminfo()
    total_kib = mem.get("MemTotal", 0)
    avail_kib = mem.get("MemAvailable", mem.get("MemFree", 0))
    if total_kib > 0:
        used_kib = max(0, total_kib - avail_kib)
        metrics["mem_total_gb"] = round(total_kib / (1024 * 1024), 2)
        metrics["mem_avail_gb"] = round(avail_kib / (1024 * 1024), 2)
        metrics["mem_used_gb"] = round(used_kib / (1024 * 1024), 2)
        metrics["mem_pct"] = round(used_kib / total_kib * 100.0, 1)
    else:
        metrics["mem_total_gb"] = None

    disk_target = workspace_dir if os.path.exists(workspace_dir) else "."
    try:
        du = shutil.disk_usage(disk_target)
        metrics["disk_total_gb"] = round(du.total / (1024 ** 3), 1)
        metrics["disk_free_gb"] = round(du.free / (1024 ** 3), 1)
        metrics["disk_used_pct"] = round(du.used / max(1, du.total) * 100.0, 1)
    except OSError:
        metrics["disk_total_gb"] = None
    return metrics


def _format_uptime(seconds: float) -> str:
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


class HealthMonitor:
    """Collects live host hardware metrics, gateway message stats, and installed engine counts."""

    def __init__(
        self,
        settings: Any = None,
        db_path: str = "memory/agent_core.db",
        store: Optional[CoreStateStore] = None,
    ):
        self.settings = settings
        self.store = store or CoreStateStore(db_path)
        self.start_time = time.time()
        self.total_incoming = 0
        self.command_hits = 0
        self.engine_calls: Dict[str, int] = {"agy": 0, "pi": 0, "claude": 0, "omp": 0}

    def record_incoming(self) -> None:
        self.total_incoming += 1

    def record_command_hit(self) -> None:
        self.command_hits += 1

    def record_engine_call(self, engine_id: str) -> None:
        self.engine_calls[engine_id] = self.engine_calls.get(engine_id, 0) + 1

    async def generate_status_report(
        self,
        hub: Any = None,
        workspace_dir: str = ".",
        active_engine: str = "",
    ) -> str:
        """Build a pure host hardware & installed-engine status report (zero wind-control items)."""
        host = await asyncio.to_thread(_collect_host_metrics, workspace_dir)
        stats = await asyncio.to_thread(self.store.get_stats)
        uptime_str = _format_uptime(time.time() - self.start_time)

        load = host.get("load_avg")
        load_str = f"{load[0]} / {load[1]} / {load[2]} (1m / 5m / 15m)" if load else "N/A"
        if host.get("mem_total_gb") is not None:
            mem_str = (
                f"可用 {host['mem_avail_gb']} GB / 总计 {host['mem_total_gb']} GB "
                f"(已用 {host['mem_used_gb']} GB, 使用率 {host['mem_pct']}%)"
            )
        else:
            mem_str = "N/A"
        if host.get("disk_total_gb") is not None:
            disk_str = (
                f"剩余 {host['disk_free_gb']} GB / 总计 {host['disk_total_gb']} GB "
                f"(使用率 {host['disk_used_pct']}%)"
            )
        else:
            disk_str = "N/A"

        installed_list: List[str] = (
            hub.get_installed_engines() if hub and hasattr(hub, "get_installed_engines") else []
        )
        installed_set = set(installed_list)
        installed_str = ", ".join(f"/{e}" for e in installed_list) if installed_list else "无已安装引擎"
        db_counts = stats.get("engine_message_counts") or {}

        lines = [
            "📊 【宿主机与网关运行状态】",
            f"⏱️ 网关运行时长: {uptime_str}",
            f"💻 CPU 负载: {load_str}",
            f"🧠 物理内存: {mem_str}",
            f"💾 工作区磁盘: {disk_str}",
            f"📁 当前工作区: {workspace_dir}",
            f"🧭 当前状态: {'引擎态 (' + active_engine.upper() + ')' if active_engine else '非引擎网关路由态'}",
            f"🔌 已安装引擎: {installed_str}",
            f"📨 消息统计: 本次启动入站 {self.total_incoming} 条 | 累计落库 {stats.get('total_messages', 0)} 条 (语音 {stats.get('voice_messages', 0)} 条)",
            "",
            "🤖 【四引擎安装与调用统计】",
        ]
        for eid in ("agy", "pi", "claude", "omp"):
            inst_badge = "🟢 已安装" if eid in installed_set else "⚪ 未安装"
            runtime_cnt = self.engine_calls.get(eid, 0)
            total_cnt = int(db_counts.get(eid, 0))
            lines.append(f"• /{eid}: {inst_badge} | 本次调用 {runtime_cnt} 次 | 历史落库 {total_cnt} 次")

        return "\n".join(lines)
