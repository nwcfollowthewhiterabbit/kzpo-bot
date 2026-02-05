import datetime
import httpx
from typing import Any, Dict, List, Optional

from .config import settings


class RingostatClient:
    def __init__(self) -> None:
        if not settings.ringostat_token:
            raise RuntimeError("RINGOSTAT_TOKEN is required for Ringostat client")
        if not settings.ringostat_project_id:
            raise RuntimeError("RINGOSTAT_PROJECT_ID is required for Ringostat client")
        self.token = settings.ringostat_token
        self.project_id = settings.ringostat_project_id
        self.base_url = "https://api.ringostat.net"
        self.client = httpx.Client(timeout=30.0)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def fetch_calls(
        self,
        date_from: datetime.datetime,
        date_to: datetime.datetime,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        payload = {
            "project_id": self.project_id,
            "date_from": date_from.strftime("%Y-%m-%d %H:%M:%S"),
            "date_to": date_to.strftime("%Y-%m-%d %H:%M:%S"),
            "page": page,
            "per_page": per_page,
        }
        resp = self.client.post(
            f"{self.base_url}/calls/list", headers=self._headers(), json=payload
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.client.close()
