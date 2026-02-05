from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .services import calls as call_service


def get_departments(
    session: Session, tz: str, date_from: Optional[str] = None, date_to: Optional[str] = None
) -> Dict[str, Any]:
    return call_service.list_departments(session, tz, date_from, date_to)


def get_calls(
    session: Session,
    tz: str,
    filters: Optional[Dict[str, Any]] = None,
    sort: Optional[Dict[str, str]] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    return call_service.list_calls(session, tz, filters=filters, sort=sort, limit=limit)


def get_metrics(
    session: Session,
    tz: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    group_by: str = "department",
) -> Dict[str, Any]:
    return call_service.list_metrics(session, tz, date_from=date_from, date_to=date_to, group_by=group_by)


def get_call_summary(session: Session, call_id: int) -> Dict[str, Any]:
    rows = call_service.list_calls(
        session,
        tz="UTC",
        filters={"id": call_id},
        sort={"field": "call_date", "direction": "desc"},
        limit=1,
    )
    return {"call": rows["calls"][0] if rows["calls"] else None}
