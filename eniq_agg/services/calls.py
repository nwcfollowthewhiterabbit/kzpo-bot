from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Call
from ..utils.time_utils import parse_date_range


def normalize_direction(direction: str) -> str:
    d = (direction or "").strip().lower()
    if d in {"out", "outgoing", "outbound", "исход", "исходящие"}:
        return "out"
    if d in {"in", "incoming", "inbound", "вход", "входящие"}:
        return "in"
    return d


def call_time_expr():
    return func.coalesce(Call.rs_start_time, Call.eniq_call_date)


def duration_minutes_expr():
    return func.coalesce(Call.eniq_duration_minutes, Call.rs_duration / 60.0)


def duration_minutes_value(call: Call) -> Optional[float]:
    if call.eniq_duration_minutes is not None:
        return float(call.eniq_duration_minutes)
    if call.rs_duration is not None:
        return float(call.rs_duration) / 60.0
    return None


def direction_expr():
    return func.coalesce(Call.rs_direction, Call.rs_call_type)


def rep_expr():
    return func.nullif(func.coalesce(Call.rs_manager_name, Call.eniq_representative), "")


def department_expr():
    return func.nullif(func.coalesce(Call.eniq_department, Call.rs_call_scheme), "")


def get_score_from_result(result_json: Optional[dict]) -> Optional[float]:
    res = result_json or {}
    for key in ("Final Score", "Загальна оцінка"):
        if key in res:
            try:
                return float(res[key])
            except Exception:
                continue
    return None


def apply_call_filters(
    stmt,
    start: Optional[datetime],
    end: Optional[datetime],
    direction: Optional[str] = None,
    representative: Optional[str] = None,
    department: Optional[str] = None,
) -> Any:
    time_col = call_time_expr()
    if start:
        stmt = stmt.where(time_col >= start)
    if end:
        stmt = stmt.where(time_col < end)
    if direction:
        stmt = stmt.where(direction_expr() == normalize_direction(direction))
    if representative:
        stmt = stmt.where(rep_expr() == representative)
    if department:
        stmt = stmt.where(department_expr() == department)
    return stmt


def count_calls(
    session: Session,
    start: Optional[datetime],
    end: Optional[datetime],
    direction: Optional[str] = None,
    representative: Optional[str] = None,
    department: Optional[str] = None,
) -> int:
    stmt = select(func.count()).select_from(Call).where(call_time_expr().is_not(None))
    stmt = apply_call_filters(stmt, start, end, direction, representative, department)
    return session.scalar(stmt) or 0


def top_managers(
    session: Session,
    start: Optional[datetime],
    end: Optional[datetime],
    order: str = "desc",
) -> List[Tuple[str, int]]:
    name_col = rep_expr()
    time_col = call_time_expr()
    stmt = (
        select(name_col.label("rep"), func.count().label("cnt"))
        .where(
            time_col.is_not(None),
            name_col.is_not(None),
        )
        .group_by(name_col)
    )
    stmt = apply_call_filters(stmt, start, end)
    if order == "asc":
        stmt = stmt.order_by(func.count().asc())
    else:
        stmt = stmt.order_by(func.count().desc())
    return session.execute(stmt).all()


def list_departments(
    session: Session, tz: str, date_from: Optional[str], date_to: Optional[str]
) -> Dict[str, Any]:
    start, end = parse_date_range(date_from, date_to, tz)
    dept_col = department_expr()
    stmt = (
        select(dept_col, func.count().label("cnt"))
        .where(call_time_expr().is_not(None), dept_col.is_not(None))
        .group_by(dept_col)
        .order_by(func.count().desc())
    )
    stmt = apply_call_filters(stmt, start, end)
    rows = session.execute(stmt).all()
    departments = [{"name": r[0], "calls": r[1]} for r in rows if r[0]]
    return {"departments": departments, "date_from": date_from, "date_to": date_to}


def list_calls(
    session: Session,
    tz: str,
    filters: Optional[Dict[str, Any]] = None,
    sort: Optional[Dict[str, str]] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    filters = filters or {}
    sort = sort or {}
    start, end = parse_date_range(filters.get("date_from"), filters.get("date_to"), tz)
    stmt = select(Call).where(call_time_expr().is_not(None))
    stmt = apply_call_filters(
        stmt,
        start,
        end,
        direction=filters.get("direction"),
        representative=filters.get("representative"),
        department=filters.get("department"),
    )
    if filters.get("call_scheme"):
        stmt = stmt.where(Call.rs_call_scheme == filters["call_scheme"])
    if filters.get("id"):
        stmt = stmt.where(Call.id == int(filters["id"]))
    if filters.get("call_id"):
        stmt = stmt.where(Call.call_id == str(filters["call_id"]))

    sort_field = sort.get("field") or sort.get("by") or "call_date"
    sort_dir = (sort.get("direction") or sort.get("order") or "desc").lower()
    if sort_field == "duration":
        order_col = duration_minutes_expr()
    else:
        order_col = call_time_expr()
    stmt = stmt.order_by(order_col.asc() if sort_dir == "asc" else order_col.desc())
    stmt = stmt.limit(max(1, min(limit, 50)))

    rows = session.scalars(stmt).all()
    calls: List[Dict[str, Any]] = []
    for r in rows:
        call_time = r.rs_start_time or r.eniq_call_date
        duration_minutes = duration_minutes_value(r)
        calls.append(
            {
                "id": r.id,
                "call_id": r.call_id,
                "analysis_id": r.eniq_analysis_id,
                "rep": r.eniq_representative or r.rs_manager_name,
                "department": r.eniq_department or r.rs_call_scheme,
                "client": (r.eniq_metadata_json or {}).get("Customer") or r.rs_client_number,
                "date": call_time,
                "duration": duration_minutes,
                "score": get_score_from_result(r.eniq_result_json),
                "url": r.eniq_external_url or r.rs_record_url,
                "summary": r.eniq_summary or r.eniq_title,
            }
        )
    return {"calls": calls, "filters": filters, "sort": sort}


def list_metrics(
    session: Session,
    tz: str,
    date_from: Optional[str],
    date_to: Optional[str],
    group_by: str = "department",
) -> Dict[str, Any]:
    start, end = parse_date_range(date_from, date_to, tz)
    if group_by == "date":
        group_column = func.date(call_time_expr())
    elif group_by == "representative":
        group_column = rep_expr()
    elif group_by == "direction":
        group_column = direction_expr()
    elif group_by == "call_scheme":
        group_column = Call.rs_call_scheme
    else:
        group_column = department_expr()
    stmt = (
        select(
            group_column.label("group"),
            func.count().label("calls"),
            func.avg(duration_minutes_expr()).label("avg_duration"),
        )
        .where(call_time_expr().is_not(None))
        .group_by(group_column)
    )
    stmt = apply_call_filters(stmt, start, end)
    if group_by == "date":
        stmt = stmt.order_by(group_column.asc())
    rows = session.execute(stmt).all()
    data = []
    for grp, calls, avg_dur in rows:
        if not grp:
            continue
        data.append(
            {
                "group": grp,
                "calls": calls,
                "avg_duration": float(avg_dur) if avg_dur is not None else None,
            }
        )
    return {
        "group_by": group_by,
        "rows": sorted(data, key=lambda x: x["calls"], reverse=True),
        "date_from": date_from,
        "date_to": date_to,
    }
