# KZPO Report Bot — текущее состояние (2026-01-09)

## Бэкапы
- Свежий полный бэкап: `kzpo-report-bot-backup-20260109-154844/` + архив `kzpo-report-bot-backup-20260109-154844.tar.gz` (код + дамп БД).
- Дамп Postgres: `kzpo-report-bot-backup-20260109-154844/eniq_dump_20260109-154844.sql`.
- Восстановление (пример):  
  `docker exec -i kzpo-report-bot-db-1 psql -U eniq -d eniq < eniq_dump_20260109-154844.sql`

## Архитектура и код
- Главная точка входа: `run.py` (`ingest`, `/last`, запуск бота).
- Бот и планировщики: `eniq_agg/bot.py` (рассылки, команды, чат-ответы, планировщик/композер с OpenAI).
- Интеграция eniq.ai: `eniq_agg/ingest.py`, `eniq_agg/client.py`, модели `Analysis`, `Call`.
- Ringostat вебхуки и API: `eniq_agg/ringostat_webhook.py`, `eniq_agg/ringostat_ingest.py`, `eniq_agg/ringostat_client.py`.
- Статистика/инструменты для бота: `eniq_agg/stats.py`, `eniq_agg/tools.py`.
- Конфиг: `eniq_agg/config.py` (env), `docker-compose.yml` (bot + Postgres).
- Общие утилиты/сервисный слой: `eniq_agg/utils/time_utils.py`, `eniq_agg/services/calls.py` (единые запросы по `calls`).

## База данных (Postgres, основная таблица `calls`)
- `calls` = единая строка на звонок. Секции колонок:
  - ENIQ: `eniq_*` (id, даты, dept/rep, prompt, summary/title, result/metadata/audio/transcription/tokens JSON).
  - Ringostat after_call: `rs_*` (project_id, direction/status, start/end, duration/talk/ringing, numbers/manager, record_url, cost, call_scheme, user_agent, is_unique, is_unique_targeted, match_method/time_diff/duration_diff, raw JSON + received_at).
  - Ringostat answer_call (маркетинг): `rs_answer_received_at`, `rs_market_comp`, `rs_target_post`, `rs_sales_channel`, `rs_sales_source`, `rs_call_type`, `rs_last_page`, `rs_landing`, `rs_referrer`, `rs_answer_user_agent`, `rs_keyword`, `rs_callback`, `rs_answer_raw_json`.
  - Служебные: `created_at`, `updated_at`, `call_id` (unique).
- Прочие таблицы: `analyses` (сырые eniq), `ringostat_calls` (легаси), `contacts` (телефон→имя/компания), `users` (репрезентатив↔телефон↔chat_id, admin flag), `admin_logs`, `interaction_logs`.

## Потоки данных
- ENIQ: периодическая выборка API `/analysis/results` + `/analysis/{id}`, запись в `analyses`, обновление `calls` по `metadata.uniqueid` (иначе поиск по телефону+времени в Ringostat).
- Ringostat:
  - Вебхуки `/webhooks/ringostat/after_call` → `upsert_calls` (основные поля звонка, уникальность, схема маршрутизации, user_agent, match c eniq).
  - Вебхуки `/webhooks/ringostat/answer_call` → `upsert_answer_call` (маркетинг до/в момент звонка: канал/источник/объявление/лендинг/keyword/callback/UA).
  - Инкрементальный опрос API (`fetch_incremental`) на 10 минут назад как бэкап канала.
- Уведомления менеджерам после анализа ENIQ строятся из `Analysis` + `Call` (уникальность, Call_Filter, ошибки <4, маркетинг не выводится).

## Новые поля/метрики из обновлённой схемы Ringostat
- Уникальность: `rs_is_unique`, `rs_is_unique_targeted` (целевой лид).
- Маршрут/кампания: `rs_call_scheme` (направление очереди/линии), `rs_project_id`.
- Качество матчинга: `rs_match_method`, `rs_match_time_diff_sec`, `rs_match_duration_diff_sec` в `rs_after_raw_json`.
- Маркетинг/старт звонка: `rs_market_comp`, `rs_target_post`, `rs_sales_channel`, `rs_sales_source`, `rs_keyword`, `rs_landing`/`rs_last_page`, `rs_referrer`, `rs_callback` (флаг обратного звонка), `rs_answer_user_agent`, **`client_mob` → сохраняется как `rs_client_number` (если ещё пусто) и кладётся в raw**.
- Техническое: `rs_user_agent` (после звонка), `call_status`/`call_type` в raw payload (`rs_status`/`rs_direction`), `call_id2` присутствует в raw.

## Идеи доработок с учётом новых метрик
1) **Атрибуция и маркетинг**: добавить отчёты/ответы по `sales_channel`/`sales_source`/`market_comp`/`target_post`/`landing`/`keyword`, фильтры в боте и недельных/дневных сводках.  
2) **Уникальность лидов**: выводить `rs_is_unique` и особенно `rs_is_unique_targeted` в уведомлениях и админских метриках; считать долю целевых/уникальных по репам/отделам/датам.  
3) **Пропущенные/обратные звонки**: мониторить `rs_status` (`NO ANSWER`/`VOICEMAIL`) + `rs_callback` → триггерить напоминания/списки “ожидают перезвона”.  
4) **Разрез по маршрутам**: отчёты по `rs_call_scheme` (Розница/Партнёрка/Callback/Responsible manager) + сравнение конверсий/оценок по схемам.  
5) **Качество матчинга**: контролировать `rs_match_*` (вынести в админ-бот: сколько связалось по uniqueid vs phone+time, средний `time_diff_sec`, подсветка несвязанных звонков).  
6) **Доп. поля в уведомлении менеджеру**: уникальность, call_status, call_scheme, ссылку на запись Ringostat (`rs_record_url`) при наличии; опционально маркетинг-блок.  
7) **Расширение инструментов бота**: в `tools.get_metrics/get_calls` добавить группировки по `calls` (источник/канал/схема/уникальность), короткие шаблоны ответов на вопросы “сколько уникальных/целевых за неделю”, “по какому каналу больше звонков сегодня”, “сколько callback’ов без ответа”.  
8) **Хранение `call_id2`**: при необходимости добавить колонку в `calls` (есть в raw Ringostat) для отладки дублей/слитых звонков.

## Быстрые заметки
- Единственная точка правды по звонкам — `calls` (анализ + маркетинг + телефонная часть + raw). `ringostat_calls` оставлена для совместимости.
- Локальные данные SQLite (`eniq.db`) больше не используются в проде, но лежат в корне для истории.
- Менеджерские маркетинг-уведомления сейчас отключены (оставлен код `_notify_manager`, вызов закомментирован).
