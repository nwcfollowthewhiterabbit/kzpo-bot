from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import Call
from .services import calls as call_service


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SCORE_EMOJI = {5: "✅", 3: "⚠️", 1: "❌"}
_METRIC_LABELS = {
    "Final Score": "Оцінка (бал)",
    "Call_Filter": "Фільтр дзвінка",
    "Wrong transcription": "Невірна транскрипція",
    "Summary": "Підсумок",
    "Title": "Тема",
    "Уточнюючі питання?": "Уточнюючі питання",
    "Уровень знаний": "Рівень знань",
    "Что именно не знал менеджер": "Що саме не знав менеджер",
    "Client_Complaints": "Скарги клієнта",
}
_METRIC_ORDER = [
    "Final Score",
    "Call_Filter",
    "Привітання",
    "Рівень знань",
    "Уточнюючі питання",
    "Обробка заперечень",
    "Спроба закриття замовлення",
    "Скарги клієнта",
    "Побажання клієнта",
    "Помилки менеджера",
    "Рекомендації",
    "Загальна оцінка",
    "Summary",
    "Title",
]

_ID_COLUMNS: List[Tuple[str, str]] = [
    ("call_id", "ID дзвінка"),
    ("analysis_id", "ID аналізу"),
]
_CONTEXT_COLUMNS: List[Tuple[str, str]] = [
    ("call_datetime", "Дата/час"),
    ("manager", "Менеджер"),
    ("department", "Відділ"),
]
_CLIENT_COLUMNS: List[Tuple[str, str]] = [
    ("client_name", "Клієнт"),
    ("company", "Компанія"),
    ("client_phone", "Телефон клієнта"),
]
_RINGOSTAT_COLUMNS: List[Tuple[str, str]] = [
    ("direction", "Напрямок"),
    ("status", "Статус"),
    ("duration", "Тривалість (хв)"),
    ("talk_time", "Розмова (сек)"),
    ("ringing_time", "Дзвінок (сек)"),
    ("record_url", "Запис"),
    ("rs_project_id", "Ringostat Project ID"),
]
_MARKETING_COLUMNS: List[Tuple[str, str]] = [
    ("sales_channel", "Канал"),
    ("sales_source", "Джерело"),
    ("market_comp", "Кампанія"),
    ("target_post", "Ключове оголошення"),
    ("keyword", "Ключове слово"),
    ("landing", "Landing"),
    ("last_page", "Остання сторінка"),
    ("referrer", "Referrer"),
    ("callback", "Callback"),
]
_GROUP_COLORS = {
    "ID": {"red": 0.91, "green": 0.91, "blue": 0.91},
    "Контекст": {"red": 0.95, "green": 0.95, "blue": 0.95},
    "Клієнт": {"red": 0.86, "green": 0.93, "blue": 0.86},
    "Ringostat": {"red": 0.86, "green": 0.91, "blue": 0.98},
    "ENIQ оцінки": {"red": 0.98, "green": 0.90, "blue": 0.84},
    "ENIQ текст": {"red": 0.98, "green": 0.88, "blue": 0.92},
    "Маркетинг": {"red": 0.98, "green": 0.96, "blue": 0.84},
}
_CALLS_SHEET_COLUMNS: List[Tuple[str, str]] = [
    ("call_display_id", "ID дзвінка"),
    ("call_datetime", "Дата-Час"),
    ("manager", "Менеджер"),
    ("client_phone", "Телефон клієнта"),
    ("duration", "Тривалість (хв)"),
    ("final_score", "Оцінка, бал"),
    ("hello", "Привітання"),
    ("knowledge", "Рівень знань"),
    ("questions", "Уточнюючі питання"),
    ("objections", "Обробка"),
    ("closing", "Спроба закрити"),
    ("overall_score", "Загальна оцінка"),
    ("summary", "Саммарі"),
    ("complaints", "Скарги клієнта"),
    ("wishes", "Побажання клієнта"),
]
_HIDDEN_CALL_METRICS = {
    "overall_score",
    "hello",
    "knowledge",
    "questions",
    "objections",
    "closing",
    "complaints",
    "wishes",
}
_METRIC_KEYS_MAP: Dict[str, List[str]] = {
    "call_filter": ["Call_Filter"],
    "final_score": ["Final Score"],
    "overall_score": ["Загальна оцінка"],
    "hello": ["Привітання", "Greeting", "Привітання та знайомство"],
    "knowledge": ["Рівень знань", "Рівень знать", "Tech_Level"],
    "questions": ["Уточнюючі питання"],
    "objections": ["Обробка заперечень", "Objection_Handling", "Робота з запереченнями", "Обробка"],
    "closing": ["Спроба закриття замовлення", "Attempt_To_Close", "Завершення розмови"],
    "complaints": ["Скарги клієнта", "Client_Complaints"],
    "wishes": ["Побажання клієнта", "Client_Wishes"],
    "summary": ["Summary"],
}


