from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from dateutil import parser
from sqlalchemy import select

from .client import EniqClient
from .config import settings
from .db import Base, engine, get_session
from .models import Analysis, Contact, Call


logger = logging.getLogger(__name__)


def ensure_schema() -> None:
    Base.metadata.create_all(engine)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parser.isoparse(value)
    except Exception:
        return None


def upsert_analysis(row: Dict[str, Any], detail: Dict[str, Any] | None) -> Analysis:
    a = Analysis(
        id=row["id"],
        uuid=row.get("uuid"),
        task_id=str(row.get("task_id")) if row.get("task_id") is not None else None,
        task_uuid=row.get("task_uuid"),
        project_id=row.get("project_id"),
        status=row.get("status"),
        content_type=row.get("content_type"),
        model=row.get("model"),
        processing_time_ms=row.get("processing_time_ms"),
        department=row.get("department"),
        representative=row.get("representative"),
        prompt_id=(row.get("prompt") or {}).get("id"),
        prompt_name=(row.get("prompt") or {}).get("scriptName"),
        call_date=parse_dt(row.get("call_date")),
        communication_date=parse_dt(row.get("communication_date")),
        created_at=parse_dt(row.get("created_at")),
        updated_at=parse_dt(row.get("updated_at")),
        duration_minutes=row.get("duration"),
        external_url=row.get("external_url"),
        metadata_json=row.get("metadata"),
        result_json=row.get("result"),
        summary=(row.get("result") or {}).get("Summary"),
        title=(row.get("result") or {}).get("Title"),
    )
    if detail:
        a.audio_file_json = detail.get("audio_file")
        a.transcription_text = (detail.get("transcription") or {}).get("text")
        a.transcription_json = detail.get("transcription")
        a.tokens_json = {
            "tokens_used": detail.get("tokens_used"),
            "tokens_used_json": detail.get("tokens_used_json"),
        }
    return a


def _update_contact_from_metadata(meta: dict) -> None:
    if not meta:
        return
    phone = meta.get("Customer") or meta.get("client_phone") or meta.get("client")
    name = meta.get("Customer_Name") or meta.get("Client_Name") or meta.get("Name")
    company = meta.get("Company") or meta.get("Organization")
    if not phone:
        return
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if not digits:
        return
    with get_session() as session:
        contact = session.scalar(select(Contact).where(Contact.phone == digits))
        if not contact:
            contact = Contact(phone=digits, name=name, company=company)
            session.add(contact)
        else:
            # обновляем только если пришли новые данные
            if name and name != contact.name:
                contact.name = name
            if company and company != contact.company:
                contact.company = company


def _update_call_from_analysis(session, analysis: Analysis) -> None:
    meta = analysis.metadata_json or {}
    call_id = meta.get("uniqueid")
    if not call_id:
        return
    now = datetime.utcnow()
    call = session.scalar(select(Call).where(Call.call_id == str(call_id)))
    if not call:
        call = Call(call_id=str(call_id), created_at=now, updated_at=now)
    call.eniq_analysis_id = analysis.id
    call.eniq_call_date = analysis.call_date
    call.eniq_communication_date = analysis.communication_date
    call.eniq_created_at = analysis.created_at
    call.eniq_updated_at = analysis.updated_at
    call.eniq_duration_minutes = analysis.duration_minutes
    call.eniq_department = analysis.department
    call.eniq_representative = analysis.representative
    call.eniq_prompt_id = analysis.prompt_id
    call.eniq_prompt_name = analysis.prompt_name
    call.eniq_status = analysis.status
    call.eniq_content_type = analysis.content_type
    call.eniq_model = analysis.model
    call.eniq_processing_time_ms = analysis.processing_time_ms
    call.eniq_external_url = analysis.external_url
    call.eniq_summary = analysis.summary
    call.eniq_title = analysis.title
    call.eniq_metadata_json = analysis.metadata_json
    call.eniq_result_json = analysis.result_json
    call.eniq_audio_file_json = analysis.audio_file_json
    call.eniq_transcription_text = analysis.transcription_text
    call.eniq_transcription_json = analysis.transcription_json
    call.eniq_tokens_json = analysis.tokens_json
    call.updated_at = now
    session.add(call)


def ingest_all() -> list[int]:
    ensure_schema()
    client = EniqClient()
    created, updated = 0, 0
    created_ids: list[int] = []
    page = 1
    while True:
        resp = client.fetch_results(
            project_id=settings.project_id,
            page=page,
            limit=settings.page_size,
            start_date=settings.start_date,
            end_date=settings.end_date,
            status=settings.status,
            with_transcription=settings.with_transcription,
        )
        rows: List[Dict[str, Any]] = (
            resp.get("data") or resp.get("results") or []
        )
        meta = resp.get("meta") or resp.get("pagination") or {}
        total_pages = (
            meta.get("total_pages")
            or meta.get("pages")
            or meta.get("totalPages")
            or 1
        )
        if not rows:
            break
        ids_in_page = [r.get("id") for r in rows if r.get("id") is not None]
        with get_session() as session:
            existing_ids = set()
            if ids_in_page:
                existing_ids = {
                    r[0]
                    for r in session.execute(
                        select(Analysis.id).where(Analysis.id.in_(ids_in_page))
                    ).all()
            }
            for row in rows:
                row_id = row.get("id")
                if row_id is None:
                    logger.warning("Skip row without id: %s", row)
                    continue
                try:
                    detail = client.fetch_analysis(row_id)
                except Exception as exc:  # pragma: no cover - network errors
                    logger.warning("Failed to fetch analysis %s: %s", row_id, exc)
                    detail = row
                    if not (detail or {}).get("transcription"):
                        logger.warning(
                            "No transcription in fallback row for id=%s; partial data stored",
                            row_id,
                        )
                entity = upsert_analysis(row, detail)
                # обновляем справочник контактов
                _update_contact_from_metadata(entity.metadata_json or {})
                _update_call_from_analysis(session, entity)
                if entity.id in existing_ids:
                    session.merge(entity)
                    updated += 1
                else:
                    session.add(entity)
                    created += 1
                    created_ids.append(entity.id)
        logger.info("page %s/%s processed, total %s", page, total_pages, len(rows))
        page += 1
        if page > total_pages:
            break
    client.close()
    logger.info("Ingest completed: created=%s, updated=%s", created, updated)
    return created_ids
