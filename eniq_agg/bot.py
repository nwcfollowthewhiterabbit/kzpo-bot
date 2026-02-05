import asyncio
import logging
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from sqlalchemy import select, func, case
from openai import OpenAI

from .config import settings
from .db import get_session
from .ingest import ensure_schema, ingest_all
from .models import Analysis, User, AdminLog, InteractionLog, Call
from .stats import (
    daily_metrics,
    list_representatives,
    get_score,
    weekly_org_metrics,
    day_stats_for_rep,
    admin_snapshot,
)
from .services import calls as call_service
from .utils.time_utils import day_range
from .tools import (
    get_departments,
    get_calls,
    get_metrics,
    get_call_summary,
)
from .ringostat_ingest import fetch_incremental as fetch_ringostat_calls
from .ringostat_webhook import start_webhook_server
from .sheets_sync import sheets_sync_scheduler


pending_actions: Dict[int, Dict[str, Any]] = {}
_ingest_lock = asyncio.Lock()
_openai_client: Optional[OpenAI] = None
# простая краткосрочная память: до 5 записей, не старше 24ч
_context_memory: Dict[int, List[Dict[str, Any]]] = {}
_context_ttl = timedelta(hours=24)


def get_openai_client() -> Optional[OpenAI]:
    global _openai_client
    if _openai_client or not settings.openai_api_key:
        return _openai_client
    _openai_client = OpenAI(api_key=settings.openai_api_key)
    return _openai_client


def _safe_json_loads(text: str) -> Optional[dict]:
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned)
    except Exception:
        return None


def compute_rep_stats(representative: str) -> Dict[str, Any]:
    with get_session() as session:
        today = day_stats_for_rep(session, representative, settings.report_timezone, day_offset=0)
        yesterday = day_stats_for_rep(session, representative, settings.report_timezone, day_offset=1)
    return {"today": today, "yesterday": yesterday}


def compute_admin_stats() -> Dict[str, Any]:
    with get_session() as session:
        return admin_snapshot(session, settings.report_timezone)


def count_calls_between(start: datetime, end: datetime) -> int:
    with get_session() as session:
        return call_service.count_calls(session, start, end)


def count_calls_for_day(offset_days: int = 0) -> int:
    start_naive, end_naive = day_range(settings.report_timezone, offset_days=offset_days)
    return count_calls_between(start_naive, end_naive)


