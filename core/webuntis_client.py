"""WebUntis JSON-RPC Client — Lese-Zugriff auf WebUntis-Daten.

Nutzt die öffentliche JSON-RPC API (verfügbar für alle WebUntis-Schulen).
Schreib-Zugriff ist über diese API nicht möglich — dafür erzeugt das Plugin
eine CSV-Datei zum manuellen Import in WebUntis.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

# JSON-RPC Request-ID Counter
_next_id = 0


def _rpc_id() -> str:
    global _next_id  # noqa: PLW0603
    _next_id += 1
    return str(_next_id)


class WebUntisApiError(Exception):
    """Fehler bei WebUntis-API-Aufrufen."""


class WebUntisClient:
    """JSON-RPC Client für WebUntis (Lese-Zugriff).

    Endpoint: https://{server}/WebUntis/jsonrpc.do?school={school}
    Auth: authenticate-Methode → Session-Cookie (JSESSIONID)
    """

    def __init__(
        self,
        server: str,
        school: str,
        username: str,
        password: str,
    ) -> None:
        self._school = school
        self._username = username
        self._password = password

        self._session = requests.Session()
        self._authenticated = False

        # Basis-URL
        server = server.strip().rstrip("/")
        if not server.startswith("http"):
            server = f"https://{server}"
        self._rpc_url = f"{server}/WebUntis/jsonrpc.do"
        if school:
            self._rpc_url += f"?school={school}"

    # --- JSON-RPC Kern ---

    def _call(self, method: str, params: dict | None = None) -> dict | list:
        """Führt einen JSON-RPC 2.0 Call aus."""
        payload = {
            "id": _rpc_id(),
            "method": method,
            "params": params or {},
            "jsonrpc": "2.0",
        }

        resp = self._session.post(
            self._rpc_url,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            err = data["error"]
            code = err.get("code", "?")
            msg = err.get("message", str(err))
            raise WebUntisApiError(f"JSON-RPC Fehler [{code}]: {msg}")

        return data.get("result", {})

    # --- Auth ---

    def login(self) -> None:
        """Authentifiziert sich bei WebUntis (Session-Cookie wird gesetzt)."""
        result = self._call(
            "authenticate",
            {
                "user": self._username,
                "password": self._password,
                "client": "SchildSpider",
            },
        )
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise WebUntisApiError("Login fehlgeschlagen: keine sessionId erhalten")
        self._authenticated = True
        log.debug("WebUntis Login erfolgreich (Session: %s...)", session_id[:8])

    def logout(self) -> None:
        """Beendet die WebUntis-Session."""
        if self._authenticated:
            try:
                self._call("logout")
            except Exception:
                pass
            self._authenticated = False

    def _ensure_auth(self) -> None:
        """Stellt sicher, dass eine Session besteht."""
        if not self._authenticated:
            self.login()

    # --- Daten lesen ---

    def get_students(self) -> list[dict]:
        """Alle Schüler aus WebUntis lesen.

        Returns: [{id, key, name, foreName, longName, gender}, ...]
        """
        self._ensure_auth()
        result = self._call("getStudents")
        return result if isinstance(result, list) else []

    def get_klassen(self) -> list[dict]:
        """Alle Klassen aus WebUntis lesen.

        Returns: [{id, name, longName, active}, ...]
        """
        self._ensure_auth()
        result = self._call("getKlassen")
        return result if isinstance(result, list) else []

    def test_connection(self) -> tuple[bool, str]:
        """Verbindungstest: Login + Schüler abrufen."""
        try:
            self.login()
            students = self.get_students()
            klassen = self.get_klassen()
            self.logout()
            return (
                True,
                f"Verbunden. {len(students)} Schüler, "
                f"{len(klassen)} Klassen in WebUntis.",
            )
        except requests.ConnectionError:
            return False, "Verbindung fehlgeschlagen. Server-URL prüfen."
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response else "?"
            return False, f"HTTP-Fehler {code}"
        except WebUntisApiError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Fehler: {exc}"