def _load_service_account_info() -> Dict[str, Any]:
    if settings.google_sheets_service_account_json:
        return json.loads(settings.google_sheets_service_account_json)
    if settings.google_sheets_service_account_file:
        with open(settings.google_sheets_service_account_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise RuntimeError("Google Sheets service account credentials not configured")


def _build_service():
    info = _load_service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _get_sheet_id(service, spreadsheet_id: str, tab_name: str) -> Optional[int]:
    sheet_meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in sheet_meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            return props.get("sheetId")
    return None


def _get_formula_separators(service, spreadsheet_id: str) -> Tuple[str, str]:
    sheet_meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    locale = (sheet_meta.get("properties", {}) or {}).get("locale", "en_US")
    if locale and locale.startswith("en"):
        return ",", ","
    return ";", "\\"


def _metric_label(key: str) -> str:
    return _METRIC_LABELS.get(key, key)


def _score_value(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except Exception:
        return None


def _is_numeric_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    raw = str(value).strip().replace(",", ".")
    return bool(re.fullmatch(r"-?\\d+(\\.\\d+)?", raw))


def _format_datetime(val: Optional[datetime]) -> str:
    if not val:
        return "-"
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return str(val)


def _format_direction(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if raw in {"in", "incoming"}:
        return "вхідний"
    if raw in {"out", "outgoing"}:
        return "вихідний"
    if raw == "callback":
        return "зворотний"
    return value or "-"


def _format_metric_value(label: str, value: Any) -> str:
    if label == "Фільтр дзвінка":
        raw = str(value or "").strip()
        if not raw or raw in {"-", "—"}:
            return "-"
        if raw.lower() == "irrelevant":
            return "🔴 Не цільовий"
        return f"🟢 {raw}"
    if value is None:
        return ""
    if isinstance(value, bool):
        return "так" if value else "ні"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, list):
        return "; ".join(str(item) for item in value) if value else "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    raw = str(value).strip()
    if not raw or raw in {"-", "—"}:
        return ""
    raw_num = raw.replace(",", ".")
    if re.fullmatch(r"-?\\d+(\\.\\d+)?", raw_num):
        try:
            return float(raw_num)
        except Exception:
            pass
    return raw


def _select_metric_value(result: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key not in result:
            continue
        value = result.get(key)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        if isinstance(value, str) and value.strip() in {"", "-", "—"}:
            continue
        return value
    return None


def _format_call_filter_compact(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw in {"-", "—"}:
        return ""
    if raw.lower() == "irrelevant":
        return "🔴 Не цільовий"
    if "sales" in raw.lower():
        return f"🟢 {raw}"
    return raw


def _score_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    try:
        return round(float(value), 2)
    except Exception:
        return _text_or_blank(value)


def _text_or_blank(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "так" if value else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if str(item).strip()) if value else ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    raw = str(value).strip()
    if not raw or raw in {"-", "—"}:
        return ""
    lowered = raw.lower()
    if lowered in {"no", "нет", "немає", "none", "нема", "відсутні"}:
        return ""
    return raw


def _extract_client_phone(call: Call) -> str:
    for src in (call.eniq_metadata_json or {}), (call.eniq_result_json or {}):
        for key, value in (src or {}).items():
            if "phone" in str(key).lower() and value:
                return str(value)
    if call.rs_client_number:
        return call.rs_client_number
    return "-"


def _client_phone_for_sheet(call: Call) -> str:
    phone = _extract_client_phone(call)
    return "" if phone in {"-", None} else str(phone)


def _call_time(call: Call) -> Optional[datetime]:
    return call.rs_start_time or call.eniq_call_date


def _is_answered(call: Call) -> bool:
    status = (call.rs_status or "").strip().upper()
    return status in {"ANSWER", "CONNECTED", "DONE"} or status == ""


def _call_filter_value(call: Call) -> Any:
    result = call.eniq_result_json if isinstance(call.eniq_result_json, dict) else {}
    return _select_metric_value(result, _METRIC_KEYS_MAP.get("call_filter", []))


def _is_sales_call(call_filter: Any) -> bool:
    raw = str(call_filter or "").strip().lower()
    return "sales" in raw if raw else False


def _is_retail_scheme(scheme: Optional[str]) -> bool:
    raw = (scheme or "").strip().lower()
    return raw.startswith("розниц")


def _passes_sales_retail_targeted(call: Call) -> bool:
    if not call.rs_is_unique_targeted:
        return False
    if not _is_retail_scheme(call.rs_call_scheme):
        return False
    call_filter = _call_filter_value(call)
    return _is_sales_call(call_filter)


def _row_for_calls_sheet(call: Call) -> List[Any]:
    result = call.eniq_result_json if isinstance(call.eniq_result_json, dict) else {}
    metric = lambda name: _select_metric_value(result, _METRIC_KEYS_MAP.get(name, []))
    call_time = _call_time(call)
    duration_minutes = call_service.duration_minutes_value(call)
    return [
        call.id,
        call_time.strftime("%Y-%m-%d %H:%M:%S") if call_time else "",
        call.rs_manager_name or call.eniq_representative or "",
        _client_phone_for_sheet(call),
        round(duration_minutes, 2) if duration_minutes is not None else "",
        _score_cell_value(metric("final_score")),
        _score_cell_value(metric("overall_score")),
        _score_cell_value(metric("hello")),
        _score_cell_value(metric("knowledge")),
        _score_cell_value(metric("questions")),
        _score_cell_value(metric("objections")),
        _score_cell_value(metric("closing")),
        _text_or_blank(metric("complaints")),
        _text_or_blank(metric("wishes")),
        _text_or_blank(metric("summary")),
    ]


def _collect_metric_keys(rows: List[Call]) -> Tuple[List[str], List[str]]:
    all_keys: List[str] = []
    seen = set()
    numeric_keys = set()
    for key in _METRIC_ORDER:
        seen.add(key)
        all_keys.append(key)
    for row in rows:
        result = row.eniq_result_json if isinstance(row.eniq_result_json, dict) else {}
        for key, value in result.items():
            if key not in seen:
                seen.add(key)
                all_keys.append(key)
            if _is_numeric_value(value):
                numeric_keys.add(key)
    numeric_order = [k for k in all_keys if k in numeric_keys]
    text_order = [k for k in all_keys if k not in numeric_keys]
    return numeric_order, text_order


def _write_calls_sheet(service, spreadsheet_id: str, tab_name: str, rows: List[Call]) -> None:
    headers = [title for _, title in _CALLS_SHEET_COLUMNS]
    filter_note = "Фільтри вкладки: Call_Filter містить 'sales'; схема = Розниця; тільки унікальні цільові (rs_is_unique_targeted)."
    values = [[filter_note] + [""] * (len(headers) - 1), headers]
    for row in rows:
        values.append(_row_for_calls_sheet(row))
    # capture existing column widths to reapply after rewrite
    existing_widths = _read_column_widths(service, spreadsheet_id, tab_name)
    sheet_id = _ensure_sheet(service, spreadsheet_id, tab_name)
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A:ZZ"
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    _apply_sheet_layout(
        service, spreadsheet_id, sheet_id, len(values), len(headers), frozen_rows=2, filter_row=1, enable_filter=False
    )
    if existing_widths:
        _apply_column_widths(service, spreadsheet_id, sheet_id, existing_widths)
    # Apply gentle conditional formatting for scores
    score_cols = ["final_score", "hello", "knowledge", "questions", "objections", "closing", "overall_score"]
    score_indexes = [idx for idx, (key, _) in enumerate(_CALLS_SHEET_COLUMNS) if key in score_cols]
    _apply_score_formatting(service, spreadsheet_id, sheet_id, score_indexes, data_start_row=2)
    hidden_indexes = [
        idx for idx, (key, _) in enumerate(_CALLS_SHEET_COLUMNS) if key in _HIDDEN_CALL_METRICS
    ]
    if hidden_indexes:
        group_defs = [{"start": min(hidden_indexes), "end": max(hidden_indexes) + 1}]
        _apply_column_groups(service, spreadsheet_id, sheet_id, group_defs)


def _clean_charts(service, spreadsheet_id: str, sheet_id: int) -> None:
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except HttpError:
        return
    requests = []
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") != sheet_id:
            continue
        for obj in sheet.get("embeddedObjects", []) or []:
            obj_id = obj.get("objectId")
            if obj_id is not None:
                requests.append({"deleteEmbeddedObject": {"objectId": obj_id}})
        break
    if requests:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError:
            return


def _score_numeric(call: Call) -> Optional[float]:
    result = call.eniq_result_json if isinstance(call.eniq_result_json, dict) else {}
    val = _select_metric_value(result, _METRIC_KEYS_MAP.get("final_score", []))
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        try:
            return float(str(val).replace(",", "."))
        except Exception:
            return None


def _avg(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _build_dashboard(service, spreadsheet_id: str, tab_name: str, rows: List[Call]) -> None:
    sheet_id = _ensure_sheet(service, spreadsheet_id, tab_name)
    _clean_charts(service, spreadsheet_id, sheet_id)
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A:ZZ"
    ).execute()
    tz = pytz.timezone(settings.report_timezone)
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    # Manager stats (all time, accepted only)
    mgr_stats: Dict[str, Dict[str, Any]] = {}
    for call in rows:
        mgr = call.rs_manager_name or call.eniq_representative or "—"
        info = mgr_stats.setdefault(mgr, {"accepted": 0, "scores": []})
        if _is_answered(call):
            info["accepted"] += 1
        sc = _score_numeric(call)
        if sc is not None:
            info["scores"].append(sc)

    # Recent 10 days score trend per manager
    cutoff = datetime.utcnow() - timedelta(days=10)
    day_mgr_scores: Dict[str, Dict[str, List[float]]] = {}
    days_set = set()
    for call in rows:
        ct = _call_time(call)
        if not ct or ct < cutoff:
            continue
        day = ct.strftime("%Y-%m-%d")
        days_set.add(day)
        mgr = call.rs_manager_name or call.eniq_representative or "—"
        sc = _score_numeric(call)
        if sc is None:
            continue
        day_mgr_scores.setdefault(day, {}).setdefault(mgr, []).append(sc)
    days_sorted = sorted(days_set)
    mgr_sorted = sorted(mgr_stats.keys())

    values: List[List[Any]] = [
        [f"KZPO Calls Dashboard ({tab_name})"],
        ["Оновлено (локальне время)", now_str],
        ["Фільтри", "Call_Filter містить 'sales'; схема = Розниця; тільки rs_is_unique_targeted=True"],
        [],
        ["Менеджери — прийняті унікальні цільові (всі часи)"],
        ["Менеджер", "Прийнято", "Ср. оцінка"],
    ]
    for mgr in sorted(mgr_stats.keys(), key=lambda m: mgr_stats[m]["accepted"], reverse=True):
        info = mgr_stats[mgr]
        avg_score_mgr = _avg(info["scores"])
        values.append(
            [
                mgr,
                info["accepted"],
                avg_score_mgr if avg_score_mgr is not None else "—",
            ]
        )
    values += [
        [],
        ["Середня оцінка за 10 днів (по днях і менеджерах)"],
    ]
    header = ["Дата"] + mgr_sorted
    values.append(header)
    for day in days_sorted:
        row = [day]
        mgr_scores = day_mgr_scores.get(day, {})
        for mgr in mgr_sorted:
            scores = mgr_scores.get(mgr, [])
            row.append(_avg(scores) if scores else "")
        values.append(row)

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    max_cols = max(len(r) for r in values) if values else 1
    _apply_sheet_layout(service, spreadsheet_id, sheet_id, len(values) + 2, max_cols + 2, enable_filter=False)

def _week_meta(tz: str) -> Tuple[datetime, datetime, str]:
    tzinfo = pytz.timezone(tz)
    now = datetime.now(tzinfo)
    # weeks start on Thursday 00:00 local
    offset_days = (now.weekday() - 3) % 7  # Thursday = 3
    week_start_local = (now - timedelta(days=offset_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end_local = week_start_local + timedelta(days=7)
    iso_year, iso_week, _ = week_start_local.isocalendar()
    tab_name = f"{iso_year}-W{iso_week:02d}"
    start_utc = week_start_local.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = week_end_local.astimezone(pytz.UTC).replace(tzinfo=None)
    return start_utc, end_utc, tab_name


def _build_column_layout(
    numeric_keys: List[str],
    text_keys: List[str],
) -> Tuple[List[str], List[Optional[str]], List[str], List[Dict[str, Any]]]:
    eniq_numeric = [(f"eniq_num:{key}", _metric_label(key)) for key in numeric_keys]
    eniq_text = [(f"eniq_text:{key}", _metric_label(key)) for key in text_keys]
    groups = [
        ("ID", _ID_COLUMNS),
        ("Контекст", _CONTEXT_COLUMNS),
        ("Клієнт", _CLIENT_COLUMNS),
        ("Ringostat", _RINGOSTAT_COLUMNS),
        ("ENIQ оцінки", eniq_numeric),
        ("ENIQ текст", eniq_text),
        ("Маркетинг", _MARKETING_COLUMNS),
    ]
    groups = [(label, cols) for label, cols in groups if cols]

    headers: List[str] = []
    column_keys: List[Optional[str]] = []
    group_row: List[str] = []
    group_defs: List[Dict[str, Any]] = []

    for idx, (label, cols) in enumerate(groups):
        start = len(headers)
        for key, title in cols:
            headers.append(title)
            column_keys.append(key)
            group_row.append("")
        group_row[start] = label
        group_defs.append({"label": label, "start": start, "end": len(headers)})
        if idx < len(groups) - 1:
            headers.append("")
            column_keys.append(None)
            group_row.append("")
    return headers, column_keys, group_row, group_defs


def _row_for_call(call: Call, column_keys: List[Optional[str]]) -> List[Any]:
    call_time = call.rs_start_time or call.eniq_call_date
    duration_minutes = call_service.duration_minutes_value(call)
    base = {
        "call_id": call.call_id or "-",
        "analysis_id": call.eniq_analysis_id or "-",
        "call_datetime": _format_datetime(call_time),
        "manager": call.rs_manager_name or call.eniq_representative or "-",
        "department": call.eniq_department or call.rs_call_scheme or "-",
        "client_name": (call.eniq_metadata_json or {}).get("Customer") or "-",
        "company": (call.eniq_metadata_json or {}).get("Company")
        or (call.eniq_metadata_json or {}).get("Organization")
        or "-",
        "client_phone": _extract_client_phone(call),
        "direction": _format_direction(call.rs_direction or call.rs_call_type),
        "status": call.rs_status or "-",
        "duration": round(duration_minutes, 2) if duration_minutes is not None else "",
        "talk_time": int(call.rs_talk_time) if call.rs_talk_time is not None else "",
        "ringing_time": int(call.rs_ringing_time) if call.rs_ringing_time is not None else "",
        "call_scheme": call.rs_call_scheme or "-",
        "record_url": call.eniq_external_url or call.rs_record_url or "-",
        "rs_project_id": call.rs_project_id or "-",
        "sales_channel": call.rs_sales_channel or "-",
        "sales_source": call.rs_sales_source or "-",
        "market_comp": call.rs_market_comp or "-",
        "target_post": call.rs_target_post or "-",
        "keyword": call.rs_keyword or "-",
        "landing": call.rs_landing or "-",
        "last_page": call.rs_last_page or "-",
        "referrer": call.rs_referrer or "-",
        "callback": call.rs_callback or "-",
    }
    result = call.eniq_result_json if isinstance(call.eniq_result_json, dict) else {}
    row: List[Any] = []
    for key in column_keys:
        if key is None:
            row.append("")
            continue
        if key.startswith("eniq_num:") or key.startswith("eniq_text:"):
            metric_key = key.split(":", 1)[1]
            label = _metric_label(metric_key)
            row.append(_format_metric_value(label, result.get(metric_key)))
        else:
            row.append(base.get(key, ""))
    return row


def _ensure_sheet(service, spreadsheet_id: str, tab_name: str) -> int:
    sheet_meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in sheet_meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            return props["sheetId"]
    add_resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    replies = add_resp.get("replies", [])
    return replies[0]["addSheet"]["properties"]["sheetId"]


def _apply_sheet_layout(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    rows: int,
    cols: int,
    frozen_rows: int = 1,
    filter_row: int = 0,
    enable_filter: bool = True,
) -> None:
    requests: List[Dict[str, Any]] = []
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": frozen_rows}},
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    if enable_filter:
        requests.append(
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": filter_row,
                            "endRowIndex": rows,
                            "startColumnIndex": 0,
                            "endColumnIndex": cols,
                        }
                    }
                }
            }
        )
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def _apply_score_formatting(
    service, spreadsheet_id: str, sheet_id: int, score_col_indexes: List[int], data_start_row: int
) -> None:
    if not score_col_indexes:
        return
    requests: List[Dict[str, Any]] = []
    green = {"red": 0.88, "green": 0.96, "blue": 0.88}
    red = {"red": 0.98, "green": 0.86, "blue": 0.86}
    for col in score_col_indexes:
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": data_start_row,
                                "startColumnIndex": col,
                                "endColumnIndex": col + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_EQ",
                                "values": [{"userEnteredValue": "5"}],
                            },
                            "format": {"backgroundColor": green},
                        },
                    },
                    "index": 0,
                }
            }
        )
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": data_start_row,
                                "startColumnIndex": col,
                                "endColumnIndex": col + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_BETWEEN",
                                "values": [
                                    {"userEnteredValue": "1"},
                                    {"userEnteredValue": "2"},
                                ],
                            },
                            "format": {"backgroundColor": red},
                        },
                    },
                    "index": 0,
                }
            }
        )
    if requests:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError:
            return


