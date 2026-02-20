from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import requests

from core.models import ConfigField
from plugins.base import PluginBase

_BATCH_SIZE_NEW = 200
_BATCH_SIZE_CHANGE = 200
_BATCH_SIZE_SUSPEND = 500


class HagenIdPlugin(PluginBase):
    """Output-Plugin für das Hagen-ID Schülerausweis-System (REST API)."""

    def __init__(self, api_url: str, api_key: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = self.api_key

    # --- Metadaten ---

    @classmethod
    def plugin_name(cls) -> str:
        return "Hagen-ID"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="api_url",
                label="API URL",
                field_type="url",
                placeholder="https://ausweisapi.example.com",
            ),
            ConfigField(
                key="api_key",
                label="API Key",
                field_type="password",
                placeholder="hgn_sk_...",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> HagenIdPlugin:
        return cls(
            api_url=config.get("api_url", ""),
            api_key=config.get("api_key", ""),
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = self._session.get(
                f"{self.api_url}/api/sync/manifest", timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            school = data.get("school_name", "?")
            count = data.get("total_count", 0)
            return True, f"Verbunden: {school} ({count} Schüler)"
        except requests.ConnectionError:
            return False, "Verbindung fehlgeschlagen. URL prüfen."
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response else "?"
            if code == 401 or code == 403:
                return False, "Authentifizierung fehlgeschlagen. API Key prüfen."
            return False, f"HTTP-Fehler {code}"
        except Exception as exc:
            return False, f"Fehler: {exc}"

    # --- Sync-Interface ---

    def get_manifest(self) -> list[dict]:
        resp = self._session.get(f"{self.api_url}/api/sync/manifest")
        resp.raise_for_status()
        data = resp.json()
        return data.get("students", [])

    def compute_data_hash(self, student: dict) -> str:
        parts = "|".join([
            student.get("first_name", "").lower(),
            student.get("last_name", "").lower(),
            student.get("dob", ""),
            student.get("class_name", "").lower(),
            student.get("email", "").lower(),
        ])
        return hashlib.sha256(parts.encode()).hexdigest()

    def apply_new(self, students: list[dict]) -> list[dict]:
        results = []
        for batch in _batched(students, _BATCH_SIZE_NEW):
            payload = {"students": [self._prepare_student(s) for s in batch]}
            resp = self._session.post(
                f"{self.api_url}/api/sync/new", json=payload
            )
            resp.raise_for_status()
            results.extend(resp.json().get("results", []))
        return results

    def apply_changes(self, students: list[dict]) -> list[dict]:
        results = []
        for batch in _batched(students, _BATCH_SIZE_CHANGE):
            payload = {"students": [self._prepare_student(s) for s in batch]}
            resp = self._session.post(
                f"{self.api_url}/api/sync/change", json=payload
            )
            resp.raise_for_status()
            results.extend(resp.json().get("results", []))
        return results

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        results = []
        for batch in _batched(school_internal_ids, _BATCH_SIZE_SUSPEND):
            payload = {"school_internal_ids": batch}
            resp = self._session.post(
                f"{self.api_url}/api/sync/suspend", json=payload
            )
            resp.raise_for_status()
            results.extend(resp.json().get("results", []))
        return results

    # --- Helpers ---

    def _prepare_student(self, student: dict) -> dict:
        entry = {
            "school_internal_id": student["school_internal_id"],
            "first_name": student["first_name"],
            "last_name": student["last_name"],
            "dob": student["dob"],
            "class_name": student["class_name"],
            "email": student.get("email", ""),
        }

        photo_path = student.get("photo_path")
        if photo_path and Path(photo_path).exists():
            with open(photo_path, "rb") as f:
                entry["photo_base64"] = base64.b64encode(f.read()).decode()

        return entry

    @staticmethod
    def compute_photo_hash(photo_path: str) -> str | None:
        path = Path(photo_path)
        if not path.exists():
            return None
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return hashlib.sha256(b64.encode()).hexdigest()


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
