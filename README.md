# eniq.ai analytics aggregator (prototype)

Мини‑сервис для выборки оценок звонков из eniq.ai и сохранения в локальную БД, с возможностью быстро вывести последние записи (для последующей интеграции с ботом/рассылкой).

## Настройка
1. Скопировать `.env.example` в `.env` и заполнить:
   - `ENIQ_TOKEN` — bearer токен (как в cookie `token`).
   - `ENIQ_PROJECT_ID` — ID проекта (по умолчанию 209).
   - `DATABASE_URL` — например `postgresql+psycopg2://eniq:eniq@localhost:5434/eniq` (для локального Postgres).
   - `ENIQ_START_DATE` / `ENIQ_END_DATE` — опционально ISO‑строки для фильтра.
   - `ENIQ_STATUS` — статус выборки (`completed` по умолчанию).
   - (опционально) `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` — для последующей рассылки.
2. Установить зависимости:
   ```bash
   pip install -r requirements.txt
   ```

## Использование
- Загрузить и сохранить все анализы:
  ```bash
  python run.py ingest
  ```
- Посмотреть последние записи:
  ```bash
  python run.py last --count 5
  python run.py last --count 10 --department "Партнерка"
  ```
- Ежедневные дайджесты и администрирование в Telegram:
  - Указать `TELEGRAM_BOT_TOKEN` и список админов в `TELEGRAM_ADMIN_IDS` (через запятую chat_id).
  - Админ открывает `/settings`, выбирает "Маппинг сотрудников", кликает ФИО и вводит телефон — сотрудник мапится на номер. После этого сотрудник нажимает `/start`, вводит свой номер и начинает получать дайджесты.
  - Админские действия логируются в таблицу `admin_logs`.

### Postgres (рекомендуется для аналитики)
- Запуск локального контейнера:  
  `docker run -d --name eniq_db -e POSTGRES_USER=eniq -e POSTGRES_PASSWORD=eniq -e POSTGRES_DB=eniq -p 5434:5432 postgres:15`
- Указать `DATABASE_URL=postgresql+psycopg2://eniq:eniq@localhost:5434/eniq` в `.env`.
- (Если ранее использовался SQLite) перенести данные:  
  `python scripts/migrate_sqlite_to_postgres.py --source sqlite:///eniq.db --target postgresql+psycopg2://eniq:eniq@localhost:5434/eniq`

### Telegram-бот
- Указать `TELEGRAM_BOT_TOKEN` в `.env`.
- Запустить:
  ```bash
  python run.py bot
  ```
- Команды бота:
  - `/start` / `/help` — подсказка
  - `/last [count] [department]` — последние записи, пример: `/last 5 Партнерка`

## Что сохраняется
- Основные поля из `/api/analysis/results`: id, статус, department, representative, prompt, даты, duration, external_url, summary/title, metadata/result (JSON).
- Детали из `/api/analysis/{id}`: audio_file JSON, транскрипт (текст + JSON), tokens_used_json.

## Следующие шаги
- Подключить Telegram‑бота на базе aiogram (зависимость уже добавлена): команда `/last` с фильтрами, отдача ссылки на аудио и summary.
- Добавить фоновый запуск (cron/systemd) для регулярной синхронизации.
