"""Microsoft Graph REST API Client mit OAuth2 Client Credentials Flow."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_SCOPE = "https://graph.microsoft.com/.default"

# Retry bei Throttling (429)
_MAX_RETRIES = 3
_DEFAULT_RETRY_AFTER = 5  # Sekunden


class GraphApiError(Exception):
    """Fehler bei Graph-API-Aufrufen."""

    def __init__(self, status_code: int, message: str, error_code: str = "") -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API {status_code}: {message} ({error_code})")


class GraphClient:
    """HTTP-Client für Microsoft Graph API (Application Permissions)."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = requests.Session()
        # Keep-Alive deaktivieren — jede Request nutzt eine frische SSL-Connection.
        # Verhindert Access Violations durch SSL Connection Reuse, die auf Windows
        # mit PyInstaller's gebundeltem OpenSSL auftreten können.
        self._session.headers["Connection"] = "close"
        self._token: str = ""
        self._token_expires: float = 0.0

    # --- Token-Management ---

    def _get_token(self) -> str:
        """Holt oder cached einen Bearer-Token (Client Credentials Flow)."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        token_url = _TOKEN_URL.format(tenant_id=self._tenant_id)
        log.debug("Token-Request: POST %s", token_url)
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": _TOKEN_SCOPE,
            },
            timeout=15,
        )
        log.debug("Token-Response: %s", resp.status_code)
        if resp.status_code != 200:
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            raise GraphApiError(
                resp.status_code,
                body.get("error_description", resp.text),
                body.get("error", ""),
            )

        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)
        return self._token

    # --- HTTP-Kern ---

    def _request(
        self,
        method: str,
        path: str,
        json: dict | list | None = None,
        params: dict | None = None,
    ) -> dict:
        """Sendet einen Request an die Graph API mit Auth + Retry."""
        url = f"{_GRAPH_BASE}{path}" if path.startswith("/") else path
        log.debug("%s %s params=%s", method, url, params)

        for attempt in range(_MAX_RETRIES):
            token = self._get_token()
            resp = self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "ConsistencyLevel": "eventual",
                },
                timeout=30,
            )

            log.debug("Response: %s %s", resp.status_code, method)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER))
                log.warning("Throttled (429), retry nach %ds", retry_after)
                time.sleep(retry_after)
                continue

            if resp.status_code == 204:
                return {}

            if resp.status_code >= 400:
                body = {}
                try:
                    body = resp.json()
                except Exception:
                    pass
                error = body.get("error", {})
                msg = error.get("message", "") or resp.text
                code = error.get("code", "")
                log.error(
                    "Graph API Fehler: %s %s → %s [%s] %s",
                    method,
                    url,
                    resp.status_code,
                    code,
                    msg,
                )
                raise GraphApiError(resp.status_code, msg, code)

            if not resp.content:
                return {}
            return resp.json()

        raise GraphApiError(429, "Throttling: max retries erreicht")

    def _request_paged(self, path: str, params: dict | None = None) -> list[dict]:
        """Holt alle Seiten einer paginierten Antwort."""
        results: list[dict] = []
        resp = self._request("GET", path, params=params)
        results.extend(resp.get("value", []))

        while "@odata.nextLink" in resp:
            resp = self._request("GET", resp["@odata.nextLink"])
            results.extend(resp.get("value", []))

        return results

    # --- User-Operationen ---

    _USER_SELECT = (
        "id,employeeId,givenName,surname,department,"
        "userPrincipalName,accountEnabled,mail,displayName"
    )

    def list_users(self, domain: str) -> list[dict]:
        """Listet alle User einer Domain auf (serverseitig gefiltert).

        Nutzt $filter=endsWith (advanced query) statt alle User zu laden.
        Benötigt ConsistencyLevel: eventual (bereits in _request gesetzt)
        und $count=true als Handshake für advanced queries.
        """
        domain_suffix = f"@{domain}"
        users = self._request_paged(
            "/users",
            params={
                "$select": self._USER_SELECT,
                "$filter": f"endsWith(userPrincipalName,'{domain_suffix}')",
                "$count": "true",
            },
        )
        log.debug("list_users: %d mit Domain %s", len(users), domain_suffix)
        return users

    def create_user(self, user_data: dict) -> dict:
        """Legt einen neuen User an."""
        return self._request("POST", "/users", json=user_data)

    def update_user(self, user_id: str, updates: dict) -> dict:
        """Aktualisiert einen User."""
        return self._request("PATCH", f"/users/{user_id}", json=updates)

    def find_user_by_employee_id(self, employee_id: str) -> dict | None:
        """Sucht einen User anhand seiner employeeId."""
        results = self._request_paged(
            "/users",
            params={
                "$filter": f"employeeId eq '{employee_id}'",
                "$select": self._USER_SELECT,
            },
        )
        return results[0] if results else None

    def find_user_by_upn(self, upn: str) -> dict | None:
        """Sucht einen User anhand seines UPN (E-Mail)."""
        try:
            return self._request(
                "GET", f"/users/{upn}", params={"$select": self._USER_SELECT}
            )
        except GraphApiError as e:
            if e.status_code == 404:
                return None
            raise

    # --- Lizenzen ---

    def list_skus(self) -> list[dict]:
        """Listet alle verfügbaren Lizenz-SKUs."""
        resp = self._request("GET", "/subscribedSkus")
        return resp.get("value", [])

    def assign_license(self, user_id: str, sku_id: str) -> dict:
        """Weist einem User eine Lizenz zu."""
        return self._request(
            "POST",
            f"/users/{user_id}/assignLicense",
            json={
                "addLicenses": [{"skuId": sku_id}],
                "removeLicenses": [],
            },
        )

    # --- Gruppen ---

    def list_groups(self, prefix: str) -> list[dict]:
        """Listet Gruppen die mit prefix beginnen (serverseitig gefiltert).

        Nutzt $filter=startsWith (advanced query) statt alle Gruppen zu laden.
        """
        safe_prefix = prefix.replace("'", "''")
        return self._request_paged(
            "/groups",
            params={
                "$select": "id,displayName,mailNickname,mail",
                "$filter": f"startsWith(displayName,'{safe_prefix}')",
                "$count": "true",
            },
        )

    def find_group_by_name(self, display_name: str) -> dict | None:
        """Sucht eine Gruppe per exaktem displayName (einfacher Gleichheitsfilter)."""
        safe_name = display_name.replace("'", "''")
        results = self._request_paged(
            "/groups",
            params={
                "$select": "id,displayName,mailNickname,mail",
                "$filter": f"displayName eq '{safe_name}'",
            },
        )
        return results[0] if results else None

    def list_all_groups(self) -> list[dict]:
        """Listet ALLE Gruppen im Tenant (für client-seitige Filterung)."""
        return self._request_paged(
            "/groups",
            params={"$select": "id,displayName,mailNickname,mail"},
        )

    def get_group(self, group_id: str) -> dict | None:
        """Holt eine Gruppe per ID. Gibt None zurück falls 404 (noch nicht repliziert)."""
        try:
            return self._request(
                "GET",
                f"/groups/{group_id}",
                params={"$select": "id,displayName"},
            )
        except GraphApiError as e:
            if e.status_code == 404:
                return None
            raise

    def create_group(self, group_data: dict) -> dict:
        """Erstellt eine neue Gruppe."""
        return self._request("POST", "/groups", json=group_data)

    def get_members(self, group_id: str) -> list[dict]:
        """Listet alle Mitglieder einer Gruppe."""
        return self._request_paged(
            f"/groups/{group_id}/members",
            params={"$select": "id,employeeId,userPrincipalName"},
        )

    def add_member(self, group_id: str, user_id: str) -> None:
        """Fügt einen User als Mitglied hinzu."""
        self._request(
            "POST",
            f"/groups/{group_id}/members/$ref",
            json={
                "@odata.id": f"{_GRAPH_BASE}/directoryObjects/{user_id}",
            },
        )

    def remove_member(self, group_id: str, user_id: str) -> None:
        """Entfernt ein Mitglied aus einer Gruppe."""
        self._request("DELETE", f"/groups/{group_id}/members/{user_id}/$ref")

    def add_owner(self, group_id: str, user_id: str) -> None:
        """Fügt einen User als Gruppen-Besitzer hinzu."""
        self._request(
            "POST",
            f"/groups/{group_id}/owners/$ref",
            json={
                "@odata.id": f"{_GRAPH_BASE}/directoryObjects/{user_id}",
            },
        )

    # --- Batch ---

    _BATCH_LIMIT = 20  # Graph API Maximum pro Batch-Request

    def batch(self, requests_list: list[dict]) -> list[dict]:
        """Sendet bis zu 20 Requests als Batch an POST /$batch.

        Jeder Eintrag in requests_list hat:
          {"id": "...", "method": "POST|DELETE", "url": "/groups/...",
           "headers": {...}, "body": {...}}

        Gibt die Responses zurück (gleiche Reihenfolge wie Eingabe).
        Bei Throttling (429) wird der gesamte Batch wiederholt.
        """
        if not requests_list:
            return []
        if len(requests_list) > self._BATCH_LIMIT:
            raise ValueError(
                f"Batch enthält {len(requests_list)} Requests, "
                f"max {self._BATCH_LIMIT} erlaubt."
            )

        payload = {"requests": requests_list}
        resp = self._request("POST", "/$batch", json=payload)
        return resp.get("responses", [])
