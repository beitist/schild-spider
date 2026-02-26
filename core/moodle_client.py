"""Moodle REST API Client — Kommunikation mit Moodle Web Services."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

# Retry bei Server-Fehlern (5xx)
_MAX_RETRIES = 3


class MoodleApiError(Exception):
    """Fehler bei Moodle-API-Aufrufen."""

    def __init__(self, errorcode: str, message: str) -> None:
        self.errorcode = errorcode
        super().__init__(f"Moodle API [{errorcode}]: {message}")


class MoodleClient:
    """HTTP-Client für Moodle Web Services (Token-basiert)."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session = requests.Session()

    # --- HTTP-Kern ---

    @staticmethod
    def _flatten_params(params: dict, prefix: str = "") -> dict:
        """Flacht verschachtelte Dicts/Listen für Moodle-Encoding ab.

        Moodle erwartet Array-Parameter als:
        users[0][username]=john&users[0][firstname]=John
        """
        flat: dict[str, str] = {}
        for key, value in params.items():
            full_key = f"{prefix}[{key}]" if prefix else str(key)
            if isinstance(value, dict):
                flat.update(MoodleClient._flatten_params(value, full_key))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    idx_key = f"{full_key}[{i}]"
                    if isinstance(item, dict):
                        flat.update(MoodleClient._flatten_params(item, idx_key))
                    else:
                        flat[idx_key] = str(item)
            else:
                flat[full_key] = str(value)
        return flat

    def _call(self, function: str, **params: Any) -> Any:
        """Ruft eine Moodle Web Service Funktion auf."""
        url = f"{self._base_url}/webservice/rest/server.php"

        # Basis-Parameter
        base = {
            "wstoken": self._token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }

        # Verschachtelte Parameter flach machen
        flat = self._flatten_params(params)
        post_data = {**base, **flat}

        log.debug("Moodle API: %s (%d params)", function, len(flat))

        for attempt in range(_MAX_RETRIES):
            resp = self._session.post(url, data=post_data, timeout=30)

            if resp.status_code >= 500:
                log.warning(
                    "Moodle Server-Fehler %d (Versuch %d/%d)",
                    resp.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES - 1:
                    continue
                raise MoodleApiError(
                    "http_error", f"HTTP {resp.status_code}: {resp.text[:200]}"
                )

            if resp.status_code != 200:
                raise MoodleApiError(
                    "http_error", f"HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = resp.json()

            # Moodle gibt Fehler als JSON-Objekt mit "exception" zurück
            if isinstance(data, dict) and "exception" in data:
                errorcode = data.get("errorcode", "unknown")
                message = data.get("message", str(data))
                log.error("Moodle API Fehler: %s — %s", errorcode, message)
                raise MoodleApiError(errorcode, message)

            return data

        raise MoodleApiError("max_retries", "Maximale Versuche erreicht")

    # --- Verbindungstest ---

    def get_site_info(self) -> dict:
        """Holt Site-Informationen (Verbindungstest)."""
        return self._call("core_webservice_get_site_info")

    # --- User-Operationen ---

    def get_users(self, criteria: list[dict]) -> list[dict]:
        """Sucht User anhand von Kriterien.

        criteria: [{"key": "auth", "value": "oidc"}]
        """
        result = self._call("core_user_get_users", criteria=criteria)
        return result.get("users", [])

    def get_users_by_field(self, field: str, values: list[str]) -> list[dict]:
        """Holt User anhand eines Feldes (id, idnumber, username, email)."""
        if not values:
            return []
        return self._call("core_user_get_users_by_field", field=field, values=values)

    def create_users(self, users: list[dict]) -> list[dict]:
        """Legt neue User an. Returns: Liste mit {id, username}."""
        return self._call("core_user_create_users", users=users)

    def update_users(self, users: list[dict]) -> None:
        """Aktualisiert bestehende User (id muss gesetzt sein)."""
        self._call("core_user_update_users", users=users)

    # --- Kategorien ---

    def get_categories(self, criteria: list[dict] | None = None) -> list[dict]:
        """Holt Kurs-Kategorien."""
        if criteria:
            return self._call("core_course_get_categories", criteria=criteria)
        return self._call("core_course_get_categories")

    def create_categories(self, categories: list[dict]) -> list[dict]:
        """Erstellt Kategorien. Returns: Liste mit {id, name}."""
        return self._call("core_course_create_categories", categories=categories)

    # --- Kurse ---

    def get_courses_by_field(self, field: str, value: str) -> list[dict]:
        """Holt Kurse anhand eines Feldes (id, idnumber, shortname, category)."""
        result = self._call(
            "core_course_get_courses_by_field", field=field, value=value
        )
        return result.get("courses", [])

    def create_courses(self, courses: list[dict]) -> list[dict]:
        """Erstellt Kurse. Returns: Liste mit {id, shortname}."""
        return self._call("core_course_create_courses", courses=courses)

    # --- Einschreibungen ---

    def get_enrolled_users(self, course_id: int) -> list[dict]:
        """Holt alle eingeschriebenen User eines Kurses."""
        return self._call("core_enrol_get_enrolled_users", courseid=course_id)

    def enrol_users(self, enrolments: list[dict]) -> None:
        """Schreibt User in Kurse ein.

        enrolments: [{"roleid": 5, "userid": 42, "courseid": 789}]
        """
        self._call("enrol_manual_enrol_users", enrolments=enrolments)

    def unenrol_users(self, enrolments: list[dict]) -> None:
        """Meldet User von Kursen ab.

        enrolments: [{"userid": 42, "courseid": 789}]
        """
        self._call("enrol_manual_unenrol_users", enrolments=enrolments)