def _read_column_widths(service, spreadsheet_id: str, tab_name: str) -> Optional[List[int]]:
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            ranges=[tab_name],
            includeGridData=True,
            fields="sheets(data(columnMetadata(pixelSize)))",
        ).execute()
    except HttpError:
        return None
    sheets = meta.get("sheets", [])
    if not sheets:
        return None
    cols = sheets[0].get("data", [{}])[0].get("columnMetadata", [])
    widths = [c.get("pixelSize") for c in cols]
    return widths if any(widths) else None


def _apply_column_widths(service, spreadsheet_id: str, sheet_id: int, widths: List[Optional[int]]) -> None:
    requests: List[Dict[str, Any]] = []
    for idx, width in enumerate(widths):
        if width:
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": idx,
                            "endIndex": idx + 1,
                        },
                        "properties": {"pixelSize": width},
                        "fields": "pixelSize",
                    }
                }
            )
    if not requests:
        return
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
    except HttpError:
        return


def _apply_group_headers(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    group_defs: List[Dict[str, Any]],
) -> None:
    requests: List[Dict[str, Any]] = []
    if group_defs:
        max_end = max(g["end"] for g in group_defs)
        requests.append(
            {
                "unmergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": max_end,
                    }
                }
            }
        )
    for group in group_defs:
        start = group["start"]
        end = group["end"]
        label = group["label"]
        color = _GROUP_COLORS.get(label)
        if end - start < 1:
            continue
        requests.append(
            {
                "mergeCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": start,
                        "endColumnIndex": end,
                    },
                    "mergeType": "MERGE_ALL",
                }
            }
        )
        if color:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": start,
                            "endColumnIndex": end,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": color,
                                "textFormat": {"bold": True},
                                "horizontalAlignment": "CENTER",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                    }
                }
            )
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()


