from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import Call, Analysis
from .ringostat_client import RingostatClient

logger = logging.getLogger(__name__)
_failure_count = 0
_next_retry_at: Optional[datetime.datetime] = None
_last_success_at: Optional[datetime.datetime] = None
_KNOWN_AFTER_FIELDS = {
    "call_id",
    "id",
    "project_id",
    "direction",
    "call_type",
    "status",
    "call_status",
    "start_time",
    "start",
    "call_date",
    "call_time",
    "date",
    "end_time",
    "end",
    "duration",
    "talk_time",
    "talk",
    "ringing_time",
    "client_number",
    "phone",
    "caller_number",
    "caller_id",
    "contact_name",
    "contact_company",
    "employee_number",
    "sip",
    "operator_number",
    "responsible_name",
    "sip_name",
    "manager",
    "record_url",
    "recording",
    "call_cost",
    "call_scheme",
    "user_agent",
    "uniq",
    "uniq_targeted",
    "call_id2",
}
_KNOWN_ANSWER_FIELDS = {
    "call_id",
    "id",
    "market_comp",
    "target_post",
    "sales_channel",
    "sales_source",
    "sales_cource",
    "call_type",
    "last_page",
    "landing",
    "referrer",
    "user_agent",
    "key_word",
    "keyword",
    "ключевое_слово",
    "callback",
    "client_mob",
    "client_number",
    "caller_id",
    "caller_number",
}