def _normalize_direction(direction: str) -> str:
    d = (direction or "").strip().lower()
    if d in {"out", "outgoing", "outbound", "исход", "исходящие"}:
        return "out"
    if d in {"in", "incoming", "inbound", "вход", "входящие"}:
        return "in"
    return d


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
_METRIC_ICONS = {
    "Оцінка (бал)": "⭐",
    "Фільтр дзвінка": "🎯",
    "Тема": "🧾",
    "Підсумок": "📝",
    "Невірна транскрипція": "📝",
    "Привітання": "👋",
    "Рівень знань": "🧠",
    "Уточнюючі питання": "❓",
    "Обробка заперечень": "🛡️",
    "Спроба закриття замовлення": "✅",
    "Скарги клієнта": "🗯️",
    "Побажання клієнта": "💡",
    "Рекомендації": "📌",
    "Помилки менеджера": "⚠️",
    "Загальна оцінка": "🏁",
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
_SCORE_EMOJI = {5: "✅", 3: "⚠️", 1: "❌"}


def _score_value(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except Exception:
        return None


def _metric_label(key: str) -> str:
    return _METRIC_LABELS.get(key, key)


def _format_call_filter(value: Any) -> str:
    if value is None:
        return "—"
    raw = str(value).strip()
    if not raw or raw in {"-", "—"}:
        return "—"
    if raw.lower() == "irrelevant":
        return "🔴 Не цільовий"
    return f"🟢 {raw}"


def _format_metric_value(label: str, value: Any) -> str:
    if label == "Фільтр дзвінка":
        return _format_call_filter(value)
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "так" if value else "ні"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        score = _score_value(value)
        if score in _SCORE_EMOJI:
            return f"{_SCORE_EMOJI[score]} {score}"
        return str(value)
    if isinstance(value, list):
        if not value:
            return "—"
        items = []
        for item in value:
            if isinstance(item, (dict, list)):
                items.append(json.dumps(item, ensure_ascii=True))
            else:
                items.append(str(item))
        return "; ".join(items)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True)
    raw = str(value).strip()
    if not raw or raw in {"-", "—"}:
        return "—"
    score = _score_value(raw)
    if score in _SCORE_EMOJI:
        return f"{_SCORE_EMOJI[score]} {score}"
    return raw


def _format_metric_line(key: str, value: Any) -> str:
    label = _metric_label(key)
    icon = _METRIC_ICONS.get(label, "•")
    value_text = _format_metric_value(label, value)
    return f"{icon} {label}: {value_text}"


def _format_direction(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    if raw in {"in", "incoming"}:
        return "вхідний"
    if raw in {"out", "outgoing"}:
        return "вихідний"
    if raw == "callback":
        return "зворотний"
    return value or "—"


def _format_duration(minutes: Optional[float]) -> str:
    if minutes is None:
        return "—"
    return f"{minutes:.2f} хв"


def _ordered_metric_items(result_json: Dict[str, Any]) -> List[tuple[str, Any]]:
    if not result_json:
        return []
    keys = list(result_json.keys())
    ordered = []
    for key in _METRIC_ORDER:
        if key in result_json:
            ordered.append(key)
    for key in sorted(keys, key=lambda k: str(k).lower()):
        if key not in ordered:
            ordered.append(key)
    return [(k, result_json[k]) for k in ordered]


def count_calls_between_direction(start: datetime, end: datetime, direction: str) -> int:
    """Count calls by direction using rs_direction/rs_call_type."""
    with get_session() as session:
        return call_service.count_calls(
            session, start, end, direction=_normalize_direction(direction)
        )


def count_calls_for_day_direction(offset_days: int = 0, direction: str = "in") -> int:
    start_naive, end_naive = day_range(settings.report_timezone, offset_days=offset_days)
    return count_calls_between_direction(start_naive, end_naive, direction)


def manager_call_counts_for_day(offset_days: int = 0, order: str = "desc") -> list[tuple[str, int]]:
    start_naive, end_naive = day_range(settings.report_timezone, offset_days=offset_days)
    time_col = call_service.call_time_expr()
    name_col = call_service.rep_expr()
    stmt = (
        select(name_col.label("rep"), func.count().label("cnt"))
        .where(
            time_col.is_not(None),
            time_col >= start_naive,
            time_col < end_naive,
            name_col.is_not(None),
        )
        .group_by(name_col)
    )
    if order == "asc":
        stmt = stmt.order_by(func.count().asc())
    else:
        stmt = stmt.order_by(func.count().desc())
    with get_session() as session:
        return session.execute(stmt).all()


def count_calls_without_greeting(offset_days: int = 0) -> tuple[int, int]:
    """(total, without_greeting) for a day offset."""
    zone = pytz.timezone(settings.report_timezone)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=offset_days)
    end = start + timedelta(days=1)
    start_naive, end_naive = start.replace(tzinfo=None), end.replace(tzinfo=None)
    with get_session() as session:
        rows = session.scalars(
            select(Analysis).where(
                Analysis.call_date.is_not(None),
                Analysis.call_date >= start_naive,
                Analysis.call_date < end_naive,
            )
        ).all()
    total = len(rows)
    without = sum(1 for r in rows if not is_truthy((r.result_json or {}).get("Привітання")))
    return total, without


def count_calls_last_days(days: int) -> int:
    """Count calls for the last N days including today."""
    days = max(1, min(days, 90))
    zone = pytz.timezone(settings.report_timezone)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return count_calls_between(start.replace(tzinfo=None), end.replace(tzinfo=None))


def daily_counts_last_days(days: int) -> List[Dict[str, Any]]:
    days = max(1, min(days, 90))
    zone = pytz.timezone(settings.report_timezone)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    with get_session() as session:
        time_col = call_service.call_time_expr()
        rows = session.execute(
            select(func.date(time_col).label("d"), func.count().label("cnt"))
            .select_from(Call)
            .where(
                time_col.is_not(None),
                time_col >= start.replace(tzinfo=None),
                time_col <= end.replace(tzinfo=None),
            )
            .group_by(func.date(time_col))
            .order_by(func.date(time_col).asc())
        ).all()
    return [{"date": str(r[0]), "calls": r[1]} for r in rows]


def is_stats_query(text: str) -> bool:
    """Rudimentary intent check: respond only to статистика/звонки queries."""
    t = (text or "").lower()
    keywords = ["стат", "metrics", "анализ", "отчет", "report", "дней", "day", "недел"]
    if has_call_keyword(t):
        return True
    return any(k in t for k in keywords)


def is_greeting_query(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    greetings = [
        "привет",
        "здрав",
        "добрый",
        "hello",
        "hi",
        "hey",
        "yo",
        "алло",
        "пинг",
        "ты тут",
        "тут?",
    ]
    return any(g in t for g in greetings)


_CALL_KEYWORDS = ("звон", "call", "calls", "вызов", "дзвін", "дзвон")
_CALL_FUZZY_BASES = ("звонок", "звонки", "дзвінок", "дзвінки")


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            insert = curr[j - 1] + 1
            delete = prev[j] + 1
            replace = prev[j - 1] + (ca != cb)
            curr.append(min(insert, delete, replace))
        prev = curr
    return prev[-1]


def has_call_keyword(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in _CALL_KEYWORDS):
        return True
    tokens = re.findall(r"[a-zа-яёіїєґ]+", t)
    for tok in tokens:
        if 4 <= len(tok) <= 8:
            for base in _CALL_FUZZY_BASES:
                if _edit_distance(tok, base) <= 2:
                    return True
    return False


def remember_context(chat_id: int, payload: Dict[str, Any]) -> None:
    now = datetime.utcnow()
    items = _context_memory.get(chat_id, [])
    # prune old
    items = [p for p in items if now - p.get("ts", now) < _context_ttl]
    items.append({"ts": now, **payload})
    _context_memory[chat_id] = items[-5:]


def get_last_context(chat_id: int, key: str) -> Optional[Any]:
    now = datetime.utcnow()
    for p in reversed(_context_memory.get(chat_id, [])):
        if now - p.get("ts", now) > _context_ttl:
            continue
        if key in p:
            return p[key]
    return None


def normalize_phone(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    digits = "".join(ch for ch in str(val) if ch.isdigit())
    return digits or None


def is_truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in {"yes", "true", "1", "так", "y", "ok"} or (s and s not in {"no", "false", "0", "none", "null", "немає"})


def flag_line(label: str, val: Any, positive_when_true: bool = True, yes_text: str = "так", no_text: str = "ні") -> str:
    raw = is_truthy(val)
    ok = raw if positive_when_true else not raw
    emoji = "🟢" if ok else "🔴"
    text = yes_text if raw else no_text
    return f"{emoji} {label}: {text}"


def log_interaction(
    chat_id: int,
    user: Optional[User],
    question: str,
    answer: str,
    is_error: bool = False,
    reason: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    with get_session() as session:
        session.add(
            InteractionLog(
                user_id=user.id if user else None,
                chat_id=str(chat_id) if chat_id else None,
                question=question,
                answer=answer,
                is_error=is_error,
                reason=reason,
                meta=meta,
            )
        )


def first_call_today() -> Optional[Analysis]:
    zone = pytz.timezone(settings.report_timezone)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_naive = start.replace(tzinfo=None)
    end_naive = start_naive + timedelta(days=1)
    with get_session() as session:
        return session.scalar(
            select(Analysis)
            .where(
                Analysis.call_date.is_not(None),
                Analysis.call_date >= start_naive,
                Analysis.call_date < end_naive,
            )
            .order_by(Analysis.call_date.asc())
        )


def latest_call() -> Optional[Analysis]:
    with get_session() as session:
        return session.scalar(
            select(Analysis)
            .where(Analysis.call_date.is_not(None))
            .order_by(Analysis.call_date.desc())
        )


def compute_admin_daily_report(session, tz: str) -> List[Dict[str, Any]]:
    zone = pytz.timezone(tz)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    start_naive, end_naive = start.replace(tzinfo=None), end.replace(tzinfo=None)
    rows = session.scalars(
        select(Analysis)
        .where(Analysis.call_date >= start_naive, Analysis.call_date < end_naive)
    ).all()
    ringostat_counts = {
        rep or "-": {
            "unique": int(unique or 0),
            "unique_targeted": int(unique_targeted or 0),
        }
        for rep, unique, unique_targeted in session.execute(
            select(
                Call.eniq_representative,
                func.sum(case((Call.rs_is_unique.is_(True), 1), else_=0)).label("unique"),
                func.sum(case((Call.rs_is_unique_targeted.is_(True), 1), else_=0)).label("unique_targeted"),
            )
            .select_from(Call)
            .where(Call.eniq_call_date >= start_naive, Call.eniq_call_date < end_naive)
            .group_by(Call.eniq_representative)
        ).all()
    }
    per_rep: Dict[str, Dict[str, Any]] = {}
    for a in rows:
        rep = a.representative or "-"
        info = per_rep.setdefault(
            rep,
            {
                "department": a.department or "-",
                "score_sum": 0.0,
                "score_count": 0,
                "complaints": 0,
            },
        )
        sc = get_score(a)
        if sc is not None:
            info["score_sum"] += sc
            info["score_count"] += 1
        if is_truthy((a.result_json or {}).get("Скарги клієнта")):
            info["complaints"] += 1
    result = []
    for rep, data in per_rep.items():
        avg_score = (
            data["score_sum"] / data["score_count"] if data["score_count"] else None
        )
        result.append(
            {
                "rep": rep,
                "department": data["department"],
                "ringostat_unique": ringostat_counts.get(rep, {}).get("unique", 0),
                "ringostat_unique_targeted": ringostat_counts.get(rep, {}).get("unique_targeted", 0),
                "avg_score": avg_score,
                "complaints": data["complaints"],
            }
        )
    result.sort(key=lambda x: x["rep"])
    return result


async def send_admin_daily_report(bot: Bot) -> None:
    with get_session() as session:
        admins = session.scalars(
            select(User).where(User.is_admin.is_(True), User.telegram_chat_id.is_not(None))
        ).all()
        report = compute_admin_daily_report(session, settings.report_timezone)
    if not admins:
        return
    date_str = datetime.now(pytz.timezone(settings.report_timezone)).strftime("%Y-%m-%d")
    lines = [f"📊 Щоденний звіт за {date_str}"]
    if not report:
        lines.append("Даних за сьогодні немає.")
    else:
        for r in report:
            avg = r.get("avg_score")
            if avg is None:
                score_str = "Оцінка: —"
            else:
                prefix = "🟢" if avg >= 4 else "🔴"
                score_str = f"{prefix} Середня оцінка: {avg:.2f}"
            lines.append(f"{r['rep']} ({r['department']}):")
            lines.append(
                f"Ringostat унік.: {r['ringostat_unique']}, унік. цільовий: {r['ringostat_unique_targeted']}"
            )
            lines.append(score_str)
            if r.get("complaints"):
                lines.append(f"🔴 Скарги: {r['complaints']}")
            lines.append("")  # пустая строка между менеджерами
    text = "\n".join(lines)
    for adm in admins:
        try:
            await bot.send_message(chat_id=adm.telegram_chat_id, text=text)
            log_interaction(int(adm.telegram_chat_id), adm, "[auto] admin_daily_report", text)
        except Exception:
            continue


async def run_planner(question: str, admin_mode: bool) -> Optional[Dict[str, Any]]:
    client = get_openai_client()
    if not client:
        return None
    system_prompt = (
        "Ты работаешь как планировщик. Отвечай ТОЛЬКО JSON без пояснений.\n"
        "Сначала выбери инструмент, который нужен для ответа, и верни команду вида:\n"
        '{"action":"get_departments","params":{"date_from":"today","date_to":"today"}}\n'
        'или {"action":"get_calls","params":{"filters":{"date_from":"today"},"sort":{"field":"call_date","direction":"asc"},"limit":1}}\n'
        'Если вопрос про отделы/департаменты/направления — всегда используй get_departments.\n'
        'Если про первый/последний/длинный звонок — get_calls c sort.\n'
        'Если про метрики по группам — get_metrics с group_by.\n'
        'Если про разрез по дням/последние N дней — используй get_metrics с group_by="date" и нужным диапазоном дат.\n'
        'Если про входящие/исходящие/направления — используй get_metrics с group_by="direction".\n'
        'Если про схемы звонка/маршрутизацию — используй get_metrics с group_by="call_scheme".\n'
        'Не придумывай данные и не отвечай текстом. Если нет подходящего инструмента — верни null.\n'
    )
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            max_tokens=200,
        )
        content = resp.choices[0].message.content or ""
        data = _safe_json_loads(content)
        if isinstance(data, dict) and data.get("action"):
            return data
    except Exception:
        return None
    return None


def execute_tool(command: Dict[str, Any]) -> Dict[str, Any]:
    action = command.get("action")
    params = command.get("params") or {}
    with get_session() as session:
        if action == "get_departments":
            return {"action": action, "result": get_departments(session, settings.report_timezone, params.get("date_from"), params.get("date_to"))}
        if action == "get_calls":
            return {
                "action": action,
                "result": get_calls(
                    session,
                    settings.report_timezone,
                    filters=params.get("filters"),
                    sort=params.get("sort"),
                    limit=int(params.get("limit") or 5),
                ),
            }
        if action == "get_metrics":
            return {
                "action": action,
                "result": get_metrics(
                    session,
                    settings.report_timezone,
                    date_from=params.get("date_from"),
                    date_to=params.get("date_to"),
                    group_by=params.get("group_by") or params.get("groupBy") or "department",
                ),
            }
        if action == "get_call_summary":
            return {
                "action": action,
                "result": get_call_summary(session, int(params.get("call_id"))),
            }
    return {"action": action, "result": None}


async def run_composer(question: str, tool_payload: Dict[str, Any], lang_ru: bool) -> str:
    client = get_openai_client()
    if not client:
        # simple fallback summary
        res = tool_payload.get("result") or {}
        if isinstance(res, dict) and "calls" in res and res["calls"]:
            first = res["calls"][0]
            base = f"{first.get('date')} {first.get('rep') or '-'}: {first.get('summary') or '-'} ({first.get('url') or '-'})"
            return base
        return "Данные получены, но не удалось сформировать ответ без LLM." if lang_ru else "Data fetched, but cannot compose answer without LLM."
    system_prompt = (
        "Ты отвечаешь пользователю, используя только предоставленный результат инструмента. "
        "Не выдумывай данных. Если списки пустые — скажи, что за выбранный период нет данных. "
        "Не добавляй комментарии про активность/оценки, которых нет в данных. "
        "Отвечай только на запрошенный аспект, не добавляй другие метрики, если их не просили. "
        "Дай короткий ответ (2-4 предложения) на языке вопроса."
    )
    content = f"QUESTION: {question}\nDATA: {json.dumps(tool_payload, default=str, ensure_ascii=False)}"
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            max_tokens=250,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return "Не удалось сформировать ответ." if lang_ru else "Could not compose answer."


async def build_chat_reply(user: User, question: str, admin_mode: bool = False) -> str:
    user_stats = compute_rep_stats(user.representative_name)
    admin_stats = compute_admin_stats() if admin_mode else None
    client = get_openai_client()
    # simple language guess
    is_cyr = bool(re.search("[А-Яа-яЁёІіЇїЄєҐґ]", question or ""))
    fallback_lang = "ru" if is_cyr else "en"
    if not client:
        # fallback without LLM
        t = user_stats["today"]
        y = user_stats["yesterday"]
        if admin_mode and admin_stats:
            today_org = admin_stats["today"]
            week = admin_stats["week"]
            if fallback_lang == "ru":
                return (
                    f"По организации: сегодня {today_org['total']} звонков, ср.дл {today_org['avg_duration']:.2f} мин; "
                    f"за 7д {week['total']} звонков, ср.дл {week['avg_duration']:.2f} мин. "
                    f"По вам лично: сегодня {t['total']}, вчера {y['total']}."
                )
            return (
                f"Org: today {today_org['total']} calls avg {today_org['avg_duration']:.2f}m; "
                f"7d {week['total']} calls avg {week['avg_duration']:.2f}m. "
                f"You: today {t['total']}, yesterday {y['total']}."
            )
        if fallback_lang == "ru":
            return (
                f"Сегодня звонков: {t['total']}, ср. длительность {t['avg_duration']:.2f} мин. "
                f"Вчера: {y['total']}, ср. длительность {y['avg_duration']:.2f} мин."
            )
        return (
            f"Today calls: {t['total']}, avg duration {t['avg_duration']:.2f} min. "
            f"Yesterday: {y['total']}, avg duration {y['avg_duration']:.2f} min."
        )
    prompt = (
        "Ты телеграм-бот аналитик. Кратко и живо отвечай на том же языке, что запрос (2-4 предложения). "
        "Опирайся только на переданные метрики, не выдумывай числа. "
        "Если пользователь админ, отвечай прежде всего по орг-метрикам (org_stats), а личные метрики (user_stats) используй только если явно спрашивают про себя. "
        "Если данных мало — скажи об этом и уточни, что есть. "
        "Если вопрос выходит за рамки доступных данных (звонки/метрики звонков) — честно скажи, что у тебя есть только данные по звонкам, и ответь по ним, если это уместно."
    )
    content = {
        "user": user.representative_name,
        "question": question,
        "user_stats": user_stats,
        "org_stats": admin_stats,
    }
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": str(content)},
            ],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        t = user_stats["today"]
        y = user_stats["yesterday"]
        if fallback_lang == "ru":
            return (
                f"Сегодня звонков: {t['total']}, ср. длительность {t['avg_duration']:.2f} мин. "
                f"Вчера: {y['total']}, ср. длительность {y['avg_duration']:.2f} мин."
            )
        return (
            f"Today calls: {t['total']}, avg duration {t['avg_duration']:.2f} min. "
            f"Yesterday: {y['total']}, avg duration {y['avg_duration']:.2f} min."
        )


def log_admin_action(admin_chat_id: int, action: str, details: Optional[dict] = None) -> None:
    with get_session() as session:
        admin_user = session.scalar(
            select(User).where(User.telegram_chat_id == str(admin_chat_id))
        )
        log = AdminLog(
            admin_user_id=admin_user.id if admin_user else None,
            action=action,
            details=details or {},
        )
        session.add(log)


def sanitize_phone(value: str) -> Optional[str]:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


def phone_candidates(digits: str) -> List[str]:
    """Return possible variants (with/without country code) for matching."""
    if not digits:
        return []
    candidates = set()
    candidates.add(digits)
    # UA-style: 380XXXXXXXXX vs 0XXXXXXXXX
    if digits.startswith("380") and len(digits) == 12:
        local = "0" + digits[3:]
        candidates.update({local, "+380" + digits[3:], "+" + digits})
    if digits.startswith("0") and len(digits) == 10:
        intl = "380" + digits[1:]
        candidates.update({intl, "+380" + digits[1:], digits[1:]})
    return list(candidates)


def canonical_phone(digits: str) -> str:
    """Choose canonical storage form (prefer intl 380...)."""
    if digits.startswith("380") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "380" + digits[1:]
    return digits


def ensure_admin_seed(chat_id: int, full_name: str) -> None:
    if str(chat_id) not in settings.telegram_admin_ids:
        return
    with get_session() as session:
        user = session.scalar(
            select(User).where(User.telegram_chat_id == str(chat_id))
        )
        if not user:
            user = User(
                representative_name=full_name or f"admin_{chat_id}",
                phone=str(chat_id),
                telegram_chat_id=str(chat_id),
                is_admin=True,
            )
            session.add(user)
        elif not user.is_admin:
            user.is_admin = True
        session.add(
            AdminLog(
                admin_user_id=user.id if user.id else None,
                action="ensure_admin_seed",
                details={"chat_id": chat_id},
            )
        )


def is_admin(chat_id: int) -> bool:
    if str(chat_id) in settings.telegram_admin_ids:
        return True
    with get_session() as session:
        return bool(
            session.scalar(
                select(User).where(
                    User.telegram_chat_id == str(chat_id), User.is_admin.is_(True)
                )
            )
        )


async def handle_start(message: Message) -> None:
    chat_id = message.chat.id
    full_name = message.from_user.full_name
    ensure_admin_seed(chat_id, full_name)
    with get_session() as session:
        user = session.scalar(
            select(User).where(User.telegram_chat_id == str(chat_id))
        )
    if user:
        role = "админ" if user.is_admin else "пользователь"
        kb = build_main_keyboard(user.is_admin)
        await message.answer(
            (
                f"Привет, {full_name}! Ты {role}.\n"
                f"С правами {role} можешь задавать любые вопросы про статистику звонков "
                f"или логику работы бота прямо текстом, я отвечу по данным."
            ),
            reply_markup=kb,
        )
        return
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Привет! Нажми кнопку, чтобы отправить свой номер телефона и привязать аккаунт.",
        reply_markup=kb,
    )


async def handle_help(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "/last [count] [department] — последние записи\n"
        "/settings — настройки (для админов)\n"
        "Бот присылает ежедневные дайджесты после привязки телефона."
    )


def get_last(count: int = 5, department: Optional[str] = None) -> list[Analysis]:
    with get_session() as session:
        stmt = select(Analysis).order_by(Analysis.call_date.desc())
        if department:
            stmt = stmt.where(Analysis.department == department)
        stmt = stmt.limit(min(count, 20))
        return session.scalars(stmt).all()


def format_row(a: Analysis) -> str:
    title = a.title or a.summary or "(без заголовка)"
    if title and len(title) > 180:
        title = title[:177] + "..."
    dur = f"{a.duration_minutes:.2f} мин" if a.duration_minutes else "-"
    return (
        f"#{a.id} | {a.department or '-'} | {a.representative or '-'}\n"
        f"{title}\n"
        f"Дата: {a.call_date} | Длительность: {dur}\n"
        f"Status: {a.status}\n"
        f"Audio: {a.external_url or '—'}"
    )


async def handle_last(message: Message) -> None:
    parts = message.text.split(maxsplit=2)
    count = 5
    department = None
    if len(parts) >= 2:
        try:
            count = int(parts[1])
        except ValueError:
            department = parts[1]
    if len(parts) == 3:
        department = parts[2]
    rows = get_last(count=count, department=department)
    if not rows:
        await message.answer("Нет данных.")
        return
    remember_context(message.chat.id, {"last_call_id": rows[0].id})
    chunks = []
    for row in rows:
        chunks.append(format_row(row))
    text = "\n\n".join(chunks)
    if len(text) > 3900:
        text = text[:3900] + "\n...\n(обрезано)"
    await message.answer(text)


def build_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Мапінг співробітників", callback_data="cfg_users")],
            [InlineKeyboardButton(text="Додати адміністраторів", callback_data="cfg_admins")],
            [InlineKeyboardButton(text="Список адміністраторів", callback_data="cfg_admin_list")],
        ]
    )


