"""M365-Plugin — Schüler-Accounts in Microsoft 365 / Entra ID verwalten."""

from __future__ import annotations

import hashlib
import secrets
import string
import warnings

from core.email_generator import generate_email
from core.graph_client import GraphApiError, GraphClient
from core.models import ConfigField
from plugins.base import PluginBase


class M365Plugin(PluginBase):
    """Output-Plugin für Microsoft 365 / Entra ID (Graph REST API)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        domain: str,
        email_template: str,
        license_sku_id: str,
        group_prefix: str,
        usage_location: str,
    ) -> None:
        self._domain = domain
        self._email_template = email_template or "{v}.{n}"
        self._license_sku_id = license_sku_id
        self._group_prefix = group_prefix or "Klasse"
        self._usage_location = usage_location or "DE"
        self._graph = GraphClient(tenant_id, client_id, client_secret)

        # Caches (pro Lauf)
        self._group_cache: dict[str, str] = {}  # class_name → group_id
        self._generated_emails: list[dict] = []  # für Write-back

    # --- Metadaten ---

    @classmethod
    def plugin_name(cls) -> str:
        return "Microsoft 365"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="tenant_id",
                label="Tenant ID",
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            ),
            ConfigField(
                key="client_id",
                label="Client (App) ID",
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            ),
            ConfigField(
                key="client_secret",
                label="Client Secret",
                field_type="password",
            ),
            ConfigField(
                key="domain",
                label="Schüler-Domain",
                placeholder="schule-hagen.de",
            ),
            ConfigField(
                key="email_template",
                label="Email-Template ({v}=Vorname, {n}=Nachname)",
                placeholder="{v}.{n}",
                default="{v}.{n}",
            ),
            ConfigField(
                key="license_sku_id",
                label="Lizenz-SKU ID (leer = keine)",
                required=False,
                default="",
            ),
            ConfigField(
                key="group_prefix",
                label="Gruppen-Präfix",
                placeholder="Klasse",
                default="Klasse",
            ),
            ConfigField(
                key="usage_location",
                label="Nutzungsstandort (ISO)",
                placeholder="DE",
                default="DE",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> M365Plugin:
        return cls(
            tenant_id=config.get("tenant_id", ""),
            client_id=config.get("client_id", ""),
            client_secret=config.get("client_secret", ""),
            domain=config.get("domain", ""),
            email_template=config.get("email_template", "{v}.{n}"),
            license_sku_id=config.get("license_sku_id", ""),
            group_prefix=config.get("group_prefix", "Klasse"),
            usage_location=config.get("usage_location", "DE"),
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            users = self._graph.list_users(self._domain)
            msg = f"Verbunden. {len(users)} Benutzer in @{self._domain}"
            if self._license_sku_id:
                skus = self._graph.list_skus()
                sku_ids = {s["skuId"] for s in skus}
                if self._license_sku_id not in sku_ids:
                    return False, f"SKU '{self._license_sku_id}' nicht gefunden."
                msg += " | SKU OK"
            return True, msg
        except GraphApiError as exc:
            if exc.status_code in (401, 403):
                return False, "Authentifizierung fehlgeschlagen. Credentials prüfen."
            return False, f"Graph API Fehler: {exc}"
        except Exception as exc:
            return False, f"Verbindungsfehler: {exc}"

    # --- Sync-Interface ---

    def get_manifest(self) -> list[dict]:
        users = self._graph.list_users(self._domain)
        manifest: list[dict] = []
        for u in users:
            eid = u.get("employeeId")
            if not eid:
                continue
            student_dict = {
                "first_name": u.get("givenName", ""),
                "last_name": u.get("surname", ""),
                "dob": (u.get("birthday") or "")[:10],
                "class_name": u.get("department", ""),
                "email": u.get("userPrincipalName", ""),
            }
            manifest.append(
                {
                    "school_internal_id": eid,
                    "data_hash": self.compute_data_hash(student_dict),
                    "is_active": u.get("accountEnabled", True),
                }
            )
        return manifest

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
        self._generated_emails = []
        existing_emails = self._collect_existing_emails()
        results: list[dict] = []

        for student in students:
            sid = student["school_internal_id"]
            try:
                email = (student.get("email") or "").strip()
                if not email:
                    email = generate_email(
                        student.get("first_name", ""),
                        student.get("last_name", ""),
                        self._domain,
                        self._email_template,
                        existing_emails,
                    )
                    self._generated_emails.append(
                        {"school_internal_id": sid, "email": email}
                    )

                existing_emails.add(email.lower())

                user_data = {
                    "accountEnabled": True,
                    "displayName": f"{student.get('first_name', '')} {student.get('last_name', '')}".strip(),
                    "givenName": student.get("first_name", ""),
                    "surname": student.get("last_name", ""),
                    "userPrincipalName": email,
                    "mailNickname": email.split("@")[0],
                    "employeeId": sid,
                    "department": student.get("class_name", ""),
                    "usageLocation": self._usage_location,
                    "passwordProfile": {
                        "password": _generate_password(),
                        "forceChangePasswordNextSignIn": True,
                    },
                }

                birthday = student.get("dob", "")
                if birthday:
                    user_data["birthday"] = birthday

                created = self._graph.create_user(user_data)
                user_id = created["id"]

                if self._license_sku_id:
                    try:
                        self._graph.assign_license(user_id, self._license_sku_id)
                    except GraphApiError as exc:
                        warnings.warn(f"Lizenz für {sid}: {exc}")

                self._assign_to_class_group(
                    user_id, student.get("class_name", ""), student
                )

                results.append(
                    {"school_internal_id": sid, "success": True, "message": email}
                )

            except GraphApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    def apply_changes(self, students: list[dict]) -> list[dict]:
        results: list[dict] = []
        for student in students:
            sid = student["school_internal_id"]
            try:
                user = self._graph.find_user_by_employee_id(sid)
                if not user:
                    results.append(
                        {
                            "school_internal_id": sid,
                            "success": False,
                            "message": f"User mit employeeId={sid} nicht gefunden",
                        }
                    )
                    continue

                user_id = user["id"]
                updates: dict = {}

                if student.get("first_name") and student["first_name"] != user.get(
                    "givenName", ""
                ):
                    updates["givenName"] = student["first_name"]
                if student.get("last_name") and student["last_name"] != user.get(
                    "surname", ""
                ):
                    updates["surname"] = student["last_name"]
                if student.get("class_name") and student["class_name"] != user.get(
                    "department", ""
                ):
                    updates["department"] = student["class_name"]

                email = (student.get("email") or "").strip()
                if email and email.lower() != user.get("userPrincipalName", "").lower():
                    updates["userPrincipalName"] = email
                    updates["mailNickname"] = email.split("@")[0]

                if updates:
                    display = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip()
                    if display:
                        updates["displayName"] = display
                    self._graph.update_user(user_id, updates)

                # Gruppenwechsel bei Klassenwechsel
                old_class = user.get("department", "")
                new_class = student.get("class_name", "")
                if old_class and new_class and old_class != new_class:
                    self._move_between_groups(user_id, old_class, new_class, student)

                results.append(
                    {"school_internal_id": sid, "success": True, "message": ""}
                )

            except GraphApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        results: list[dict] = []
        for sid in school_internal_ids:
            try:
                user = self._graph.find_user_by_employee_id(sid)
                if not user:
                    results.append(
                        {
                            "school_internal_id": sid,
                            "success": False,
                            "message": f"User mit employeeId={sid} nicht gefunden",
                        }
                    )
                    continue

                self._graph.update_user(user["id"], {"accountEnabled": False})
                results.append(
                    {"school_internal_id": sid, "success": True, "message": ""}
                )

            except GraphApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    # --- Write-back ---

    def get_write_back_data(self) -> list[dict]:
        return list(self._generated_emails)

    # --- Gruppen-Hilfsmethoden ---

    def _ensure_class_group(self, class_name: str) -> str | None:
        """Findet oder erstellt die M365-Gruppe für eine Klasse."""
        if not class_name:
            return None

        if class_name in self._group_cache:
            return self._group_cache[class_name]

        display_name = f"{self._group_prefix} {class_name}"
        nickname = f"{self._group_prefix.lower()}-{class_name.lower()}"
        nickname = "".join(c for c in nickname if c.isalnum() or c == "-")

        # Existierende Gruppe suchen
        groups = self._graph.list_groups(display_name)
        for g in groups:
            if g.get("displayName") == display_name:
                self._group_cache[class_name] = g["id"]
                return g["id"]

        # Neue Gruppe erstellen
        try:
            group = self._graph.create_group(
                {
                    "displayName": display_name,
                    "mailNickname": nickname,
                    "mailEnabled": True,
                    "securityEnabled": True,
                    "groupTypes": ["Unified"],
                }
            )
            group_id = group["id"]
            self._group_cache[class_name] = group_id
            return group_id
        except GraphApiError as exc:
            warnings.warn(
                f"Gruppe '{display_name}' konnte nicht erstellt werden: {exc}"
            )
            return None

    def _assign_to_class_group(
        self, user_id: str, class_name: str, student: dict
    ) -> None:
        """Fügt User zur Klassengruppe hinzu und setzt Lehrer als Owner."""
        group_id = self._ensure_class_group(class_name)
        if not group_id:
            return

        try:
            self._graph.add_member(group_id, user_id)
        except GraphApiError as exc:
            if "already exist" not in str(exc).lower():
                warnings.warn(f"Gruppenmitglied {user_id}: {exc}")

        # Klassenlehrer als Owner (best-effort)
        for teacher_field in ("class_teacher_1", "class_teacher_2"):
            teacher_name = student.get(teacher_field, "")
            if teacher_name:
                self._set_teacher_as_owner(group_id, teacher_name)

    def _set_teacher_as_owner(self, group_id: str, teacher_name: str) -> None:
        """Sucht den Lehrer per UPN/Name und setzt ihn als Gruppen-Owner."""
        try:
            # Versuche UPN-basierte Suche (name@domain)
            users = self._graph.list_users(self._domain)
            for u in users:
                surname = u.get("surname", "")
                if surname and surname.lower() == teacher_name.lower():
                    try:
                        self._graph.add_owner(group_id, u["id"])
                    except GraphApiError as exc:
                        if "already exist" not in str(exc).lower():
                            warnings.warn(f"Owner {teacher_name}: {exc}")
                    return
        except Exception:
            pass

    def _move_between_groups(
        self, user_id: str, old_class: str, new_class: str, student: dict
    ) -> None:
        """Verschiebt einen User von der alten in die neue Klassengruppe."""
        # Aus alter Gruppe entfernen
        old_group_id = self._ensure_class_group(old_class)
        if old_group_id:
            try:
                self._graph.remove_member(old_group_id, user_id)
            except GraphApiError:
                pass

        # In neue Gruppe aufnehmen
        self._assign_to_class_group(user_id, new_class, student)

    def _collect_existing_emails(self) -> set[str]:
        """Sammelt alle existierenden Email-Adressen aus M365."""
        try:
            users = self._graph.list_users(self._domain)
            return {u.get("userPrincipalName", "").lower() for u in users}
        except GraphApiError:
            return set()


def _generate_password(length: int = 16) -> str:
    """Generiert ein sicheres Initialpasswort."""
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    # Mindestens je 1 Groß-, Kleinbuchstabe, Ziffer, Sonderzeichen
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%" for c in password)
        ):
            return password