def parse_dt(value: str | None) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def normalize_phone(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    digits = "".join(ch for ch in str(val) if ch.isdigit())
    return digits or None


def _extract_call_time(row: Dict[str, Any]) -> Optional[datetime.datetime]:
    for key in ("start_time", "start", "call_date", "call_time", "date"):
        if row.get(key):
            return parse_dt(row.get(key))
    return None


def _extract_analysis_phone(meta: Optional[dict]) -> Optional[str]:
    if not meta:
        return None
    return normalize_phone(meta.get("Customer") or meta.get("client_phone") or meta.get("client"))


def _match_analysis(row: Dict[str, Any], received_at: Optional[datetime.datetime]) -> Optional[Dict[str, Any]]:
    call_id = str(row.get("call_id") or row.get("id") or "").strip()
    if call_id:
        with get_session() as session:
            by_unique = session.scalar(
                select(Analysis).where(Analysis.metadata_json["uniqueid"].as_string() == call_id)
            )
        if by_unique:
            return {
                "analysis_id": by_unique.id,
                "time_diff_sec": None,
                "duration_diff_sec": None,
                "matched_by": "uniqueid",
            }
    client_number = normalize_phone(
        row.get("client_number") or row.get("phone") or row.get("caller_number")
    )
    event_time = _extract_call_time(row) or received_at
    if not client_number or not event_time:
        return None
    # search in a +-20 minute window
    start = event_time - datetime.timedelta(minutes=20)
    end = event_time + datetime.timedelta(minutes=20)
    duration_sec = row.get("duration") or row.get("talk_time") or row.get("talk")
    try:
        duration_sec = int(duration_sec) if duration_sec is not None else None
    except Exception:
        duration_sec = None

    best = None
    best_score = None
    with get_session() as session:
        candidates = session.scalars(
            select(Analysis).where(
                Analysis.call_date.is_not(None),
                Analysis.call_date >= start,
                Analysis.call_date <= end,
            )
        ).all()
    for cand in candidates:
        cand_phone = _extract_analysis_phone(cand.metadata_json or {})
        if cand_phone != client_number:
            continue
        time_diff = abs((cand.call_date - event_time).total_seconds()) if cand.call_date else None
        dur_diff = None
        if duration_sec is not None and cand.duration_minutes is not None:
            dur_diff = abs(duration_sec - int(cand.duration_minutes * 60))
        score = (time_diff or 0) + (dur_diff or 0) * 0.2
        if best_score is None or score < best_score:
            best_score = score
            best = {
                "analysis_id": cand.id,
                "time_diff_sec": time_diff,
                "duration_diff_sec": dur_diff,
                "matched_by": "phone+time",
            }
    return best


def _get_call_id(row: Dict[str, Any]) -> str:
    return str(row.get("call_id") or row.get("id") or "").strip()


def _bool_value(val: Any) -> Optional[bool]:
    if val is None:
        return None
    return str(val).lower() in {"1", "true", "yes", "on"}


def upsert_call(row: Dict[str, Any], received_at: Optional[datetime.datetime]) -> Optional[Call]:
    call_id = _get_call_id(row)
    if not call_id:
        return None
    match_info = _match_analysis(row, received_at)
    if match_info:
        logger.info(
            "Ringostat match analysis_id=%s time_diff_sec=%s duration_diff_sec=%s",
            match_info.get("analysis_id"),
            match_info.get("time_diff_sec"),
            match_info.get("duration_diff_sec"),
        )
    elif received_at and _extract_call_time(row):
        skew = abs((received_at - (_extract_call_time(row) or received_at)).total_seconds())
        if skew > 3600:
            logger.warning("Ringostat time skew seconds=%s", int(skew))
    now = datetime.datetime.utcnow()
    with get_session() as session:
        entity = session.scalar(select(Call).where(Call.call_id == call_id))
        if not entity:
            entity = Call(call_id=call_id, created_at=now, updated_at=now)
        entity.rs_project_id = str(row.get("project_id") or settings.ringostat_project_id or "")
        entity.rs_direction = row.get("direction") or row.get("call_type")
        entity.rs_status = row.get("status") or row.get("call_status")
        entity.rs_start_time = _extract_call_time(row)
        entity.rs_end_time = parse_dt(row.get("end_time") or row.get("end"))
        entity.rs_duration = row.get("duration")
        entity.rs_talk_time = row.get("talk_time") or row.get("talk")
        entity.rs_ringing_time = row.get("ringing_time")
        entity.rs_client_number = normalize_phone(
            row.get("client_number")
            or row.get("phone")
            or row.get("caller_number")
            or row.get("caller_id")
        )
        entity.rs_contact_name = row.get("contact_name")
        entity.rs_contact_company = row.get("contact_company")
        entity.rs_employee_number = normalize_phone(
            row.get("employee_number") or row.get("sip") or row.get("operator_number")
        )
        entity.rs_manager_name = row.get("responsible_name") or row.get("sip_name") or row.get("manager")
        entity.rs_record_url = row.get("record_url") or row.get("recording")
        entity.rs_cost = row.get("call_cost")
        entity.rs_call_scheme = row.get("call_scheme")
        entity.rs_user_agent = row.get("user_agent")
        entity.rs_is_unique = _bool_value(row.get("uniq"))
        entity.rs_is_unique_targeted = _bool_value(row.get("uniq_targeted"))
        if match_info and not entity.eniq_analysis_id:
            entity.eniq_analysis_id = match_info.get("analysis_id")
        entity.rs_match_method = match_info.get("matched_by") if match_info else None
        entity.rs_match_time_diff_sec = match_info.get("time_diff_sec") if match_info else None
        entity.rs_match_duration_diff_sec = match_info.get("duration_diff_sec") if match_info else None
        entity.rs_after_received_at = received_at
        entity.rs_after_raw_json = {
            "payload": row,
            "match": match_info,
            "received_at": received_at.isoformat() if received_at else None,
            "extras": {k: v for k, v in row.items() if k not in _KNOWN_AFTER_FIELDS},
        }
        entity.updated_at = now
        session.add(entity)
    return entity


def upsert_calls(
    rows: List[Dict[str, Any]], received_at: Optional[datetime.datetime] = None
) -> tuple[List[int], int, int]:
    created_ids: List[int] = []
    created = 0
    updated = 0
    if not rows:
        logger.info("Ringostat upsert_calls: empty payload")
        return created_ids, created, updated
    ids_in_rows = [cid for cid in (_get_call_id(r) for r in rows) if cid]
    missing_id_rows = [r for r in rows if not _get_call_id(r)]
    if missing_id_rows:
        sample_keys = list(missing_id_rows[0].keys())
        logger.warning(
            "Ringostat upsert_calls: missing call_id/id count=%s sample_keys=%s",
            len(missing_id_rows),
            sample_keys[:40],
        )
    with get_session() as session:
        existing_map: Dict[str, int] = {}
        if ids_in_rows:
            existing_map = {
                call_id: rid
                for call_id, rid in session.execute(
                    select(Call.call_id, Call.id).where(Call.call_id.in_(ids_in_rows))
                ).all()
            }
        logger.info(
            "Ringostat upsert_calls: rows=%s with_ids=%s existing_ids=%s",
            len(rows),
            len(ids_in_rows),
            len(existing_map),
        )
        for r in rows:
            cid = _get_call_id(r)
            if not cid:
                continue
            entity = upsert_call(r, received_at=received_at)
            if not entity:
                continue
            if cid in existing_map:
                updated += 1
            else:
                created += 1
    return created_ids, created, updated


def upsert_answer_call(payload: Dict[str, Any], received_at: Optional[datetime.datetime]) -> bool:
    call_id = _get_call_id(payload)
    if not call_id:
        logger.warning("Ringostat answer_call missing call_id keys=%s", list(payload.keys())[:30])
        return False
    now = datetime.datetime.utcnow()
    sales_source = payload.get("sales_source") or payload.get("sales_cource")
    keyword = payload.get("key_word") or payload.get("keyword") or payload.get("ключевое_слово")
    client_phone = normalize_phone(
        payload.get("client_mob")
        or payload.get("client_number")
        or payload.get("caller_id")
        or payload.get("caller_number")
    )
    with get_session() as session:
        entity = session.scalar(select(Call).where(Call.call_id == call_id))
        if not entity:
            entity = Call(call_id=call_id, created_at=now, updated_at=now)
        if client_phone:
            # сохраняем номер, полученный на старте звонка, если он ещё не заполнен
            entity.rs_client_number = entity.rs_client_number or client_phone
        entity.rs_answer_received_at = received_at
        entity.rs_market_comp = payload.get("market_comp")
        entity.rs_target_post = payload.get("target_post")
        entity.rs_sales_channel = payload.get("sales_channel")
        entity.rs_sales_source = sales_source
        entity.rs_call_type = payload.get("call_type")
        entity.rs_last_page = payload.get("last_page")
        entity.rs_landing = payload.get("landing")
        entity.rs_referrer = payload.get("referrer")
        entity.rs_answer_user_agent = payload.get("user_agent")
        entity.rs_keyword = keyword
        entity.rs_callback = payload.get("callback")
        entity.rs_answer_raw_json = {
            "payload": payload,
            "normalized_client_mob": client_phone,
            "received_at": received_at.isoformat() if received_at else None,
            "extras": {k: v for k, v in payload.items() if k not in _KNOWN_ANSWER_FIELDS},
        }
        entity.updated_at = now
        session.add(entity)
    return True


def fetch_incremental() -> List[int]:
    """Fetch calls for last 10 minutes and upsert; return new call IDs (db pk)."""
    if not settings.ringostat_token or not settings.ringostat_project_id:
        return []
    global _failure_count, _next_retry_at, _last_success_at
    now_utc = datetime.datetime.utcnow()
    if _next_retry_at and now_utc < _next_retry_at:
        logger.info("Ringostat fetch skipped (backoff until %s)", _next_retry_at)
        return []
    client = RingostatClient()
    created_ids: List[int] = []
    now = datetime.datetime.utcnow()
    start = now - datetime.timedelta(minutes=10)
    page = 1
    per_page = 100
    try:
        while True:
            data = client.fetch_calls(start, now, page=page, per_page=per_page)
            rows = data.get("results") or data.get("data") or data.get("items") or []
            meta = data.get("meta") or data.get("pagination") or {}
            total_pages = meta.get("pages") or meta.get("total_pages") or 1
            if not rows:
                break
            new_ids, _, _ = upsert_calls(rows)
            created_ids.extend(new_ids)
            logger.info("ringostat page %s/%s processed (%s rows)", page, total_pages, len(rows))
            page += 1
            if page > total_pages:
                break
        _failure_count = 0
        _next_retry_at = None
        _last_success_at = now_utc
    except Exception as exc:
        _register_failure(exc)
    finally:
        client.close()
    return created_ids


def ringostat_health() -> Dict[str, Optional[datetime.datetime | int]]:
    """Return basic health info for diagnostics."""
    return {
        "last_success_at": _last_success_at,
        "failure_count": _failure_count,
        "next_retry_at": _next_retry_at,
    }


def _register_failure(exc: Exception) -> None:
    global _failure_count, _next_retry_at
    _failure_count += 1
    backoff_minutes = min(60, 2 * _failure_count)  # simple linear backoff up to 1h
    _next_retry_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=backoff_minutes)
    logger.warning(
        "Ringostat fetch failed (%s). Failures=%s, next retry after %s minutes at %s",
        exc,
        _failure_count,
        backoff_minutes,
        _next_retry_at,
    )
