from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any

import pytz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Analysis, Call
from .services import calls as call_service
from .utils.time_utils import day_range


def normalize_date(dt: datetime, tz: str) -> datetime:
    zone = pytz.timezone(tz)
    if dt.tzinfo is None:
        return zone.localize(dt)
    return dt.astimezone(zone)


def get_score_from_result(result_json: Optional[dict]) -> Optional[float]:
    res = result_json or {}
    for key in ("Final Score", "Загальна оцінка"):
        if key in res:
            try:
                return float(res[key])
            except Exception:
                continue
    return None


def get_score(a: Analysis) -> Optional[float]:
    return get_score_from_result(a.result_json)


def get_call_score(call: Call) -> Optional[float]:
    return get_score_from_result(call.eniq_result_json)


def daily_metrics(
    session: Session, representative: str, tz: str
) -> Dict[str, Optional[float]]:
    start, end = day_range(tz, offset_days=0)
    # counts/durations from calls (Ringostat+ENIQ)
    rep_name = representative
    time_col = call_service.call_time_expr()
    rep_col = call_service.rep_expr()
    base_stmt = (
        select(Call)
        .where(
            time_col.is_not(None),
            time_col >= start,
            time_col < end,
            rep_col == rep_name,
        )
    )
    calls_rows: List[Call] = session.scalars(base_stmt).all()
    calls_today = len(calls_rows)
    durations = [call_service.duration_minutes_value(r) for r in calls_rows]
    avg_duration = (
        sum(d for d in durations if d is not None) / calls_today if calls_today else 0
    )

    # errors list (still using ENIQ analyses for detail buttons)
    errors = session.scalars(
        select(Analysis).where(
            Analysis.representative == representative,
            Analysis.call_date.is_not(None),
            Analysis.call_date >= start,
            Analysis.call_date < end,
        )
    ).all()
    errors = [r for r in errors if (get_score(r) is not None and get_score(r) < 4)]

    # average per day over last 30 days (calls table)
    month_start = start - timedelta(days=30)
    calls_30d = session.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            time_col.is_not(None),
            time_col >= month_start,
            time_col < end,
            rep_col == rep_name,
        )
    ) or 0
    avg_per_day_30d = calls_30d / 30.0

    return {
        "calls_today": calls_today,
        "avg_duration": avg_duration,
        "avg_per_day_30d": avg_per_day_30d,
        "errors": errors,
    }


def list_representatives(session: Session) -> List[str]:
    reps = session.execute(
        select(call_service.rep_expr())
        .select_from(Call)
        .where(call_service.rep_expr().is_not(None))
        .distinct()
        .order_by(call_service.rep_expr())
    ).scalars()
    return [r for r in reps if r]


def weekly_org_metrics(session: Session, tz: str) -> Dict[str, Any]:
    now = datetime.now(pytz.timezone(tz))
    start = now - timedelta(days=7)
    time_col = call_service.call_time_expr()
    rep_col = call_service.rep_expr()

    rows: List[Call] = session.scalars(
        select(Call).where(
            time_col.is_not(None),
            time_col >= start.replace(tzinfo=None),
            time_col <= now.replace(tzinfo=None),
        )
    ).all()
    total = len(rows)
    durations = [call_service.duration_minutes_value(r) for r in rows]
    avg_dur = sum(d for d in durations if d is not None) / total if total else 0
    errors = [
        r for r in session.scalars(
            select(Analysis).where(
                Analysis.call_date.is_not(None),
                Analysis.call_date >= start.replace(tzinfo=None),
                Analysis.call_date <= now.replace(tzinfo=None),
            )
        ).all()
        if get_score(r) is not None and get_score(r) < 4
    ]
    call_filter_irrel = sum(
        1 for r in rows if (r.eniq_result_json or {}).get("Call_Filter") == "Irrelevant"
    )

    rep_counts = session.execute(
        select(rep_col, func.count())
        .select_from(Call)
        .where(
            time_col.is_not(None),
            time_col >= start.replace(tzinfo=None),
            time_col <= now.replace(tzinfo=None),
            rep_col.is_not(None),
        )
        .group_by(rep_col)
        .order_by(func.count().desc())
    ).all()
    top_reps = [(r[0], r[1]) for r in rep_counts if r[0]][:5]

    return {
        "total": total,
        "avg_duration": avg_dur,
        "errors": errors,
        "irrelevant": call_filter_irrel,
        "top_reps": top_reps,
    }