def _list_sheets(service, spreadsheet_id: str) -> List[Dict[str, Any]]:
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,index))",
        ).execute()
    except HttpError:
        return []
    return [s.get("properties", {}) for s in meta.get("sheets", [])]


def _copy_sheet(service, spreadsheet_id: str, source_sheet_id: int, new_title: str) -> Optional[int]:
    try:
        resp = service.spreadsheets().sheets().copyTo(
            spreadsheetId=spreadsheet_id,
            sheetId=source_sheet_id,
            body={"destinationSpreadsheetId": spreadsheet_id},
        ).execute()
        new_id = resp.get("sheetId")
        if new_id is None:
            return None
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": new_id, "title": new_title},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()
        return new_id
    except HttpError:
        return None


def _delete_other_sheets(service, spreadsheet_id: str, keep_title: str) -> None:
    sheets = _list_sheets(service, spreadsheet_id)
    requests = []
    for sheet in sheets:
        if sheet.get("title") != keep_title:
            requests.append({"deleteSheet": {"sheetId": sheet.get("sheetId")}})
    if not requests:
        return
    try:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    except HttpError:
        return


def _ensure_week_sheet(service, spreadsheet_id: str, tab_name: str) -> int:
    sheets = _list_sheets(service, spreadsheet_id)
    for sheet in sheets:
        if sheet.get("title") == tab_name:
            return sheet.get("sheetId")
    template_sheet = sheets[0] if sheets else None
    if template_sheet and template_sheet.get("sheetId") is not None:
        new_id = _copy_sheet(service, spreadsheet_id, int(template_sheet["sheetId"]), tab_name)
        if new_id is not None:
            return new_id
    return _ensure_sheet(service, spreadsheet_id, tab_name)


