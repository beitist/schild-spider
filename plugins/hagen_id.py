from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

import requests

from core.models import ConfigField
from plugins.base import PluginBase, is_success

log = logging.getLogger(__name__)

# Obergrenze für Rohantworten im Log — genug zum Diagnostizieren,
# wenig genug, dass ein Massenfehler spider.log nicht sprengt.
_LOG_BODY_MAX = 2000

_BATCH_SIZE_NEW = 200
_BATCH_SIZE_CHANGE = 200
_BATCH_SIZE_SUSPEND = 500

# Lehrkräfte liegen im Ausweis-System als Student-Datensätze in dieser
# Pseudo-Klasse. Beide Plugins müssen danach filtern — das Schüler-Plugin
# schließt sie aus, das Lehrer-Plugin arbeitet ausschließlich darauf.
TEACHER_CLASS = "Lehrerkollegium"

# (connect, read) — Read großzügig, weil Batches mit base64-Fotos groß werden
_TIMEOUT_MANIFEST = (10, 60)
_TIMEOUT_SYNC = (10, 180)


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
            resp = self._session.get(f"{self.api_url}/api/sync/manifest", timeout=10)
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

    def _fetch_manifest(self) -> list[dict]:
        """Holt das rohe Manifest — Schüler UND Lehrkräfte."""
        resp = self._session.get(
            f"{self.api_url}/api/sync/manifest", timeout=_TIMEOUT_MANIFEST
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("students", [])

    def get_manifest(self) -> list[dict]:
        """IST-Zustand der Schüler — ohne das Kollegium.

        Ohne diesen Filter wären die Lehrkräfte "im Zielsystem, aber nicht
        in den SchILD-Schülerdaten" und das Plugin würde bei jedem Lauf
        das komplette Kollegium deaktivieren.
        """
        return [
            s for s in self._fetch_manifest() if s.get("class_name") != TEACHER_CLASS
        ]

    def compute_data_hash(self, student: dict) -> str:
        parts = "|".join(
            [
                student.get("first_name", "").lower(),
                student.get("last_name", "").lower(),
                student.get("dob", ""),
                student.get("class_name", "").lower(),
                student.get("email", "").lower(),
            ]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def apply_new(self, students: list[dict]) -> list[dict]:
        results = []
        for batch in _batched(_dedupe_students(students), _BATCH_SIZE_NEW):
            payload = {"students": [self._prepare_student(s) for s in batch]}
            results.extend(self._post_sync("new", payload))
        return results

    def apply_changes(self, students: list[dict]) -> list[dict]:
        results = []
        for batch in _batched(_dedupe_students(students), _BATCH_SIZE_CHANGE):
            payload = {"students": [self._prepare_student(s) for s in batch]}
            results.extend(self._post_sync("change", payload))
        return results

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        results = []
        for batch in _batched(_dedupe_ids(school_internal_ids), _BATCH_SIZE_SUSPEND):
            payload = {"school_internal_ids": batch}
            results.extend(self._post_sync("suspend", payload))
        return results

    # --- Helpers ---

    def _post_sync(self, endpoint: str, payload: dict) -> list[dict]:
        """Schickt einen Batch und protokolliert die Antwort des Servers.

        Ohne dieses Logging bleibt bei einer unerwarteten Antwortstruktur
        nur ein nacktes "Unbekannter Fehler" im GUI übrig — die eigentliche
        Auskunft des Servers landet dann nirgendwo.
        """
        url = f"{self.api_url}/api/sync/{endpoint}"
        log.info("POST %s → %s", url, _payload_summary(payload))

        resp = self._session.post(url, json=payload, timeout=_TIMEOUT_SYNC)

        if resp.status_code >= 400:
            # Bei Validierungsfehlern steht die Ursache im Body —
            # raise_for_status() würde ihn wegwerfen.
            log.error(
                "POST %s → HTTP %s: %s",
                url,
                resp.status_code,
                resp.text[:_LOG_BODY_MAX],
            )
        resp.raise_for_status()

        data = resp.json()
        results = data.get("results", [])

        if not results:
            log.warning(
                "POST %s: Antwort ohne 'results': %s", url, str(data)[:_LOG_BODY_MAX]
            )
        else:
            unklar = [r for r in results if not is_success(r)]
            if unklar:
                log.warning(
                    "POST %s: %d von %d Ergebnissen nicht als Erfolg erkannt. "
                    "Rohantwort (erste 3): %s",
                    url,
                    len(unklar),
                    len(results),
                    str(unklar[:3])[:_LOG_BODY_MAX],
                )

        return results

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
        if photo_path and self._include_photo(student) and Path(photo_path).exists():
            with open(photo_path, "rb") as f:
                entry["photo_base64"] = base64.b64encode(f.read()).decode()

        return entry

    def _include_photo(self, student: dict) -> bool:
        """Darf das lokale Foto mitgeschickt werden? Beim Schüler-Sync immer."""
        return True

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


def _payload_summary(payload: dict) -> str:
    """Beschreibt einen Request fürs Log: Größe plus erster Datensatz.

    Belegt schwarz auf weiß, welche Felder tatsächlich rausgehen — sonst
    steht bei "Feld kommt im Zielsystem nicht an" Aussage gegen Aussage.
    Fotos werden auf ihre Länge reduziert, sonst ist das Log unlesbar.
    """
    if "school_internal_ids" in payload:
        ids = payload["school_internal_ids"]
        return f"{len(ids)} IDs, erste: {ids[:5]}"

    students = payload.get("students", [])
    if not students:
        return "leerer Payload"

    first = dict(students[0])
    photo = first.pop("photo_base64", None)
    if photo is not None:
        first["photo_base64"] = f"<{len(photo)} Zeichen>"

    return f"{len(students)} Datensätze, erster: {first}"


def _dedupe_students(students: list[dict]) -> list[dict]:
    """Entfernt doppelte school_internal_ids (letzter Eintrag gewinnt).

    Datensatz- und Foto-Änderungen laufen über denselben /change-Endpunkt;
    ohne Dedup landet eine Person, bei der sich beides geändert hat,
    zweimal im selben Request.
    """
    by_id: dict[str, dict] = {}
    for student in students:
        by_id[student["school_internal_id"]] = student
    return list(by_id.values())


def _dedupe_ids(school_internal_ids: list[str]) -> list[str]:
    """Entfernt doppelte IDs, behält die Reihenfolge."""
    return list(dict.fromkeys(school_internal_ids))
