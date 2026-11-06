"""System Telemetry, Health Observability & Wind Control Dashboard."""
from typing import Dict, Any
from memory.core_state_store import CoreStateStore

class HealthMonitor:
    def __init__(self, db_path: str = "memory/agent_core.db"):
        self.store = CoreStateStore(db_path)

    def get_dashboard_summary(self) -> Dict[str, Any]:
        return self.store.get_stats()
