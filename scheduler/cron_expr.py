"""Pure-Python Standard 5-Field POSIX Cron Expression Parser & Due Evaluator.

Supports `*`, `*/step`, `a-b`, `a-b/step`, and comma lists `a,b,c` across:
    minute (0-59) | hour (0-23) | day_of_month (1-31) | month (1-12) | day_of_week (0-7, 0 & 7 = Sun)
Zero external third-party dependencies required.
"""
import datetime
from typing import Set


def _parse_field(expr: str, min_val: int, max_val: int, is_dow: bool = False) -> Set[int]:
    values: Set[int] = set()
    for part in expr.strip().split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = max(1, int(step_s))
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(base)
            if is_dow and val == 7:
                val = 0
            if not (min_val <= val <= max_val):
                raise ValueError(f"Cron value {val} out of bounds [{min_val}, {max_val}]")
            values.add(val)
            continue

        for v in range(start, end + 1, step):
            norm_v = 0 if (is_dow and v == 7) else v
            if min_val <= norm_v <= max_val:
                values.add(norm_v)

    if not values:
        raise ValueError(f"Invalid cron field: '{expr}'")
    return values


def cron_matches_datetime(cron_expr: str, dt: datetime.datetime) -> bool:
    """Return True if `dt` (local minute resolution) satisfies 5-field `cron_expr`."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: '{cron_expr}'")

    minutes = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    days = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12)
    dows = _parse_field(fields[4], 0, 7, is_dow=True)

    cron_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in days
        and dt.month in months
        and cron_dow in dows
    )


def cron_is_due(cron_expr: str, now_ts: float, last_run_ts: float = 0.0) -> bool:
    """Return True iff `now_ts` matches `cron_expr` AND the job has not already run in this minute."""
    now_dt = datetime.datetime.fromtimestamp(now_ts)
    if last_run_ts > 0:
        last_dt = datetime.datetime.fromtimestamp(last_run_ts)
        if (
            now_dt.year == last_dt.year
            and now_dt.month == last_dt.month
            and now_dt.day == last_dt.day
            and now_dt.hour == last_dt.hour
            and now_dt.minute == last_dt.minute
        ):
            return False
    return cron_matches_datetime(cron_expr, now_dt)
