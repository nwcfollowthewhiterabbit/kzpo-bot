# KZPO Report Bot — Overview

## Что это
Бот собирает данные о звонках из двух источников и складывает в одну базу, чтобы строить отчеты и уведомления:
- Ringostat (телефония + маркетинговые данные из вебхуков).
- ENIQ AI (анализ разговоров и метрики качества).

## Главная логика
Ключевая идея — одна строка в таблице `calls` = один звонок. Данные приходят поэтапно и дописываются в ту же строку:
1) Сначала приходит вебхук Ringostat `answer_call` — маркетинг до/в момент ответа. Он сохраняется в `calls` по `call_id`.
2) Затем приходит Ringostat `after_call` — фактические данные звонка (статус, длительность, уникальность и т.д.). Он также обновляет строку `calls` по `call_id`.
3) Позже ENIQ отправляет анализ. По `metadata_json.uniqueid` (он равен `call_id`) ENIQ-данные дописываются в эту же строку.
   Только после этого отправляется уведомление менеджеру в Telegram.

## Где что хранится
Все данные складываются в таблицу `calls`. В ней есть блоки:
- `rs_*` — данные Ringostat (after_call и answer_call).
- `eniq_*` — данные ENIQ.
- `rs_*_raw_json` и `eniq_*_json` — сырые payload/метаданные, чтобы ничего не терять.

Старые таблицы `analyses` и `ringostat_calls` пока оставлены для совместимости, но основная точка правды — `calls`.

## Основные эндпоинты вебхуков
- `POST /webhooks/ringostat/answer_call`
  - Маркетинг: `market_comp`, `target_post`, `sales_channel`, `sales_source/sales_cource`, `key_word`, `callback`, `last_page`, `user_agent`, `call_id`.
- `POST /webhooks/ringostat/after_call`
  - Телефония: `call_id`, `call_date`, `call_type`, `call_status`, `talk_time`, `manager`, `caller_id`, `uniq`, `uniq_targeted`, `call_scheme`, `user_agent` и т.д.

## Почему это надежно
- Все записи объединяются по `call_id`.
- Если часть данных пришла раньше/позже — запись будет обновлена при следующем событии.
- Сырые payload сохраняются, поэтому можно добавлять новые поля без потерь.

## Вызовы и нюансы
- `answer_call` может не содержать имя менеджера или номер клиента — они приходят в `after_call` или ENIQ.
- Если в ENIQ отсутствует `uniqueid`, запись не свяжется автоматически.
- Ringostat использует разные ключи (`sales_source` vs `sales_cource`, `key_word`), это учитывается при сохранении.
- Время звонка может отличаться (time skew); логируем это для диагностики.

## Что важно помнить
- Единая точка правды — `calls`.
- Для новых метрик проще всего:
  - либо добавить колонку `calls.rs_*` или `calls.eniq_*`,
  - либо временно читать из `rs_*_raw_json`.
- Когда нужно менять отчетность — используем `calls`, не `ringostat_calls`/`analyses`.

## Логика уведомлений менеджеру
Уведомление по звонку отправляется **только после прихода ENIQ анализа**.
Правила отображения ключевых метрик:
- Привітання: 5 → ✅, 3 → ⚠️ (порушено скрипт), 1 → ❌ (не дотримано скрипт).
- Рівень знань: 5 → не показываем; 3 → ⚠️ (підготовка не повністю задовільна); 1 → ❌ (недостатній рівень компетенцій).
  Если есть поле `Що саме не знав менеджер` и оно не `-`/`—`, показываем строкой.
- Скарги клієнта: если есть → показываем.
- Спроба закриття замовлення: 5 → не показываем; 1 → ❌ (недостатня активність при спробі закриття).

## Быстрая проверка данных
- Сырые данные `answer_call`:
  `SELECT call_id, rs_answer_raw_json FROM calls WHERE rs_answer_raw_json IS NOT NULL ORDER BY rs_answer_received_at DESC LIMIT 1;`
- Сырые данные `after_call`:
  `SELECT call_id, rs_after_raw_json FROM calls WHERE rs_after_raw_json IS NOT NULL ORDER BY rs_after_received_at DESC LIMIT 1;`
- ENIQ данные:
  `SELECT call_id, eniq_result_json FROM calls WHERE eniq_result_json IS NOT NULL ORDER BY eniq_call_date DESC LIMIT 1;`

## Файлы, где логика
- `eniq_agg/ringostat_webhook.py` — вебхуки Ringostat.
- `eniq_agg/ringostat_ingest.py` — запись Ringostat в `calls`.
- `eniq_agg/ingest.py` — запись ENIQ в `calls`.
- `eniq_agg/bot.py` — отчеты и уведомления.
- `eniq_agg/models.py` — структура таблиц.