def day_stats_for_rep(
    session: Session, representative: str, tz: str, day_offset: int = 0
) -> Dict[str, Any]:
    """Stats for specific day (offset 0=today, 1=yesterday)."""
    start, end = day_range(tz, offset_days=day_offset)
    time_col = call_service.call_time_expr()
    rows: List[Call] = session.scalars(
        select(Call).where(
            time_col.is_not(None),
            time_col >= start,
            time_col < end,
            rep_col == representative,
        )
    ).all()
    total = len(rows)
    durations = [call_service.duration_minutes_value(r) for r in rows]
    avg_dur = sum(d for d in durations if d is not None) / total if total else 0
    scores = [get_call_score(r) for r in rows if get_call_score(r) is not None]
    avg_score = sum(scores) / len(scores) if scores else None
    return {"total": total, "avg_duration": avg_dur, "avg_score": avg_score}


def admin_snapshot(session: Session, tz: str, window_days: int = 30, recent_limit: int = 5) -> Dict[str, Any]:
    """Aggregated stats for admins across org."""
    zone = pytz.timezone(tz)
    now = datetime.now(zone)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    start_7 = now - timedelta(days=7)
    start_window = now - timedelta(days=window_days)
    time_col = call_service.call_time_expr()
    rep_col = call_service.rep_expr()
    rows: List[Call] = session.scalars(
        select(Call).where(
            time_col.is_not(None),
            time_col >= start_window.replace(tzinfo=None),
            time_col <= now.replace(tzinfo=None),
        )
    ).all()

    def _filter(start: datetime, end: datetime) -> List[Call]:
        return [
            r
            for r in rows
            if (r.rs_start_time or r.eniq_call_date)
            and start <= normalize_date((r.rs_start_time or r.eniq_call_date), tz) < end
        ]

    today_rows = _filter(start_today, start_today + timedelta(days=1))
    yesterday_rows = _filter(start_yesterday, start_today)
    week_rows = _filter(start_7, now)

    def _avg_duration(items: List[Call]) -> float:
        vals = [call_service.duration_minutes_value(r) for r in items]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else 0

    def _error_items(items: List[Call]) -> List[Call]:
        return [r for r in items if (get_call_score(r) is not None and get_call_score(r) < 4)]

    rep_counts = {}
    rep_avg_dur = {}
    for r in week_rows:
        rep = (r.eniq_representative or r.rs_manager_name or "—")
        rep_counts[rep] = rep_counts.get(rep, 0) + 1
        duration = call_service.duration_minutes_value(r)
        if duration is not None:
            rep_avg_dur.setdefault(rep, []).append(duration)
    rep_avg_dur = {k: sum(v) / len(v) for k, v in rep_avg_dur.items()}

    recent_calls = session.scalars(
        select(Call)
        .where(time_col.is_not(None))
        .order_by(time_col.desc())
        .limit(recent_limit)
    ).all()

    recent_errors = [
        r
        for r in session.scalars(
            select(Call)
            .where(time_col.is_not(None))
            .order_by(time_col.desc())
            .limit(100)
        ).all()
        if get_call_score(r) is not None and get_call_score(r) < 4
    ][:recent_limit]

    return {
        "window_days": window_days,
        "today": {
            "total": len(today_rows),
            "avg_duration": _avg_duration(today_rows),
            "errors": _error_items(today_rows),
        },
        "yesterday": {
            "total": len(yesterday_rows),
            "avg_duration": _avg_duration(yesterday_rows),
            "errors": _error_items(yesterday_rows),
        },
        "week": {
            "total": len(week_rows),
            "avg_duration": _avg_duration(week_rows),
            "errors": _error_items(week_rows),
        },
        "window": {
            "total": len(rows),
            "avg_duration": _avg_duration(rows),
            "errors": _error_items(rows),
        },
        "per_rep": {
            rep: {
                "calls_7d": rep_counts.get(rep, 0),
                "avg_duration_7d": rep_avg_dur.get(rep, 0),
            }
            for rep in rep_counts
        },
        "top_reps_7d": sorted(rep_counts.items(), key=lambda x: x[1], reverse=True)[:5],
        "recent_calls": [
            {
                "id": r.id,
                "rep": r.eniq_representative or r.rs_manager_name,
                "client": (r.eniq_metadata_json or {}).get("Customer") or r.rs_client_number,
                "score": get_call_score(r),
                "summary": r.eniq_summary or r.eniq_title,
                "url": r.eniq_external_url or r.rs_record_url,
                "date": r.rs_start_time or r.eniq_call_date,
            }
            for r in recent_calls
        ],
        "recent_errors": [
            {
                "id": r.id,
                "rep": r.eniq_representative or r.rs_manager_name,
                "client": (r.eniq_metadata_json or {}).get("Customer") or r.rs_client_number,
                "score": get_call_score(r),
                "summary": r.eniq_summary or r.eniq_title,
                "url": r.eniq_external_url or r.rs_record_url,
                "date": r.rs_start_time or r.eniq_call_date,
            }
            for r in recent_errors
        ],
    }
