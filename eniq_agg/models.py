from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Float, Integer, String, Text, Index, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_call_id", "call_id", unique=True),
        Index("ix_calls_eniq_analysis_id", "eniq_analysis_id"),
        Index("ix_calls_eniq_call_date", "eniq_call_date"),
        Index("ix_calls_rs_start_time", "rs_start_time"),
        Index("ix_calls_rs_client_number", "rs_client_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    # ENIQ fields
    eniq_analysis_id: Mapped[Optional[int]] = mapped_column(Integer)
    eniq_call_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    eniq_communication_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    eniq_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    eniq_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    eniq_duration_minutes: Mapped[Optional[Float]] = mapped_column(Float)
    eniq_department: Mapped[Optional[str]] = mapped_column(String(128))
    eniq_representative: Mapped[Optional[str]] = mapped_column(String(128))
    eniq_prompt_id: Mapped[Optional[int]] = mapped_column(Integer)
    eniq_prompt_name: Mapped[Optional[str]] = mapped_column(String(256))
    eniq_status: Mapped[Optional[str]] = mapped_column(String(32))
    eniq_content_type: Mapped[Optional[str]] = mapped_column(String(32))
    eniq_model: Mapped[Optional[str]] = mapped_column(String(128))
    eniq_processing_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    eniq_external_url: Mapped[Optional[str]] = mapped_column(Text)
    eniq_summary: Mapped[Optional[str]] = mapped_column(Text)
    eniq_title: Mapped[Optional[str]] = mapped_column(Text)
    eniq_metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    eniq_result_json: Mapped[Optional[dict]] = mapped_column(JSON)
    eniq_audio_file_json: Mapped[Optional[dict]] = mapped_column(JSON)
    eniq_transcription_text: Mapped[Optional[str]] = mapped_column(Text)
    eniq_transcription_json: Mapped[Optional[dict]] = mapped_column(JSON)
    eniq_tokens_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Ringostat after_call fields
    rs_project_id: Mapped[Optional[str]] = mapped_column(String(64))
    rs_direction: Mapped[Optional[str]] = mapped_column(String(16))
    rs_status: Mapped[Optional[str]] = mapped_column(String(32))
    rs_start_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rs_end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rs_duration: Mapped[Optional[int]] = mapped_column(Integer)
    rs_talk_time: Mapped[Optional[int]] = mapped_column(Integer)
    rs_ringing_time: Mapped[Optional[int]] = mapped_column(Integer)
    rs_client_number: Mapped[Optional[str]] = mapped_column(String(64))
    rs_contact_name: Mapped[Optional[str]] = mapped_column(String(256))
    rs_contact_company: Mapped[Optional[str]] = mapped_column(String(256))
    rs_employee_number: Mapped[Optional[str]] = mapped_column(String(64))
    rs_manager_name: Mapped[Optional[str]] = mapped_column(String(256))
    rs_record_url: Mapped[Optional[str]] = mapped_column(Text)
    rs_cost: Mapped[Optional[float]] = mapped_column(Float)
    rs_call_scheme: Mapped[Optional[str]] = mapped_column(String(256))
    rs_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    rs_is_unique: Mapped[Optional[bool]] = mapped_column(Boolean)
    rs_is_unique_targeted: Mapped[Optional[bool]] = mapped_column(Boolean)
    rs_match_method: Mapped[Optional[str]] = mapped_column(String(32))
    rs_match_time_diff_sec: Mapped[Optional[int]] = mapped_column(Integer)
    rs_match_duration_diff_sec: Mapped[Optional[int]] = mapped_column(Integer)
    rs_after_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rs_after_raw_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Ringostat answer_call fields (marketing)
    rs_answer_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    rs_market_comp: Mapped[Optional[str]] = mapped_column(Text)
    rs_target_post: Mapped[Optional[str]] = mapped_column(Text)
    rs_sales_channel: Mapped[Optional[str]] = mapped_column(String(128))
    rs_sales_source: Mapped[Optional[str]] = mapped_column(String(256))
    rs_call_type: Mapped[Optional[str]] = mapped_column(String(16))
    rs_last_page: Mapped[Optional[str]] = mapped_column(Text)
    rs_landing: Mapped[Optional[str]] = mapped_column(Text)
    rs_referrer: Mapped[Optional[str]] = mapped_column(Text)
    rs_answer_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    rs_keyword: Mapped[Optional[str]] = mapped_column(Text)
    rs_callback: Mapped[Optional[str]] = mapped_column(String(128))
    rs_answer_raw_json: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        Index("ix_analyses_call_date", "call_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[Optional[str]] = mapped_column(String(64))
    task_id: Mapped[Optional[str]] = mapped_column(String(64))
    task_uuid: Mapped[Optional[str]] = mapped_column(String(64))
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[Optional[str]] = mapped_column(String(32))
    content_type: Mapped[Optional[str]] = mapped_column(String(32))
    model: Mapped[Optional[str]] = mapped_column(String(128))
    processing_time_ms: Mapped[Optional[int]] = mapped_column(Integer)

    department: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    representative: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    prompt_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    prompt_name: Mapped[Optional[str]] = mapped_column(String(256))

    call_date: Mapped[Optional[datetime]] = mapped_column()
    communication_date: Mapped[Optional[datetime]] = mapped_column()
    created_at: Mapped[Optional[datetime]] = mapped_column()
    updated_at: Mapped[Optional[datetime]] = mapped_column()

    duration_minutes: Mapped[Optional[Float]] = mapped_column(Float)
    external_url: Mapped[Optional[str]] = mapped_column(Text)

    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    result_json: Mapped[Optional[dict]] = mapped_column(JSON)
    audio_file_json: Mapped[Optional[dict]] = mapped_column(JSON)
    transcription_text: Mapped[Optional[str]] = mapped_column(Text)
    transcription_json: Mapped[Optional[dict]] = mapped_column(JSON)
    tokens_json: Mapped[Optional[dict]] = mapped_column(JSON)

    summary: Mapped[Optional[str]] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(Text)


class User(Base):
    """Telegram user mapping to representative."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    representative_name: Mapped[str] = mapped_column(String(256), index=True, unique=True)
    phone: Mapped[str] = mapped_column(String(64), unique=True)
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class AdminLog(Base):
    """Audit log for admin actions."""

    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class InteractionLog(Base):
    """Store user questions/answers and error flags for feedback/eval."""

    __tablename__ = "interaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    chat_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    question: Mapped[Optional[str]] = mapped_column(Text)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    meta: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class RingostatCall(Base):
    __tablename__ = "ringostat_calls"
    __table_args__ = (Index("ix_ringostat_calls_call_id", "call_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    call_id2: Mapped[Optional[str]] = mapped_column(String(128))
    project_id: Mapped[Optional[str]] = mapped_column(String(64))
    direction: Mapped[Optional[str]] = mapped_column(String(16))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    talk_time: Mapped[Optional[int]] = mapped_column(Integer)
    ringing_time: Mapped[Optional[int]] = mapped_column(Integer)
    client_number: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(256))
    contact_company: Mapped[Optional[str]] = mapped_column(String(256))
    employee_number: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    responsible_name: Mapped[Optional[str]] = mapped_column(String(256))
    record_url: Mapped[Optional[str]] = mapped_column(Text)
    cost: Mapped[Optional[float]] = mapped_column(Float)
    call_scheme: Mapped[Optional[str]] = mapped_column(String(256))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    is_unique: Mapped[Optional[bool]] = mapped_column(Boolean)
    is_unique_targeted: Mapped[Optional[bool]] = mapped_column(Boolean)
    analysis_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    match_method: Mapped[Optional[str]] = mapped_column(String(32))
    match_time_diff_sec: Mapped[Optional[int]] = mapped_column(Integer)
    match_duration_diff_sec: Mapped[Optional[int]] = mapped_column(Integer)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Contact(Base):
    """Простой справочник телефон → имя/компания."""

    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_phone", "phone", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(256))
    company: Mapped[Optional[str]] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
