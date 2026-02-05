from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz


def day_range(tz: str, offset_days: int = 0) -> Tuple[datetime, datetime]:
    """Return naive (tz-stripped) start/end for a day in the given timezone."""
    zone = pytz.timezone(tz)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=offset_days)
    end = start + timedelta(days=1)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def parse_date_range(
    date_from: Optional[str], date_to: Optional[str], tz: str
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse simple date strings: 'today', 'yesterday', or ISO date."""
    zone = pytz.timezone(tz)
    now = datetime.now(zone)

    def _parse(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        val = value.lower()
        if val == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if val == "yesterday":
            return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    start = _parse(date_from)
    end = _parse(date_to)
    if start and not end:
        end = start + timedelta(days=1)
    if start:
        start = start.replace(tzinfo=None)
    if end:
        end = end.replace(tzinfo=None)
    return start, end