def build_main_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = []
    info_btn = InlineKeyboardButton(text="ℹ️ Інфо", callback_data="info")
    request_btn = InlineKeyboardButton(text="🛠️ Запит на доопрацювання", callback_data="request_feature")
    if is_admin:
        gear_btn = InlineKeyboardButton(text="⚙️", callback_data="cfg_menu")
        buttons.append([gear_btn, info_btn])
    else:
        buttons.append([info_btn])
    buttons.append([request_btn])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def handle_settings(message: Message) -> None:
    if not is_admin(message.chat.id):
        await message.answer("Нет доступа. Обратитесь к администратору.")
        return
    await message.answer("Налаштування:", reply_markup=build_settings_keyboard())


async def handle_callback(query: CallbackQuery) -> None:
    data = query.data or ""
    chat_id = query.message.chat.id
    if data == "cfg_menu":
        await query.message.answer("Налаштування:", reply_markup=build_settings_keyboard())
        await query.answer()
        return
    if data == "info":
        is_adm = is_admin(chat_id)
        if is_adm:
            text_lines = [
                "ℹ️ Бот для адміністраторів і менеджерів. Підключений до чат-GPT: можна писати питання вільним текстом, відповідає за даними дзвінків.",
                "",
                "Адмін:",
                "- Щоденний звіт о 19:00 (Europe/Kyiv): унікальні дзвінки >30с, вперше, всього унікальні, середня оцінка (🟢 якщо ≥4, 🔴 якщо <4), якщо були скарги — рядок '🔴 Скарги: N'.",
                "- Персональні нотифікації менеджерам після аналізу: телефон/компанія, кваліфікація, оцінка (🟢 ≥4, 🔴 ≤3), привітання, уточнюючі питання, скарги/побажання (лише якщо є), спроба закриття, обробка заперечень (лише якщо є), summary, аудіо.",
                "- Можна ставити питання про статистику або логіку бота вільним текстом.",
            ]
        else:
            text_lines = [
                "ℹ️ Бот для адміністраторів і менеджерів. Підключений до чат-GPT: можна писати питання вільним текстом, відповідає за даними дзвінків.",
                "",
                "Менеджер:",
                "- Після аналізу кожного дзвінка приходить повідомлення: телефон/компанія, кваліфікація, оцінка (🟢 якщо ≥4, 🔴 якщо ≤3), привітання, уточнюючі питання, скарги/побажання (якщо є), спроба закриття, обробка заперечень (якщо є), summary, аудіо.",
                "- Можна ставити питання про свою статистику вільним текстом.",
            ]
        await query.message.answer("\n".join(text_lines))
        await query.answer()
        return
    if data == "request_feature":
        await query.message.answer(
            "Напишіть, будь ласка, що потрібно змінити чи додати в боті. Я передам запит."
        )
        await query.answer()
        return
    if data == "last5_me":
        with get_session() as session:
            user = session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))
            rep_filter = user.representative_name if user else None
            rows = (
                session.scalars(
                    select(Analysis)
                    .where(Analysis.call_date.is_not(None))
                    .where(Analysis.representative == rep_filter if rep_filter else True)
                    .order_by(Analysis.call_date.desc())
                    .limit(5)
                ).all()
            )
        if not rows:
            await query.message.answer("Нет данных.")
            await query.answer()
            return
        chunks = []
        for r in rows:
            client = (r.metadata_json or {}).get("Customer") or "-"
            summary = r.summary or r.title or "-"
            link = r.external_url or "-"
            chunks.append(f"{client}\nSummary: {summary}\n{link}")
        text = "\n\n".join(chunks)
        await query.message.answer(text)
        await query.answer()
        return
    if data == "cfg_users":
        with get_session() as session:
            reps = [
                r
                for r in list_representatives(session)
                if r and r.lower() not in {"s", "f"} and len(r) > 1
            ]
        if not reps:
            await query.message.answer("Нет сотрудников в данных.")
            await query.answer()
            return
        buttons = [
            InlineKeyboardButton(text=rep, callback_data=f"map_rep:{rep}") for rep in reps[:50]
        ]
        # chunk rows
        rows: List[List[InlineKeyboardButton]] = []
        for b in buttons:
            if not rows or len(rows[-1]) >= 1:
                rows.append([])
            rows[-1].append(b)
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_map")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await query.message.answer("Выберите сотрудника для маппинга:", reply_markup=kb)
        await query.answer()
        return
    if data.startswith("map_rep:"):
        rep = data.split("map_rep:", 1)[1]
        pending_actions[chat_id] = {"type": "map_rep", "rep": rep}
        await query.message.answer(
            f"Введіть номер телефону для співробітника: {rep}\nФормат: 380XXXXXXXXX (12 цифр) або 0XXXXXXXXX.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_map")]]
            ),
        )
        await query.answer()
        return
    if data == "cfg_admins":
        pending_actions[chat_id] = {"type": "add_admin_phone"}
        await query.message.answer(
            "Введіть телефон користувача, щоб додати права адміністратора.\nФормат: 380XXXXXXXXX (12 цифр) або 0XXXXXXXXX.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel_admin")]]
            ),
        )
        await query.answer()
        return
    if data == "cancel_map":
        pending_actions.pop(chat_id, None)
        await query.message.answer("Дію скасовано.", reply_markup=build_settings_keyboard())
        await query.answer()
        return
    if data == "cancel_admin":
        pending_actions.pop(chat_id, None)
        await query.message.answer("Дію скасовано.", reply_markup=build_settings_keyboard())
        await query.answer()
        return
    if data == "cfg_admin_list":
        with get_session() as session:
            admins = session.scalars(
                select(User).where(User.is_admin.is_(True))
            ).all()
        if not admins:
            await query.message.answer("Администраторов нет.")
            await query.answer()
            return
        lines = []
        for a in admins:
            lines.append(f"{a.representative_name or '-'} | {a.phone or '-'} | chat_id={a.telegram_chat_id or '-'}")
        await query.message.answer("\n".join(lines))
        await query.answer()
        return
    if data.startswith("err:"):
        analysis_id = int(data.split("err:", 1)[1])
        await send_error_details(chat_id, analysis_id, query.message)
        await query.answer()
        return
    await query.answer()


