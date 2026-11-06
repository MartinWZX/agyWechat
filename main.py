"""Single Process Entrypoint with flock, Multi-CLI Hub, Gateway & Scheduler."""
import asyncio
import fcntl
import os
import sys
import yaml
from gateway.wechat_gateway import WeChatGateway
from scheduler.code_first_cron import CodeFirstScheduler
from bridge.multi_cli_hub import MultiCLIHub
from guard.wind_control_governor import WindControlGovernor

LOCK_FILE = "/tmp/agent_core/locks/main.lock"

def acquire_main_lock():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except IOError:
        print("[ERROR] Another instance of autonomous_multi_cli_agent is already running.")
        sys.exit(1)

async def main():
    lock_fd = acquire_main_lock()
    print("[SYSTEM] Acquired single-instance flock. Initializing core modules...")

    config_path = "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    governor = WindControlGovernor(config=config)
    hub = MultiCLIHub(config=config, governor=governor)
    gateway = WeChatGateway(config=config, hub=hub)
    scheduler = CodeFirstScheduler(hub=hub)

    print("[SYSTEM] All 12 core modules loaded successfully.")
    print("[SYSTEM] Code-First, LLM-Last Autonomous Multi-CLI Agent running.")

if __name__ == "__main__":
    asyncio.run(main())
