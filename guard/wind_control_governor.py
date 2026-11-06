"""Multi-Engine Wind Control Governor (Anti-429 & Anti-403 Defense)."""
import time
import random
import sqlite3
from typing import Dict, Any, Tuple

class WindControlGovernor:
    def __init__(self, db_path: str = "memory/agent_core.db", config: Dict[str, Any] = None):
        self.db_path = db_path
        self.config = config or {}
        wc = self.config.get("wind_control", {})
        self.min_interval = wc.get("min_call_interval_seconds", 12.0)
        self.window_5m_max = wc.get("limits", {}).get("window_5m_max_calls", 6)
        self.window_1h_max = wc.get("limits", {}).get("window_1h_max_calls", 30)
        self.jitter_min = wc.get("human_cadence", {}).get("min_jitter_seconds", 1.5)
        self.jitter_max = wc.get("human_cadence", {}).get("max_jitter_seconds", 4.8)
        self.quiet_hours_enabled = wc.get("quiet_hours", {}).get("enabled", True)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def is_quiet_hours(self) -> bool:
        if not self.quiet_hours_enabled:
            return False
        import datetime
        now = datetime.datetime.now().time()
        start = datetime.time(1, 0)
        end = datetime.time(7, 0)
        return start <= now <= end

    def check_engine_availability(self, engine_id: str, is_p0_urgent: bool = False) -> Tuple[bool, str]:
        # 1. Quiet hours check
        if self.is_quiet_hours() and not is_p0_urgent:
            return False, "Quiet hours active (01:00-07:00). Routine cognitive calls paused."

        now = time.time()
        with self._get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT state, cooldown_until, last_call_timestamp FROM engine_health_states WHERE engine_id = ?;", (engine_id,))
            row = c.fetchone()
            if not row:
                return True, "OK"
            state = row["state"]
            cooldown_until = row["cooldown_until"]
            last_call = row["last_call_timestamp"]

            # Circuit breaker checks
            if state == "LOCKDOWN_403":
                if now < cooldown_until:
                    return False, f"Engine {engine_id} is in 403 RED LOCKDOWN until {cooldown_until}."
                else:
                    # Reset after 24h
                    c.execute("UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0 WHERE engine_id = ?;", (engine_id,))
                    conn.commit()

            if state == "COOLDOWN_429":
                if now < cooldown_until:
                    return False, f"Engine {engine_id} is in 429 YELLOW COOLDOWN until {cooldown_until}."
                else:
                    c.execute("UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0 WHERE engine_id = ?;", (engine_id,))
                    conn.commit()

            # Min interval check
            if now - last_call < self.min_interval:
                return False, f"Call interval too short (< {self.min_interval}s). Throttled."

            # Sliding window checks
            five_min_ago = now - 300
            one_hour_ago = now - 3600
            c.execute("SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;", (engine_id, five_min_ago))
            count_5m = c.fetchone()[0]
            if count_5m >= self.window_5m_max:
                return False, f"Engine {engine_id} reached 5-min rate limit ({count_5m}/{self.window_5m_max})."

            c.execute("SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;", (engine_id, one_hour_ago))
            count_1h = c.fetchone()[0]
            if count_1h >= self.window_1h_max:
                return False, f"Engine {engine_id} reached 1-hour rate limit ({count_1h}/{self.window_1h_max})."

        return True, "OK"

    def record_call(self, engine_id: str):
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("UPDATE engine_health_states SET last_call_timestamp = ?, total_calls_today = total_calls_today + 1 WHERE engine_id = ?;", (now, engine_id))
            conn.execute("INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, 'CALL', 'NORMAL_CALL', ?);", (engine_id, now))
            conn.commit()

    def apply_human_jitter(self):
        jitter = random.uniform(self.jitter_min, self.jitter_max)
        time.sleep(jitter)

    def trigger_429_backoff(self, engine_id: str):
        cooldown_duration = 300 # 5 minutes default
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("UPDATE engine_health_states SET state = 'COOLDOWN_429', cooldown_until = ?, failover_count = failover_count + 1 WHERE engine_id = ?;", (now + cooldown_duration, engine_id))
            conn.execute("INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, '429_BACKOFF', ?, ?);", (engine_id, f"Cooldown for {cooldown_duration}s", now))
            conn.commit()

    def trigger_403_lockdown(self, engine_id: str):
        lockdown_duration = 86400 # 24 hours
        now = time.time()
        with self._get_conn() as conn:
            conn.execute("UPDATE engine_health_states SET state = 'LOCKDOWN_403', cooldown_until = ?, failover_count = failover_count + 1 WHERE engine_id = ?;", (now + lockdown_duration, engine_id))
            conn.execute("INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, '403_LOCKDOWN', '24h RED LOCKDOWN', ?);", (engine_id, now))
            conn.commit()