async def send_error_details(chat_id: int, analysis_id: int, origin_msg: Optional[Message]) -> None:
    with get_session() as session:
        a = session.scalar(select(Analysis).where(Analysis.id == analysis_id))
    if not a:
        await origin_msg.answer("Запись не найдена.")
        return
    complaints = (a.result_json or {}).get("Скарги клієнта") or (a.result_json or {}).get(
        "Client_Complaints"
    )
    has_complaint = complaints and str(complaints).lower() not in {"no", "false", "0"}
    summary = a.summary or a.title or "(нет summary)"
    call_filter = (a.result_json or {}).get("Call_Filter")
    def _score_value(val: Any) -> Optional[int]:
        if val is None:
            return None
        try:
            return int(float(val))
        except Exception:
            return None

    greeting = (a.result_json or {}).get("Привітання")
    clarifying = (a.result_json or {}).get("Уточнюючі питання") or (a.result_json or {}).get("Уточнюючі питання?")
    complaints = (a.result_json or {}).get("Скарги клієнта")
    wishes = (a.result_json or {}).get("Побажання клієнта")
    closing_attempt = (a.result_json or {}).get("Спроба закриття замовлення")
    objections = (a.result_json or {}).get("Обробка заперечень")
    knowledge = (a.result_json or {}).get("Рівень знань") or (a.result_json or {}).get("Уровень знаний")
    unknown_details = (a.result_json or {}).get("Що саме не знав менеджер") or (a.result_json or {}).get("Что именно не знал менеджер")
    greeting = (a.result_json or {}).get("Привітання")
    questions = (a.result_json or {}).get("Уточнюючі питання")
    score = get_score(a)
    text_lines = [
        f"#{a.id} | {a.representative or '-'} | {a.department or '-'}",
        f"Оценка: {score if score is not None else '—'} | Call_Filter: {call_filter or '—'}",
        f"Жалобы: {'есть' if has_complaint else 'нет'}",
        f"Уточнюючі питання: {'есть' if questions else 'нет'}",
        f"Привітання: {'есть' if greeting else 'нет'}",
        f"Длительность: {a.duration_minutes or '-'} мин",
        f"Аудио: {a.external_url or '—'}",
        f"Summary: {summary}",
    ]
    text = "\n".join(text_lines)
    try:
        await origin_msg.answer_audio(audio=a.external_url, caption=text)
    except Exception:
        await origin_msg.answer(text)


