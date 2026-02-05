from typing import Any, Dict, List, Optional

import httpx

from .config import settings


class EniqClient:
    def __init__(
        self,
        token: str = settings.eniq_token,
        base_url: str = settings.base_url,
        timeout: int = 30,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def fetch_results(
        self,
        project_id: int,
        page: int = 1,
        limit: int = 100,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: str = "completed",
        with_transcription: bool = True,
    ) -> Dict[str, Any]:
        params = {
            "project_id": project_id,
            "page": page,
            "limit": limit,
            "status": status,
            "withTranscription": 1 if with_transcription else 0,
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        resp = self._client.get(
            f"{self.base_url}/analysis/results",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_analysis(self, analysis_id: int) -> Dict[str, Any]:
        resp = self._client.get(
            f"{self.base_url}/analysis/{analysis_id}", headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()["data"]

    def fetch_departments(self, project_id: int) -> List[str]:
        resp = self._client.get(
            f"{BASE_URL}/analytics/departments",
            headers=self._headers(),
            params={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_representatives(self, project_id: int) -> List[str]:
        resp = self._client.get(
            f"{BASE_URL}/analytics/representatives",
            headers=self._headers(),
            params={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
