"""Microsoft Graph REST API Client mit OAuth2 Client Credentials Flow."""

from __future__ import annotations

import time

import requests

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
        self._token: str = ""
        self._token_expires: float = 0.0

    # --- Token-Management ---

    def _get_token(self) -> str:
        """Holt oder cached einen Bearer-Token (Client Credentials Flow)."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        resp = requests.post(
            _TOKEN_URL.format(tenant_id=self._tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": _TOKEN_SCOPE,
            },
            timeout=15,
        )
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
                },
                timeout=30,
            )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER))
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
                raise GraphApiError(
                    resp.status_code,
                    error.get("message", resp.text),
                    error.get("code", ""),
                )

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
        "id,employeeId,givenName,surname,birthday,department,"
        "userPrincipalName,accountEnabled,mail,displayName"
    )

    def list_users(self, domain: str) -> list[dict]:
        """Listet alle User einer Domain auf."""
        return self._request_paged(
            "/users",
            params={
                "$filter": f"endsWith(userPrincipalName,'@{domain}')",
                "$select": self._USER_SELECT,
                "$count": "true",
            },
        )

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
        """Listet Gruppen die mit prefix beginnen."""
        return self._request_paged(
            "/groups",
            params={
                "$filter": f"startsWith(displayName,'{prefix}')",
                "$select": "id,displayName,mailNickname,mail",
            },
        )

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