async def handle_text(message: Message) -> None:
    chat_id = message.chat.id
    pending = pending_actions.get(chat_id)
    if pending and pending.get("type") in {"map_rep", "add_admin_phone"}:
        digits = sanitize_phone(message.text or "")
        if not digits:
            await message.answer("Нужен номер телефона (цифрами).")
            return
        phone = canonical_phone(digits)
        if pending["type"] == "map_rep":
            rep = pending["rep"]
            with get_session() as session:
                user = session.scalar(
                    select(User).where(User.representative_name == rep)
                )
                existing_phone_user = session.scalar(select(User).where(User.phone == phone))
                if user and existing_phone_user and existing_phone_user.id != user.id:
                    # Переназначаем номер: снимаем с прошлого владельца
                    existing_phone_user.phone = None
                    existing_phone_user.telegram_chat_id = None
                if user:
                    user.phone = phone
                elif existing_phone_user:
                    # Переименовываем существующую запись с номером
                    existing_phone_user.representative_name = rep
                else:
                    user = User(
                        representative_name=rep,
                        phone=phone,
                        is_admin=False,
                    )
                    session.add(user)
            log_admin_action(chat_id, "map_rep", {"rep": rep, "phone": phone})
            await message.answer(f"Сохранено. {rep} → {phone}. Попросите сотрудника написать /start для привязки чата.")
            pending_actions.pop(chat_id, None)
            return
        if pending["type"] == "add_admin_phone":
            with get_session() as session:
                user = session.scalar(select(User).where(User.phone == phone))
                if not user:
                    user = User(
                        representative_name=phone,
                        phone=phone,
                        is_admin=True,
                    )
                    session.add(user)
                else:
                    user.is_admin = True
            log_admin_action(chat_id, "add_admin", {"phone": phone})
            await message.answer("Админ добавлен/обновлен. Если чат ещё не привязан — попросите пользователя нажать /start.")
            pending_actions.pop(chat_id, None)
            return
    # чат-ответ на произвольный текст
    with get_session() as session:
        user = session.scalar(select(User).where(User.telegram_chat_id == str(chat_id)))
    if not user:
        await message.answer("Привяжи номер через /start, чтобы я смог отвечать по данным.")
        return
    # быстрые ответы на конкретные запросы (первый звонок сегодня)
    if message.text:
        q = message.text.lower()
        lang_cyr = bool(re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""))
        call_kw = has_call_keyword(q)
        if is_greeting_query(q):
            ans = (
                "Привет! Я бот по статистике звонков. "
                "Спроси, например: «сколько звонков сегодня?» или «последний звонок»."
                if lang_cyr
                else "Hi! I can help with call stats. Ask e.g. “calls today?” or “last call”."
            )
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans)
            return
        today_total_asked = "сколь" in q and call_kw and ("сегод" in q or "today" in q)
        min_yday_asked = (
            call_kw
            and ("вчера" in q or "yesterday" in q)
            and ("мин" in q or "наимен" in q or "least" in q or "меньш" in q or "lowest" in q)
        )
        if today_total_asked and min_yday_asked:
            total_today = count_calls_for_day(0)
            tz = pytz.timezone(settings.report_timezone)
            now = datetime.now(tz)
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            end = start + timedelta(days=1)
            start_naive, end_naive = start.replace(tzinfo=None), end.replace(tzinfo=None)
            with get_session() as session:
                rows = session.execute(
                    select(Analysis.representative, func.count().label("cnt"))
                    .where(
                        Analysis.call_date.is_not(None),
                        Analysis.call_date >= start_naive,
                        Analysis.call_date < end_naive,
                        Analysis.representative.is_not(None),
                    )
                    .group_by(Analysis.representative)
                    .order_by(func.count().asc())
                ).all()
            lines = [f"Сегодня звонков: {total_today}."]
            if rows:
                min_count = rows[0].cnt
                last_place = [r.representative for r in rows if r.cnt == min_count and r.representative]
                names = ", ".join(last_place)
                lines.append(f"Минимум звонков вчера у: {names} (звонков: {min_count}).")
                meta = {"combined": {"total_today": total_today, "min_yesterday": {"reps": last_place, "count": min_count}}}
            else:
                lines.append("Вчера звонков в базе нет.")
                meta = {"combined": {"total_today": total_today, "min_yesterday": None}}
            ans = "\n".join(lines)
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta=meta)
            return
        # сколько звонков сегодня без приветствия
        if (
            "прив" in q
            and ("без" in q or "нет" in q or "відсут" in q)
            and ("сегод" in q or "today" in q)
        ):
            total, without = count_calls_without_greeting(0)
            if lang_cyr:
                ans = f"Сегодня было {total} звонков, без приветствия: {without}."
            else:
                ans = f"Today there were {total} calls, without greeting: {without}."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"total_today": total, "without_greeting": without})
            return
        # исходящие/входящие за сегодня
        if ("исход" in q or "outgoing" in q) and ("сегод" in q or "today" in q):
            total_out = count_calls_for_day_direction(0, direction="out")
            ans = f"Сегодня исходящих звонков: {total_out}."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"direction": "out", "total": total_out})
            return
        if ("вход" in q or "incoming" in q) and ("сегод" in q or "today" in q):
            total_in = count_calls_for_day_direction(0, direction="in")
            ans = f"Сегодня входящих звонков: {total_in}."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"direction": "in", "total": total_in})
            return
        # пример отчета по звонку для менеджера
        if ("пример" in q or "sample" in q) and (("отчет" in q or "отчёт" in q or "report" in q) and call_kw):
            lc = latest_call()
            if not lc:
                ans = "Пока нет звонков, чтобы показать пример отчёта."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans, is_error=True, reason="no_calls_for_sample")
                return
            client = (lc.metadata_json or {}).get("Customer") or "-"
            summary = lc.summary or lc.title or "-"
            score = get_score(lc)
            score_text = f"{score:.2f}" if score is not None else "—"
            ans_lines = [
                "Пример отчёта по последнему звонку:",
                f"Дата/время: {lc.call_date}",
                f"Менеджер: {lc.representative or '-'} ({lc.department or '-'})",
                f"Клиент: {client}",
                f"Длительность: {lc.duration_minutes or '-'} мин",
                f"Оценка: {score_text}",
                f"Summary: {summary}",
                f"Аудио: {lc.external_url or '—'}",
            ]
            ans = "\n".join(ans_lines)
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"sample_call_id": lc.id})
            return
        # последн. звонок
        if ("последн" in q or "last" in q) and call_kw:
            lc = latest_call()
            if lc:
                ans = f"Последний звонок был {lc.call_date}."
                await message.answer(ans)
                remember_context(chat_id, {"last_call_id": lc.id})
                log_interaction(chat_id, user, message.text or "", ans, meta={"last_call_id": lc.id})
            else:
                ans = "Нет данных о звонках."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans, is_error=True, reason="no_calls")
            return
        # лидер по количеству звонков сегодня
        if (
            call_kw
            and ("сегод" in q or "today" in q)
            and (
                ("перв" in q and "мест" in q)
                or "топ" in q
                or "лидер" in q
                or "лучший" in q
                or ("больш" in q and ("у кого" in q or "кто" in q))
                or "most" in q
                or "max" in q
            )
        ):
            rows = manager_call_counts_for_day(0, order="desc")
            if not rows:
                ans = "Сегодня ещё нет звонков в базе."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans)
                return
            top_count = rows[0].cnt
            leaders = [r.rep for r in rows if r.cnt == top_count and r.rep]
            leaders_str = ", ".join(leaders)
            ans = f"Лидируют по звонкам сегодня: {leaders_str} (звонков: {top_count})."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"top_today": {"leaders": leaders, "count": top_count}})
            return
        # у кого меньше всего звонков за вчера
        if (
            call_kw
            and ("вчера" in q or "yesterday" in q)
            and ("мин" in q or "наимен" in q or "least" in q or "меньш" in q or "lowest" in q)
        ):
            rows = manager_call_counts_for_day(1, order="asc")
            if not rows:
                ans = "Вчера звонков в базе нет."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans)
                return
            min_count = rows[0].cnt
            last_place = [r.rep for r in rows if r.cnt == min_count and r.rep]
            names = ", ".join(last_place)
            ans = f"Минимум звонков вчера у: {names} (звонков: {min_count})."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, meta={"min_yesterday": {"reps": last_place, "count": min_count}})
            return
        # кто его принял? — используем контекст последнего звонка
        if ("кто" in q or "who" in q) and ("принял" in q or "handled" in q or "ответил" in q or "answered" in q or "rep" in q or "менедж" in q):
            last_id = get_last_context(chat_id, "last_call_id")
            a = None
            used_fallback = False
            if last_id:
                with get_session() as session:
                    a = session.scalar(select(Analysis).where(Analysis.id == last_id))
            if not a:
                a = latest_call()
                used_fallback = True
            if a:
                rep = a.representative or "-"
                if used_fallback:
                    ans = (
                        f"По последнему звонку принял(а): {rep}. Если нужен другой — напиши «последний звонок»."
                        if lang_cyr
                        else f"Latest call was handled by: {rep}. If you mean another call, ask for “last call”."
                    )
                    remember_context(chat_id, {"last_call_id": a.id})
                    log_interaction(chat_id, user, message.text or "", ans, meta={"last_call_id": a.id, "fallback_context": True})
                else:
                    ans = f"Звонок принял(а): {rep}."
                    log_interaction(chat_id, user, message.text or "", ans, meta={"last_call_id": last_id})
                await message.answer(ans)
                return
            ans = "Нет данных о звонках." if lang_cyr else "No call data yet."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, is_error=True, reason="no_calls")
            return
        # длительность последнего звонка из контекста
        if "длит" in q or "duration" in q or "сколько длился" in q:
            last_id = get_last_context(chat_id, "last_call_id")
            a = None
            used_fallback = False
            if last_id:
                with get_session() as session:
                    a = session.scalar(select(Analysis).where(Analysis.id == last_id))
            if not a:
                a = latest_call()
                used_fallback = True
            if a and a.duration_minutes is not None:
                if used_fallback:
                    ans = (
                        f"Длительность последнего звонка: {a.duration_minutes:.2f} минут. "
                        "Если нужен другой — напиши «последний звонок»."
                        if lang_cyr
                        else f"Latest call duration: {a.duration_minutes:.2f} minutes. "
                             "If you mean another call, ask for “last call”."
                    )
                    remember_context(chat_id, {"last_call_id": a.id})
                    log_interaction(chat_id, user, message.text or "", ans, meta={"last_call_id": a.id, "fallback_context": True})
                else:
                    ans = f"Длительность звонка: {a.duration_minutes:.2f} минут."
                    log_interaction(chat_id, user, message.text or "", ans, meta={"last_call_id": last_id})
                await message.answer(ans)
                return
            if a and a.duration_minutes is None:
                if used_fallback:
                    ans = (
                        "У последнего звонка нет длительности. Уточни дату/ID или запроси /last 1."
                        if lang_cyr
                        else "Latest call has no duration. Specify date/ID or request /last 1."
                    )
                else:
                    ans = (
                        "У этого звонка нет длительности. Уточни дату/ID или запроси /last 1."
                        if lang_cyr
                        else "This call has no duration. Specify date/ID or request /last 1."
                    )
                await message.answer(ans)
                log_interaction(
                    chat_id,
                    user,
                    message.text or "",
                    ans,
                    is_error=True,
                    reason="missing_call_duration",
                    meta={"last_call_id": a.id, "fallback_context": used_fallback},
                )
                return
            ans = "Нет данных о звонках." if lang_cyr else "No call data yet."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans, is_error=True, reason="no_calls")
            return
        if ("перв" in q or "first" in q) and call_kw and ("сегод" in q or "today" in q):
            first = first_call_today()
            if first:
                client = (first.metadata_json or {}).get("Customer") or "-"
                summary = first.summary or first.title or "-"
                url = first.external_url or "-"
                ans = (
                    f"Самый ранний звонок сегодня:\n"
                    f"{first.call_date} | {first.representative or '-'}\n"
                    f"Клиент: {client}\n"
                    f"Summary: {summary}\n"
                    f"Ссылка: {url}"
                )
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans)
                return
            else:
                ans = "Сегодня ещё нет звонков в базе."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans)
                return
        # быстрый ответ на общее количество звонков сегодня
        if "сколь" in q and call_kw and ("сегод" in q or "today" in q):
            total_today = count_calls_for_day(0)
            if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""):
                ans = f"Сегодня у компании звонков: {total_today}."
            else:
                ans = f"Today the company has {total_today} calls."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans)
            return
        # быстрый ответ на количество звонков вчера
        if ("сколь" in q and call_kw and ("вчера" in q or "yesterday" in q)):
            total_yesterday = count_calls_for_day(1)
            if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""):
                ans = f"Вчера у компании звонков: {total_yesterday}."
            else:
                ans = f"Yesterday the company had {total_yesterday} calls."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans)
            return
        # быстрый ответ на количество звонков позавчера
        if ("сколь" in q and call_kw and ("позавчера" in q or "day before yesterday" in q)):
            total_prev = count_calls_for_day(2)
            if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""):
                ans = f"Позавчера у компании звонков: {total_prev}."
            else:
                ans = f"The day before yesterday the company had {total_prev} calls."
            await message.answer(ans)
            log_interaction(chat_id, user, message.text or "", ans)
            return
        # быстрый ответ на количество звонков за последние N дней
        match_days = re.search(r"(?:последн\w*|last)\s+(\d{1,3})\s+(?:дн|day|days)", q)
        if match_days and call_kw:
            num = None
            for g in match_days.groups():
                if g and g.isdigit():
                    num = int(g)
                    break
            if num:
                # если в вопросе упомянуты "по дням" или "какой день" — выведем разбивку
                if "по дн" in q or "какой день" in q or "в какой день" in q or "by day" in q:
                    rows = daily_counts_last_days(num)
                    if not rows:
                        msg = "За этот период нет звонков." if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or "") else "No calls in this period."
                        await message.answer(msg)
                        log_interaction(chat_id, user, message.text or "", msg, is_error=True, reason="no_calls_period", meta={"days": num})
                        return
                    lines = [f"{r['date']}: {r['calls']}" for r in rows]
                    msg = "\n".join(lines)
                    await message.answer(msg)
                    log_interaction(chat_id, user, message.text or "", msg, meta={"days": num})
                    return
                total = count_calls_last_days(num)
                if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""):
                    ans = f"За последние {num} дней звонков: {total}."
                else:
                    ans = f"Calls in the last {num} days: {total}."
                await message.answer(ans)
                log_interaction(chat_id, user, message.text or "", ans, meta={"days": num})
                return
        week_mentioned = ("недел" in q) or ("week" in q) or re.search(r"7\s*дн", q)
        if week_mentioned and ("уникаль" in q or "unique" in q):
            msg = "Цей показник більше не відображається." if lang_cyr else "This metric is no longer shown."
            await message.answer(msg)
            log_interaction(chat_id, user, message.text or "", msg, meta={"kind": "unique_disabled"})
            return
    # tool-calling режим: сначала планировщик, потом инструмент, потом композитор
    planner_cmd = await run_planner(message.text or "", admin_mode=is_admin(chat_id))
    if planner_cmd and planner_cmd.get("action"):
        tool_result = execute_tool(planner_cmd)
        reply = await run_composer(message.text or "", tool_result, lang_ru=bool(re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or "")))
        await message.answer(reply)
        log_interaction(
            chat_id,
            user,
            message.text or "",
            reply,
            is_error=not bool(tool_result.get("result")),
            reason="empty_tool_result" if not bool(tool_result.get("result")) else None,
            meta={"tool": planner_cmd},
        )
        return

    try:
        reply = await build_chat_reply(user, message.text, admin_mode=is_admin(chat_id))
        await message.answer(reply)
        log_interaction(chat_id, user, message.text or "", reply)
    except Exception as exc:
        if re.search("[А-Яа-яЁёІіЇїЄєҐґ]", message.text or ""):
            ans = "Пока не могу обработать этот запрос. Обратитесь к администратору, если нужна такая функция."
        else:
            ans = "I can't handle this request yet. Please ask an administrator if you need this feature."
        await message.answer(ans)
        log_interaction(chat_id, user, message.text or "", ans, is_error=True, reason=f"exception: {exc}")