def _apply_column_groups(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    group_defs: List[Dict[str, Any]],
) -> None:
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sheet in meta.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != sheet_id:
                continue
            existing = sheet.get("columnGroups", [])
            if existing:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "requests": [
                            {"deleteDimensionGroup": {"range": g["range"]}} for g in existing
                        ]
                    },
                ).execute()
            break
    except HttpError:
        pass

    for group in group_defs:
        start = group["start"]
        end = group["end"]
        if end - start < 2:
            continue
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addDimensionGroup": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": start,
                                    "endIndex": end,
                                }
                            }
                        }
                    ]
                },
            ).execute()
        except HttpError:
            continue
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateDimensionGroup": {
                                "dimensionGroup": {
                                    "range": {
                                        "sheetId": sheet_id,
                                        "dimension": "COLUMNS",
                                        "startIndex": start,
                                        "endIndex": end,
                                    },
                                    "collapsed": True,
                                },
                                "fields": "collapsed",
                            }
                        }
                    ]
                },
            ).execute()
        except HttpError:
            continue


def sync_calls_to_sheet(full_refresh: bool = True) -> None:
    if not settings.google_sheets_enabled:
        return
    if not settings.google_sheets_spreadsheet_id:
        logging.getLogger(__name__).warning("Google Sheets sync enabled, but spreadsheet id is missing")
        return
    try:
        service = _build_service()
    except Exception as exc:
        logging.getLogger(__name__).warning("Google Sheets auth failed: %s", exc)
        return
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    with get_session() as session:
        rows = session.scalars(
            select(Call)
            .where(call_service.call_time_expr().is_not(None))
            .order_by(call_service.call_time_expr().desc().nullslast())
        ).all()
    rows = [call for call in rows if _passes_sales_retail_targeted(call)]
    try:
        week_start, week_end, week_tab = _week_meta(settings.report_timezone)
        _ensure_week_sheet(service, spreadsheet_id, week_tab)
        week_rows = [
            r for r in rows if (ct := _call_time(r)) and week_start <= ct < week_end
        ]
        _write_calls_sheet(service, spreadsheet_id, week_tab, week_rows)
        _delete_other_sheets(service, spreadsheet_id, week_tab)
    except HttpError as exc:
        logging.getLogger(__name__).warning("Google Sheets sync failed: %s", exc)


