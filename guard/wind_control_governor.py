"""Multi-Engine Wind Control Governor (Anti-429 & Anti-403 Defense)."""
import asyncio
import datetime
import random
import sqlite3
import time
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class CircuitState(str, Enum):
    """Tri-state circuit breaker states."""
    CLOSED = "CLOSED"
    COOLDOWN_429 = "COOLDOWN_429"
    LOCKDOWN_403 = "LOCKDOWN_403"


class WindControlGovernor:
    """Multi-Engine Wind Control Guard implementing Five-Dimensional Defense."""

    def __init__(
        self,
        store_or_db: Any = None,
        config_or_settings: Any = None,
        db_path: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        # Resolve DB Path
        if hasattr(store_or_db, "db_path"):
            self.db_path = str(store_or_db.db_path)
            self.store = store_or_db
        elif isinstance(store_or_db, (str, bytes)):
            self.db_path = str(store_or_db)
            self.store = None
        elif db_path:
            self.db_path = str(db_path)
            self.store = None
        else:
            self.db_path = "memory/agent_core.db"
            self.store = None

        # Resolve Config & Settings
        self.settings = None
        self.config = {}
        if hasattr(config_or_settings, "MIN_COOLDOWN_SECONDS"):
            self.settings = config_or_settings
        elif isinstance(config_or_settings, dict):
            self.config = config_or_settings
        elif config:
            self.config = config

        wc = self.config.get("wind_control", {})
        limits = wc.get("limits", {})
        cadence = wc.get("human_cadence", {})

        self.min_interval = float(
            getattr(self.settings, "MIN_COOLDOWN_SECONDS", wc.get("min_call_interval_seconds", 12.0))
        )
        self.window_5m_max = int(
            getattr(self.settings, "SLIDING_WINDOW_5M_MAX", limits.get("window_5m_max_calls", 6))
        )
        self.window_1h_max = int(
            getattr(self.settings, "SLIDING_WINDOW_1H_MAX", limits.get("window_1h_max_calls", 30))
        )
        self.jitter_min = float(
            getattr(self.settings, "JITTER_MIN_SECONDS", cadence.get("min_jitter_seconds", 1.5))
        )
        self.jitter_max = float(
            getattr(self.settings, "JITTER_MAX_SECONDS", cadence.get("max_jitter_seconds", 4.8))
        )
        self.quiet_hours_enabled = wc.get("quiet_hours", {}).get("enabled", True)
        self.backoff_schedule = getattr(
            self.settings, "COOLDOWN_429_SCHEDULE", [120, 600, 1800]
        )
        self._backoff_index: Dict[str, int] = {}

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def is_quiet_hours(self) -> bool:
        if not self.quiet_hours_enabled:
            return False
        now = datetime.datetime.now().time()
        start = datetime.time(1, 0)
        end = datetime.time(7, 0)
        return start <= now <= end

    def check_engine_availability(self, engine_id: str, is_p0_urgent: bool = False) -> Tuple[bool, str]:
        """Synchronous per-engine availability check."""
        if self.is_quiet_hours() and not is_p0_urgent:
            return False, "Quiet hours active (01:00-07:00). Routine cognitive calls paused."

        now = time.time()
        with self._get_conn() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT state, cooldown_until, last_call_timestamp FROM engine_health_states WHERE engine_id = ?;",
                (engine_id,)
            )
            row = c.fetchone()
            if not row:
                return True, "OK"

            state = row["state"]
            cooldown_until = row["cooldown_until"]
            last_call = row["last_call_timestamp"]

            if state == CircuitState.LOCKDOWN_403.value:
                if now < cooldown_until:
                    return False, f"Engine {engine_id} is in RED KILL-SWITCH ACTIVE until {cooldown_until}."
                c.execute(
                    "UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0 WHERE engine_id = ?;",
                    (engine_id,)
                )
                conn.commit()

            if state == CircuitState.COOLDOWN_429.value:
                if now < cooldown_until:
                    return False, f"Engine {engine_id} is in YELLOW 429 COOLDOWN until {cooldown_until}."
                c.execute(
                    "UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0 WHERE engine_id = ?;",
                    (engine_id,)
                )
                conn.commit()

            if now - last_call < self.min_interval:
                return False, f"Call interval too short (< {self.min_interval}s). Throttled."

            five_min_ago = now - 300
            one_hour_ago = now - 3600
            c.execute(
                "SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;",
                (engine_id, five_min_ago)
            )
            count_5m = c.fetchone()[0]
            if count_5m >= self.window_5m_max:
                return False, f"Engine {engine_id} reached 5-min rate limit ({count_5m}/{self.window_5m_max})."

            c.execute(
                "SELECT COUNT(*) FROM wind_control_logs WHERE engine_id = ? AND event_type = 'CALL' AND created_at > ?;",
                (engine_id, one_hour_ago)
            )
            count_1h = c.fetchone()[0]
            if count_1h >= self.window_1h_max:
                return False, f"Engine {engine_id} reached 1-hour rate limit ({count_1h}/{self.window_1h_max})."

        return True, "OK"

    async def check_can_invoke(self, is_p0: bool = False, engine_id: str = "agy") -> Tuple[bool, str, float]:
        """Asynchronous check compatible with test_code_first_system."""
        ok, reason = self.check_engine_availability(engine_id, is_p0_urgent=is_p0)
        wait_time = 0.0
        if not ok and "until" in reason:
            try:
                parts = reason.split("until")
                until_ts = float(parts[-1].strip().rstrip("."))
                wait_time = max(0.0, until_ts - time.time())
            except Exception:
                wait_time = 120.0
        return ok, reason, wait_time

    def record_call(self, engine_id: str = "agy"):
        """Record successful call in DB."""
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE engine_health_states SET last_call_timestamp = ?, total_calls_today = total_calls_today + 1 WHERE engine_id = ?;",
                (now, engine_id)
            )
            conn.execute(
                "INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, 'CALL', 'NORMAL_CALL', ?);",
                (engine_id, now)
            )
            conn.commit()

    def apply_human_jitter(self) -> float:
        """Apply human typing cadence jitter delay synchronously."""
        jitter = random.uniform(self.jitter_min, self.jitter_max)
        time.sleep(jitter)
        return jitter

    async def apply_cadence_jitter(self) -> float:
        """Apply human typing cadence jitter delay asynchronously."""
        jitter = random.uniform(self.jitter_min, self.jitter_max)
        await asyncio.sleep(jitter)
        return jitter

    def trigger_429_backoff(self, engine_id: str = "agy", custom_seconds: Optional[int] = None):
        """Trigger yellow 429 exponential cooldown."""
        idx = self._backoff_index.get(engine_id, 0)
        cooldown_duration = custom_seconds or self.backoff_schedule[min(idx, len(self.backoff_schedule) - 1)]
        self._backoff_index[engine_id] = min(idx + 1, len(self.backoff_schedule) - 1)
        now = time.time()
        until = now + cooldown_duration
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE engine_health_states SET state = 'COOLDOWN_429', cooldown_until = ?, failover_count = failover_count + 1 WHERE engine_id = ?;",
                (until, engine_id)
            )
            conn.execute(
                "INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, '429_BACKOFF', ?, ?);",
                (engine_id, f"YELLOW 429 COOLDOWN until {until}", now)
            )
            conn.commit()

    async def trigger_429_circuit_breaker(self, reason: str, engine_id: str = "agy"):
        """Asynchronous trigger for 429 yellow circuit breaker."""
        self.trigger_429_backoff(engine_id)

    def trigger_403_lockdown(self, engine_id: str = "agy"):
        """Trigger red 403 24-hour lockdown."""
        lockdown_duration = 86400.0
        now = time.time()
        until = now + lockdown_duration
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE engine_health_states SET state = 'LOCKDOWN_403', cooldown_until = ?, failover_count = failover_count + 1 WHERE engine_id = ?;",
                (until, engine_id)
            )
            conn.execute(
                "INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, '403_LOCKDOWN', ?, ?);",
                (engine_id, f"RED KILL-SWITCH ACTIVE until {until}", now)
            )
            conn.commit()

    async def trigger_403_kill_switch(self, reason: str, engine_id: str = "agy"):
        """Asynchronous trigger for 403 red kill-switch."""
        self.trigger_403_lockdown(engine_id)

    async def manual_admin_unlock(self, user_id: str, engine_id: str = "agy") -> Tuple[bool, str]:
        """Manual admin unlock to reset circuit breaker."""
        admin_users = getattr(self.settings, "ADMIN_USERS", ["admin_user", "*"])
        if "*" not in admin_users and user_id not in admin_users:
            return False, f"Permission denied: user '{user_id}' is not an authorized administrator."

        with self._get_conn() as conn:
            conn.execute(
                "UPDATE engine_health_states SET state = 'CLOSED', cooldown_until = 0 WHERE engine_id = ?;",
                (engine_id,)
            )
            conn.execute(
                "INSERT INTO wind_control_logs (engine_id, event_type, details, created_at) VALUES (?, 'ADMIN_UNLOCK', ?, ?);",
                (engine_id, f"Unlocked by admin {user_id}", time.time())
            )
            conn.commit()

        self._backoff_index[engine_id] = 0
        return True, f"Admin {user_id} successfully unlocked engine '{engine_id}'."