async def handle_contact(message: Message) -> None:
    chat_id = message.chat.id
    contact = message.contact
    digits = sanitize_phone(contact.phone_number if contact else "")
    if not digits:
        await message.answer("Не удалось прочитать номер телефона.")
        return
    candidates = phone_candidates(digits)
    with get_session() as session:
        user = session.scalar(select(User).where(User.phone.in_(candidates)))
        if not user:
            await message.answer("Твой номер не найден в списке сотрудников. Обратись к администратору.")
            return
        user.telegram_chat_id = str(chat_id)
        if user.phone not in candidates:
            user.phone = canonical_phone(digits)
        role = "админ" if user.is_admin else "пользователь"
        is_admin = user.is_admin
    kb = build_main_keyboard(is_admin)
    await message.answer(
        f"Телефон привязан. Роль: {role}. Дайджесты будут приходить ежедневно.",
        reply_markup=kb,
    )
    pending_actions.pop(chat_id, None)


def format_digest(name: str, phone: str, metrics: Dict[str, Any]) -> str:
    calls_today = metrics["calls_today"]
    avg_dur = metrics["avg_duration"]
    avg_30 = metrics["avg_per_day_30d"]
    delta = calls_today - avg_30
    err_count = len(metrics["errors"])
    return (
        f"📊 Отчёт за сегодня\n"
        f"Сотрудник: {name} ({phone})\n"
        f"Звонков: {calls_today} (среднее 30д: {avg_30:.2f}, Δ={delta:+.2f})\n"
        f"Ср. длительность: {avg_dur:.2f} мин\n"
        f"Проблемные (<4): {err_count}"
    )


