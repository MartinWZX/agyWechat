"""L0 Deterministic Script: System Status (Pure Python Standard Library)."""
import os
import shutil

def run_status() -> str:
    # 1. CPU Load average
    cpu_load = "N/A"
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_load = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
    except (AttributeError, OSError):
        pass

    # 2. Memory Info from /proc/meminfo
    mem_percent = "N/A"
    try:
        if os.path.exists("/proc/meminfo"):
            mem_info = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem_info[parts[0].strip()] = int(parts[1].split()[0])
            total = mem_info.get("MemTotal", 1)
            avail = mem_info.get("MemAvailable", mem_info.get("MemFree", 0))
            used = total - avail
            mem_percent = f"{(used / total) * 100:.1f}%"
    except Exception:
        pass

    # 3. Disk Usage
    disk_percent = "N/A"
    try:
        total_b, used_b, free_b = shutil.disk_usage("/")
        disk_percent = f"{(used_b / total_b) * 100:.1f}%"
    except Exception:
        pass

    return f"【系统运行状态】\nCPU负载(1/5/15m): {cpu_load}\n内存占用: {mem_percent}\n根分区磁盘: {disk_percent}\n运行状态: 正常运行 (0 Token)"

if __name__ == "__main__":
    print(run_status())
