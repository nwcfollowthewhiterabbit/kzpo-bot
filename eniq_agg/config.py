import os
from dataclasses import dataclass
from typing import Optional, List

from dotenv import load_dotenv


load_dotenv()


@dataclass
class Settings:
    eniq_token: str
    base_url: str
    project_id: int
    start_date: Optional[str]
    end_date: Optional[str]
    status: str
    page_size: int
    with_transcription: bool
    database_url: str
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    telegram_admin_ids: List[str]
    report_timezone: str
    weekly_report_chat_id: Optional[str]
    openai_api_key: Optional[str]
    openai_model: str
    ringostat_token: Optional[str]
    ringostat_project_id: Optional[str]
    disable_daily_digests: bool
    ringostat_webhook_user: Optional[str]
    ringostat_webhook_password: Optional[str]
    ringostat_webhook_port: int
    ringostat_webhook_enabled: bool
    google_sheets_enabled: bool
    google_sheets_spreadsheet_id: Optional[str]
    google_sheets_tab_name: str
    google_sheets_service_account_file: Optional[str]
    google_sheets_service_account_json: Optional[str]
    google_sheets_sync_interval_sec: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("ENIQ_TOKEN")
        if not token:
            raise RuntimeError("ENIQ_TOKEN is required in environment")
        base_url = os.getenv(
            "ENIQ_BASE_URL", "https://new-api.aicallsupervisor.io/api/v1"
        )
        project_id = int(os.getenv("ENIQ_PROJECT_ID", "209"))
        with_transcription_raw = os.getenv("ENIQ_WITH_TRANSCRIPTION", "1").lower()
        with_transcription = with_transcription_raw in {"1", "true", "yes", "on"}
        disable_digests_raw = os.getenv("DISABLE_DAILY_DIGESTS", "0").lower()
        disable_digests = disable_digests_raw in {"1", "true", "yes", "on"}
        webhook_enabled_raw = os.getenv("RINGOSTAT_WEBHOOK_ENABLED", "1").lower()
        webhook_enabled = webhook_enabled_raw in {"1", "true", "yes", "on"}
        sheets_enabled_raw = os.getenv("GOOGLE_SHEETS_SYNC_ENABLED", "0").lower()
        sheets_enabled = sheets_enabled_raw in {"1", "true", "yes", "on"}
        admin_ids = [
            x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()
        ]
        return cls(
            eniq_token=token,
            base_url=base_url,
            project_id=project_id,
            start_date=os.getenv("ENIQ_START_DATE"),
            end_date=os.getenv("ENIQ_END_DATE"),
            status=os.getenv("ENIQ_STATUS", "completed"),
            page_size=int(os.getenv("ENIQ_PAGE_SIZE", "100")),
            with_transcription=with_transcription,
            database_url=os.getenv("DATABASE_URL", "sqlite:///eniq.db"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            telegram_admin_ids=admin_ids,
            report_timezone=os.getenv("REPORT_TZ", "Europe/Kyiv"),
            weekly_report_chat_id=os.getenv("WEEKLY_REPORT_CHAT_ID"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            ringostat_token=os.getenv("RINGOSTAT_TOKEN"),
            ringostat_project_id=os.getenv("RINGOSTAT_PROJECT_ID"),
            disable_daily_digests=disable_digests,
            ringostat_webhook_user=os.getenv("RINGOSTAT_WEBHOOK_USER"),
            ringostat_webhook_password=os.getenv("RINGOSTAT_WEBHOOK_PASSWORD"),
            ringostat_webhook_port=int(os.getenv("RINGOSTAT_WEBHOOK_PORT", "8080")),
            ringostat_webhook_enabled=webhook_enabled,
            google_sheets_enabled=sheets_enabled,
            google_sheets_spreadsheet_id=os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"),
            google_sheets_tab_name=os.getenv("GOOGLE_SHEETS_TAB_NAME", "Разом"),
            google_sheets_service_account_file=os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE"),
            google_sheets_service_account_json=os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"),
            google_sheets_sync_interval_sec=int(os.getenv("GOOGLE_SHEETS_SYNC_INTERVAL_SEC", "300")),
        )


settings = Settings.from_env()