def build_errors_keyboard(errors: List[Analysis]) -> Optional[InlineKeyboardMarkup]:
    if not errors:
        return None
    rows = []
    for a in errors[:10]:
        title = a.title or a.summary or f"#{a.id}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=title[:60],
                    callback_data=f"err:{a.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_daily_digests(bot: Bot) -> None:
    if settings.disable_daily_digests:
        logging.getLogger(__name__).info("Daily digests are disabled via env")
        return
    with get_session() as session:
        users = session.scalars(
            select(User).where(User.telegram_chat_id.is_not(None))
        ).all()
        for u in users:
            metrics = daily_metrics(session, u.representative_name, settings.report_timezone)
            text = format_digest(u.representative_name, u.phone, metrics)
            kb = build_errors_keyboard(metrics["errors"])
            try:
                await bot.send_message(chat_id=u.telegram_chat_id, text=text, reply_markup=kb)
            except Exception:
                continue


async def digest_scheduler(bot: Bot) -> None:
    if settings.disable_daily_digests:
        logging.getLogger(__name__).info("Daily digest scheduler disabled via env")
        return
    tz = pytz.timezone(settings.report_timezone)
    while True:
        now = datetime.now(tz)
        # личные дайджесты менеджерам — оставляем на ночь (23:55)
        target = now.replace(hour=23, minute=55, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_daily_digests(bot)
        except Exception:
            continue


async def weekly_scheduler(bot: Bot) -> None:
    if not settings.weekly_report_chat_id:
        return
    tz = pytz.timezone(settings.report_timezone)
    while True:
        now = datetime.now(tz)
        # next Monday 09:00
        days_ahead = (7 - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
            days=days_ahead
        )
        await asyncio.sleep((target - now).total_seconds())
        try:
            with get_session() as session:
                metrics = weekly_org_metrics(session, settings.report_timezone)
            text_lines = [
                "📈 Еженедельный отчёт",
                f"Звонков за 7д: {metrics['total']}",
                f"Ср. длительность: {metrics['avg_duration']:.2f} мин",
                f"Проблемные (<4): {len(metrics['errors'])}",
                f"Irrelevant: {metrics['irrelevant']}",
            ]
            if metrics["top_reps"]:
                top = ", ".join(f"{name}: {cnt}" for name, cnt in metrics["top_reps"])
                text_lines.append(f"Топ по звонкам: {top}")
            text = "\n".join(text_lines)
            await bot.send_message(chat_id=settings.weekly_report_chat_id, text=text)
        except Exception:
            continue


async def notify_managers_for_analysis(bot: Bot, analysis_id: int) -> None:
    with get_session() as session:
        a = session.scalar(select(Analysis).where(Analysis.id == analysis_id))
        if not a:
            return
        uniqueid = (a.metadata_json or {}).get("uniqueid")
        ringostat = session.scalar(
            select(Call)
            .where(
                (Call.eniq_analysis_id == a.id)
                | (Call.call_id == (uniqueid or ""))
            )
            .order_by(Call.updated_at.desc())
        )
        if not ringostat or not (ringostat.rs_is_unique and ringostat.rs_is_unique_targeted):
            return
        users = session.scalars(
            select(User).where(
                User.representative_name == a.representative,
                User.telegram_chat_id.is_not(None),
                User.is_admin.is_(False),
            )
        ).all()
    if not users:
        return
    client_name = (a.metadata_json or {}).get("Customer") or "-"
    company = (a.metadata_json or {}).get("Company") or (a.metadata_json or {}).get("Organization") or "-"
    # попытка достать телефон из metadata/result
    client_phone = "-"
    for src in (a.metadata_json or {}), (a.result_json or {}):
        for k, v in (src or {}).items():
            if "phone" in str(k).lower() and v:
                client_phone = normalize_phone(v) or str(v)
                break
        if client_phone != "-":
            break
    if client_phone == "-" and ringostat and ringostat.rs_client_number:
        client_phone = ringostat.rs_client_number
    rep_name = a.representative or (ringostat.rs_manager_name if ringostat else None) or "-"
    dept_name = a.department or (ringostat.rs_call_scheme if ringostat else None) or "-"
    call_time = a.call_date or (ringostat.rs_start_time if ringostat else None)
    duration_minutes = a.duration_minutes
    if duration_minutes is None and ringostat:
        duration_minutes = call_service.duration_minutes_value(ringostat)
    record_url = a.external_url or (ringostat.rs_record_url if ringostat else None)
    msg_lines = [
        f"📞 Новый анализ звонка #{a.id}",
        f"🗓 Дата/час: {call_time or '—'}",
        f"👤 Представитель: {rep_name} | Отдел: {dept_name}",
        f"👥 Клиент: {client_name} | Компания: {company}",
        f"📱 Телефон клиента: {client_phone}",
    ]
    if ringostat:
        direction = _format_direction(ringostat.rs_direction or ringostat.rs_call_type)
        status = ringostat.rs_status or "—"
        msg_lines.append(f"📡 Направление: {direction} | Статус: {status}")
        msg_lines.append(f"⏱ Длительность: {_format_duration(duration_minutes)}")
        if ringostat.rs_call_scheme:
            msg_lines.append(f"🧭 Схема: {ringostat.rs_call_scheme}")
        if ringostat.rs_is_unique:
            msg_lines.append("🟣 Ringostat: унікальний дзвінок")
        if ringostat.rs_is_unique_targeted:
            msg_lines.append("🟣 Ringostat: унікальний цільовий дзвінок")
    if record_url:
        msg_lines.append(f"🎧 Запись: {record_url}")

    result_json = a.result_json if isinstance(a.result_json, dict) else {}
    metric_lines = [_format_metric_line(k, v) for k, v in _ordered_metric_items(result_json)]
    msg_lines.append("📊 Метрики ENIQ:")
    if metric_lines:
        msg_lines.extend(metric_lines)
    else:
        msg_lines.append("— Метрик нет.")
    text = "\n".join(msg_lines)
    for u in users:
        try:
            with get_session() as session:
                already_sent = session.scalar(
                    select(InteractionLog.id).where(
                        InteractionLog.chat_id == str(u.telegram_chat_id),
                        InteractionLog.question == f"[auto] new_analysis {a.id}",
                    )
                )
            if already_sent:
                continue
            await bot.send_message(chat_id=u.telegram_chat_id, text=text)
            log_interaction(int(u.telegram_chat_id), u, f"[auto] new_analysis {a.id}", text)
        except Exception:
            continue


async def ingest_scheduler(bot: Bot) -> None:
    while True:
        try:
            async with _ingest_lock:
                new_ids = await asyncio.to_thread(ingest_all)
            if new_ids:
                for aid in new_ids:
                    await notify_managers_for_analysis(bot, aid)
            # ringostat incremental
            try:
                await asyncio.to_thread(fetch_ringostat_calls)
            except Exception as exc:  # pragma: no cover
                logging.getLogger(__name__).warning("Ringostat fetch failed: %s", exc)
        except Exception:
            pass
        await asyncio.sleep(2 * 60)


async def admin_daily_scheduler(bot: Bot) -> None:
    tz = pytz.timezone(settings.report_timezone)
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=19, minute=1, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await send_admin_daily_report(bot)
        except Exception:
            continue


async def _main() -> None:
    ensure_schema()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.message.register(handle_start, Command("start"))
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_last, Command("last"))
    dp.message.register(handle_settings, Command("settings"))
    dp.callback_query.register(handle_callback, F.data)
    dp.message.register(handle_contact, F.contact)
    dp.message.register(handle_text, F.text)
    if not settings.disable_daily_digests:
        asyncio.create_task(digest_scheduler(bot))
    async def _webhook_task() -> None:
        try:
            await start_webhook_server()
        except Exception as exc:  # pragma: no cover - runtime safety
            logging.getLogger(__name__).exception("Webhook server failed: %s", exc)

    asyncio.create_task(_webhook_task())
    asyncio.create_task(weekly_scheduler(bot))
    asyncio.create_task(ingest_scheduler(bot))
    # Admin daily digest disabled per request
    # asyncio.create_task(admin_daily_scheduler(bot))
    asyncio.create_task(sheets_sync_scheduler())
    await dp.start_polling(bot)


def start_bot() -> None:
    asyncio.run(_main())