_last_daily_sync_date: Optional[datetime.date] = None


async def sheets_sync_scheduler() -> None:
    if not settings.google_sheets_enabled:
        return
    interval = 30 * 60  # 30 minutes for weekly tab refresh
    tz = pytz.timezone(settings.report_timezone)
    global _last_daily_sync_date
    while True:
        now = datetime.now(tz)
        full_refresh = False
        if _last_daily_sync_date is None or _last_daily_sync_date != now.date():
            if now.hour >= 19:
                full_refresh = True
                _last_daily_sync_date = now.date()
        try:
            await asyncio.to_thread(sync_calls_to_sheet, full_refresh)
        except Exception as exc:  # pragma: no cover - runtime safety
            logging.getLogger(__name__).warning("Google Sheets sync failed: %s", exc)
        await asyncio.sleep(interval)


def setup_dashboard_sheet() -> None:
    if not settings.google_sheets_enabled or not settings.google_sheets_spreadsheet_id:
        logging.getLogger(__name__).warning("Google Sheets dashboard: spreadsheet id is missing")
        return
    try:
        service = _build_service()
    except Exception as exc:
        logging.getLogger(__name__).warning("Google Sheets auth failed: %s", exc)
        return
    source_tab = settings.google_sheets_tab_name or "Дзвінки"
    _build_dashboard(service, settings.google_sheets_spreadsheet_id, source_tab, "Дашборд")
