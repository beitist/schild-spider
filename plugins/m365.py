"""M365-Plugin — Schüler-Accounts in Microsoft 365 / Entra ID verwalten."""

from __future__ import annotations

import hashlib
import re
import secrets
import string
import warnings

from core.email_generator import generate_email
from core.graph_client import GraphApiError, GraphClient
from core.models import ChangeSet, ConfigField
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
        group_sus_template: str,
        group_kuk_template: str,
        usage_location: str,
        display_name_template: str = "",
        default_password: str = "",
    ) -> None:
        self._domain = domain
        self._email_template = email_template or "{k}.{n}"
        self._license_sku_id = license_sku_id
        self._group_sus_template = group_sus_template or "{k}_sus"
        self._group_kuk_template = group_kuk_template or "{k}_kuk"
        self._usage_location = usage_location or "DE"
        self._display_name_template = display_name_template or "{k} {n}, {v}"
        self._default_password = default_password or ""
        self._graph = GraphClient(tenant_id, client_id, client_secret)

        # Caches (pro Lauf)
        self._sus_cache: dict[str, str] = {}  # class_name → group_id
        self._kuk_cache: dict[str, str] = {}  # class_name → group_id
        self._kuk_processed: set[str] = set()  # Klassen, deren KuK schon bearbeitet
        self._generated_emails: list[dict] = []  # für Write-back
        self._existing_emails: set[str] = set()  # gecached aus get_manifest
        self._all_users: list[dict] | None = None  # gecached für Lehrer-Suche
        self._groups_bulk_loaded: bool = False  # Gruppen-Cache komplett?

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
                label="Email-Template ({k}=Klasse, {n}=Nachname, {v}=Vorname)",
                placeholder="{k}.{n}",
                default="{k}.{n}",
            ),
            ConfigField(
                key="license_sku_id",
                label="Lizenz-SKU ID (leer = keine)",
                required=False,
                default="",
            ),
            ConfigField(
                key="group_sus_template",
                label="Gruppen-Mail SuS ({k}=Klasse)",
                placeholder="{k}_sus",
                default="{k}_sus",
            ),
            ConfigField(
                key="group_kuk_template",
                label="Gruppen-Mail KuK ({k}=Klasse)",
                placeholder="{k}_kuk",
                default="{k}_kuk",
            ),
            ConfigField(
                key="default_password",
                label="Start-Passwort (leer = zufällig)",
                placeholder="Schule2223!",
                default="Schule2223!",
                required=False,
            ),
            ConfigField(
                key="usage_location",
                label="Nutzungsstandort (ISO)",
                placeholder="DE",
                default="DE",
            ),
            ConfigField(
                key="display_name_template",
                label="Anzeigename ({k}=Klasse, {n}=Nachname, {v}=Vorname)",
                placeholder="{k} {n}, {v}",
                default="{k} {n}, {v}",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> M365Plugin:
        return cls(
            tenant_id=config.get("tenant_id", ""),
            client_id=config.get("client_id", ""),
            client_secret=config.get("client_secret", ""),
            domain=config.get("domain", ""),
            email_template=config.get("email_template", "{k}.{n}"),
            license_sku_id=config.get("license_sku_id", ""),
            group_sus_template=config.get("group_sus_template", "{k}_sus"),
            group_kuk_template=config.get("group_kuk_template", "{k}_kuk"),
            usage_location=config.get("usage_location", "DE"),
            display_name_template=config.get("display_name_template", "{k} {n}, {v}"),
            default_password=config.get("default_password", "Schule2223!"),
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

    def _format_display_name(self, student: dict) -> str:
        """Formatiert den Anzeigenamen nach dem konfigurierten Template."""
        v = student.get("first_name", "")
        n = student.get("last_name", "")
        k = student.get("class_name", "")
        return (
            self._display_name_template.replace("{v}", v)
            .replace("{n}", n)
            .replace("{k}", k)
            .strip()
        )

    # --- Sync-Interface ---

    def get_manifest(self) -> list[dict]:
        users = self._graph.list_users(self._domain)
        # Email-Cache für enrich_preview und apply_new
        self._existing_emails = {
            (u.get("userPrincipalName") or "").lower() for u in users
        }
        self._all_users = users

        manifest: list[dict] = []
        # Email-Fallback: User ohne employeeId per Email matchen
        self._email_manifest: dict[str, dict] = {}

        for u in users:
            eid = u.get("employeeId")
            upn = (u.get("userPrincipalName") or "").lower()
            student_dict = {
                "first_name": u.get("givenName") or "",
                "last_name": u.get("surname") or "",
                "class_name": u.get("department") or "",
                "email": u.get("userPrincipalName") or "",
            }
            data_hash = self.compute_data_hash(student_dict)
            is_active = u.get("accountEnabled", True)

            if eid:
                manifest.append(
                    {
                        "school_internal_id": eid,
                        "data_hash": data_hash,
                        "is_active": is_active,
                    }
                )
            elif upn:
                # Kein employeeId → per Email matchbar (Fallback für Engine)
                self._email_manifest[upn] = {
                    "school_internal_id": "",
                    "data_hash": data_hash,
                    "is_active": is_active,
                }
        return manifest

    def compute_data_hash(self, student: dict) -> str:
        parts = "|".join(
            [
                (student.get("first_name") or "").lower(),
                (student.get("last_name") or "").lower(),
                (student.get("class_name") or "").lower(),
                (student.get("email") or "").lower(),
            ]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def enrich_preview(self, changeset: ChangeSet) -> None:
        """Generiert Emails für neue Schüler, damit sie in der Vorschau sichtbar sind."""
        preview_emails = set(self._existing_emails)
        for student in changeset.new:
            email = (student.get("email") or "").strip()
            if not email:
                email = generate_email(
                    student.get("first_name", ""),
                    student.get("last_name", ""),
                    self._domain,
                    self._email_template,
                    preview_emails,
                    class_name=student.get("class_name", ""),
                )
                if email:
                    student["email"] = email
                    preview_emails.add(email.lower())

    def apply_new(self, students: list[dict]) -> list[dict]:
        self._generated_emails = []
        existing_emails = set(self._existing_emails) or self._collect_existing_emails()
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
                        class_name=student.get("class_name", ""),
                    )
                    if email is None:
                        results.append(
                            {
                                "school_internal_id": sid,
                                "success": False,
                                "message": "Email-Kollision: manuell vergeben",
                            }
                        )
                        continue

                # Generierte Email für Write-back merken
                self._generated_emails.append(
                    {
                        "school_internal_id": sid,
                        "email": email,
                        "first_name": student.get("first_name", ""),
                        "last_name": student.get("last_name", ""),
                        "class_name": student.get("class_name", ""),
                    }
                )

                existing_emails.add(email.lower())

                # Prüfen ob User per Email schon existiert (ohne employeeId)
                existing_user = self._graph.find_user_by_upn(email)
                if existing_user:
                    user_id = existing_user["id"]
                    self._graph.update_user(
                        user_id,
                        {
                            "employeeId": sid,
                            "department": student.get("class_name", ""),
                            "displayName": self._format_display_name(student),
                        },
                    )
                    self._assign_to_class_group(
                        user_id, student.get("class_name", ""), student
                    )
                    results.append(
                        {
                            "school_internal_id": sid,
                            "success": True,
                            "message": f"Verknüpft: {email}",
                        }
                    )
                    continue

                user_data = {
                    "accountEnabled": True,
                    "displayName": self._format_display_name(student),
                    "givenName": student.get("first_name", ""),
                    "surname": student.get("last_name", ""),
                    "userPrincipalName": email,
                    "mailNickname": email.split("@")[0],
                    "employeeId": sid,
                    "department": student.get("class_name", ""),
                    "usageLocation": self._usage_location,
                    "passwordProfile": {
                        "password": self._default_password or _generate_password(),
                        "forceChangePasswordNextSignIn": True,
                    },
                }

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
                    # Fallback: per Email suchen (User ohne employeeId)
                    email = (student.get("email") or "").strip()
                    if email:
                        user = self._graph.find_user_by_upn(email)
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

                # employeeId nachsetzen falls fehlend
                if not user.get("employeeId"):
                    updates["employeeId"] = sid

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
                if (
                    email
                    and email.lower() != (user.get("userPrincipalName") or "").lower()
                ):
                    updates["userPrincipalName"] = email
                    updates["mailNickname"] = email.split("@")[0]

                if updates:
                    updates["displayName"] = self._format_display_name(student)
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

    def _ensure_group(
        self, class_name: str, template: str, cache: dict[str, str]
    ) -> str | None:
        """Findet oder erstellt eine M365-Gruppe anhand des Templates."""
        if not class_name:
            return None

        cache_key = f"{template}|{class_name}"
        if cache_key in cache:
            return cache[cache_key]

        sanitized = _sanitize_nickname(class_name)
        nickname = template.replace("{k}", sanitized)
        display_name = nickname  # z.B. "10a_sus"

        # Existierende Gruppe suchen (serverseitiger Filter)
        found = self._graph.find_group_by_name(display_name)
        if found:
            cache[cache_key] = found["id"]
            return found["id"]

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
            cache[cache_key] = group_id
            return group_id
        except GraphApiError as exc:
            warnings.warn(
                f"Gruppe '{display_name}' konnte nicht erstellt werden: {exc}"
            )
            return None

    def _assign_to_class_group(
        self, user_id: str, class_name: str, student: dict
    ) -> None:
        """Fügt User zur SuS-Gruppe hinzu und pflegt KuK-Gruppe."""
        # --- SuS-Gruppe: Schüler als Member, Lehrer als Owner ---
        sus_id = self._ensure_group(
            class_name, self._group_sus_template, self._sus_cache
        )
        if sus_id:
            try:
                self._graph.add_member(sus_id, user_id)
            except GraphApiError as exc:
                if "already exist" not in str(exc).lower():
                    warnings.warn(f"SuS-Mitglied {user_id}: {exc}")

            # Klassenlehrer als Owner der SuS-Gruppe (best-effort)
            for teacher_field in ("class_teacher_1", "class_teacher_2"):
                teacher_name = student.get(teacher_field, "")
                if teacher_name:
                    self._add_teacher_to_group(sus_id, teacher_name, as_owner=True)

        # --- KuK-Gruppe: Lehrer als Member (einmal pro Klasse) ---
        if class_name not in self._kuk_processed:
            self._kuk_processed.add(class_name)
            kuk_id = self._ensure_group(
                class_name, self._group_kuk_template, self._kuk_cache
            )
            if kuk_id:
                for teacher_field in ("class_teacher_1", "class_teacher_2"):
                    teacher_name = student.get(teacher_field, "")
                    if teacher_name:
                        self._add_teacher_to_group(kuk_id, teacher_name, as_owner=False)

    def _add_teacher_to_group(
        self, group_id: str, teacher_name: str, *, as_owner: bool
    ) -> None:
        """Sucht den Lehrer per Nachname und fügt ihn zur Gruppe hinzu."""
        try:
            users = self._all_users or self._graph.list_users(self._domain)
            for u in users:
                surname = u.get("surname", "")
                if surname and surname.lower() == teacher_name.lower():
                    try:
                        if as_owner:
                            self._graph.add_owner(group_id, u["id"])
                        else:
                            self._graph.add_member(group_id, u["id"])
                    except GraphApiError as exc:
                        if "already exist" not in str(exc).lower():
                            warnings.warn(f"Lehrer {teacher_name}: {exc}")
                    return
        except Exception:
            pass

    def _move_between_groups(
        self, user_id: str, old_class: str, new_class: str, student: dict
    ) -> None:
        """Verschiebt einen User zwischen SuS-Gruppen bei Klassenwechsel."""
        # Aus alter SuS-Gruppe entfernen
        old_sus_id = self._ensure_group(
            old_class, self._group_sus_template, self._sus_cache
        )
        if old_sus_id:
            try:
                self._graph.remove_member(old_sus_id, user_id)
            except GraphApiError:
                pass

        # In neue SuS-Gruppe aufnehmen + KuK-Gruppe pflegen
        self._assign_to_class_group(user_id, new_class, student)

    def _collect_existing_emails(self) -> set[str]:
        """Sammelt alle existierenden Email-Adressen aus M365."""
        try:
            users = self._graph.list_users(self._domain)
            return {(u.get("userPrincipalName") or "").lower() for u in users}
        except GraphApiError:
            return set()

    # --- Gruppen-Sync (Compute + Apply getrennt für Preview) ---

    def _build_lookups(self) -> None:
        """Baut Lookup-Dicts für Schüler (employeeId/Email) und Lehrer (Nachname)."""
        if self._all_users is None:
            self._all_users = self._graph.list_users(self._domain)

        self._eid_to_uid: dict[str, str] = {}
        self._upn_to_uid: dict[str, str] = {}
        self._uid_to_name: dict[str, str] = {}
        for u in self._all_users:
            uid = u["id"]
            self._uid_to_name[uid] = u.get("displayName") or u.get("surname", uid)
            eid = u.get("employeeId")
            if eid:
                self._eid_to_uid[eid] = uid
            upn = (u.get("userPrincipalName") or "").lower()
            if upn:
                self._upn_to_uid[upn] = uid

        self._surname_to_uid: dict[str, str] = {}
        for u in self._all_users:
            surname = (u.get("surname") or "").strip().lower()
            if surname and surname not in self._surname_to_uid:
                self._surname_to_uid[surname] = u["id"]

    def _find_group(
        self, class_name: str, template: str, cache: dict[str, str]
    ) -> tuple[str | None, bool]:
        """Sucht eine Gruppe, erstellt sie NICHT. Returns: (group_id, is_new)."""
        if not class_name:
            return None, False

        cache_key = f"{template}|{class_name}"
        if cache_key in cache:
            return cache[cache_key], False

        # Wenn Gruppen bulk-geladen: Cache-Miss = Gruppe existiert nicht
        if self._groups_bulk_loaded:
            return None, True

        # Fallback: einzelne Abfrage (nur wenn kein Bulk-Load)
        sanitized = _sanitize_nickname(class_name)
        display_name = template.replace("{k}", sanitized)

        found = self._graph.find_group_by_name(display_name)
        if found:
            cache[cache_key] = found["id"]
            return found["id"], False

        return None, True  # Gruppe existiert nicht → is_new=True

    def _bulk_load_groups(self, class_names: set[str]) -> None:
        """Lädt alle relevanten Gruppen in einem Durchgang (statt pro Klasse).

        Extrahiert Prefix/Suffix aus den Templates und nutzt serverseitige
        Filter (startsWith) oder lädt alle Gruppen einmal und filtert lokal.
        """
        self._groups_bulk_loaded = False
        sus_prefix, sus_suffix = _extract_template_parts(self._group_sus_template)
        kuk_prefix, kuk_suffix = _extract_template_parts(self._group_kuk_template)

        all_groups: list[dict] = []

        if sus_prefix or kuk_prefix:
            # Serverseitig per startsWith filtern
            loaded_prefixes: set[str] = set()
            for prefix in (sus_prefix, kuk_prefix):
                if prefix and prefix not in loaded_prefixes:
                    all_groups.extend(self._graph.list_groups(prefix))
                    loaded_prefixes.add(prefix)
        elif sus_suffix or kuk_suffix:
            # Kein Prefix → alle Gruppen laden, client-seitig filtern
            all_groups = self._graph.list_all_groups()
            # Nur Gruppen behalten die zum Suffix passen
            filtered: list[dict] = []
            for g in all_groups:
                dn = (g.get("displayName") or "").lower()
                if sus_suffix and dn.endswith(sus_suffix.lower()):
                    filtered.append(g)
                elif kuk_suffix and dn.endswith(kuk_suffix.lower()):
                    filtered.append(g)
            all_groups = filtered
        else:
            # Template ist nur {k} → Warnung, Fallback auf Einzel-Abfragen
            warnings.warn(
                "Gruppen-Templates haben keinen Prefix/Suffix "
                "(z.B. '{k}_sus'). Lade Gruppen einzeln pro Klasse — "
                "das erzeugt mehr API-Abfragen."
            )
            return

        # displayName → group_id Lookup bauen
        name_to_id: dict[str, str] = {}
        for g in all_groups:
            dn = g.get("displayName", "")
            if dn:
                name_to_id[dn.lower()] = g["id"]

        # SuS- und KuK-Caches vorbelegen
        for class_name in class_names:
            sanitized = _sanitize_nickname(class_name)

            sus_name = self._group_sus_template.replace("{k}", sanitized)
            gid = name_to_id.get(sus_name.lower())
            if gid:
                self._sus_cache[f"{self._group_sus_template}|{class_name}"] = gid

            kuk_name = self._group_kuk_template.replace("{k}", sanitized)
            gid = name_to_id.get(kuk_name.lower())
            if gid:
                self._kuk_cache[f"{self._group_kuk_template}|{class_name}"] = gid

        self._groups_bulk_loaded = True

    def compute_group_diff(
        self, all_students: list[dict], teachers: list[dict]
    ) -> list[dict]:
        """Berechnet geplante Gruppenänderungen (SOLL vs IST) für die Vorschau."""
        self._build_lookups()

        # Schüler nach Klasse gruppieren
        classes: dict[str, list[dict]] = {}
        for s in all_students:
            cn = s.get("class_name", "")
            if cn:
                classes.setdefault(cn, []).append(s)

        # Gruppen einmal bulk-laden statt pro Klasse
        self._bulk_load_groups(set(classes.keys()))

        changes: list[dict] = []
        for class_name, class_students in sorted(classes.items()):
            changes.extend(self._diff_class_sus(class_name, class_students))
            changes.extend(self._diff_class_kuk(class_name, class_students))
        return changes

    def _diff_class_sus(self, class_name: str, students: list[dict]) -> list[dict]:
        """Berechnet Diff für eine SuS-Gruppe (ohne auszuführen)."""
        changes: list[dict] = []
        group_name = self._group_sus_template.replace(
            "{k}", _sanitize_nickname(class_name)
        )
        group_id, is_new = self._find_group(
            class_name, self._group_sus_template, self._sus_cache
        )

        if is_new:
            changes.append(
                {
                    "id": f"sus:{class_name}:create",
                    "group_type": "sus",
                    "group_name": group_name,
                    "group_id": "",
                    "action": "create_group",
                    "member_name": "",
                    "member_id": "",
                    "class_name": class_name,
                }
            )

        # SOLL: aktive Schüler dieser Klasse (employeeId → Fallback Email)
        expected_ids: set[str] = set()
        for s in students:
            uid = self._eid_to_uid.get(s.get("school_internal_id", ""))
            if not uid:
                email = (s.get("email") or "").lower()
                if email:
                    uid = self._upn_to_uid.get(email)
            if uid:
                expected_ids.add(uid)

        # IST: aktuelle Mitglieder (nur bei existierenden Gruppen)
        actual_ids: set[str] = set()
        if group_id:
            current_members = self._graph.get_members(group_id)
            actual_ids = {m["id"] for m in current_members}

        for uid in sorted(expected_ids - actual_ids):
            changes.append(
                {
                    "id": f"sus:{class_name}:add:{uid}",
                    "group_type": "sus",
                    "group_name": group_name,
                    "group_id": group_id or "",
                    "action": "add_member",
                    "member_name": self._uid_to_name.get(uid, uid),
                    "member_id": uid,
                    "class_name": class_name,
                }
            )

        for uid in sorted(actual_ids - expected_ids):
            changes.append(
                {
                    "id": f"sus:{class_name}:rm:{uid}",
                    "group_type": "sus",
                    "group_name": group_name,
                    "group_id": group_id or "",
                    "action": "remove_member",
                    "member_name": self._uid_to_name.get(uid, uid),
                    "member_id": uid,
                    "class_name": class_name,
                }
            )

        return changes

    def _diff_class_kuk(self, class_name: str, students: list[dict]) -> list[dict]:
        """Berechnet Diff für eine KuK-Gruppe (ohne auszuführen)."""
        changes: list[dict] = []
        group_name = self._group_kuk_template.replace(
            "{k}", _sanitize_nickname(class_name)
        )
        group_id, is_new = self._find_group(
            class_name, self._group_kuk_template, self._kuk_cache
        )

        if is_new:
            changes.append(
                {
                    "id": f"kuk:{class_name}:create",
                    "group_type": "kuk",
                    "group_name": group_name,
                    "group_id": "",
                    "action": "create_group",
                    "member_name": "",
                    "member_id": "",
                    "class_name": class_name,
                }
            )

        # SOLL: alle Lehrer dieser Klasse (Klassenlehrer + Fachlehrer)
        expected_teacher_names: set[str] = set()
        for s in students:
            for f in ("class_teacher_1", "class_teacher_2"):
                name = (s.get(f) or "").strip().lower()
                if name:
                    expected_teacher_names.add(name)
            for course in s.get("courses", []):
                if isinstance(course, dict):
                    name = (course.get("teacher_name") or "").strip().lower()
                else:
                    name = (course.teacher_name or "").strip().lower()
                if name:
                    expected_teacher_names.add(name)

        expected_ids: set[str] = set()
        for name in expected_teacher_names:
            uid = self._surname_to_uid.get(name)
            if uid:
                expected_ids.add(uid)

        # IST
        actual_ids: set[str] = set()
        if group_id:
            current_members = self._graph.get_members(group_id)
            actual_ids = {m["id"] for m in current_members}

        for uid in sorted(expected_ids - actual_ids):
            changes.append(
                {
                    "id": f"kuk:{class_name}:add:{uid}",
                    "group_type": "kuk",
                    "group_name": group_name,
                    "group_id": group_id or "",
                    "action": "add_member",
                    "member_name": self._uid_to_name.get(uid, uid),
                    "member_id": uid,
                    "class_name": class_name,
                }
            )

        for uid in sorted(actual_ids - expected_ids):
            changes.append(
                {
                    "id": f"kuk:{class_name}:rm:{uid}",
                    "group_type": "kuk",
                    "group_name": group_name,
                    "group_id": group_id or "",
                    "action": "remove_member",
                    "member_name": self._uid_to_name.get(uid, uid),
                    "member_id": uid,
                    "class_name": class_name,
                }
            )

        return changes

    def apply_group_changes(self, changes: list[dict]) -> list[dict]:
        """Führt die vom User ausgewählten Gruppenänderungen aus."""
        results: list[dict] = []

        for ch in changes:
            action = ch["action"]
            class_name = ch["class_name"]
            group_type = ch["group_type"]

            try:
                if action == "create_group":
                    template = (
                        self._group_sus_template
                        if group_type == "sus"
                        else self._group_kuk_template
                    )
                    cache = self._sus_cache if group_type == "sus" else self._kuk_cache
                    group_id = self._ensure_group(class_name, template, cache)
                    results.append(
                        {
                            "action": "create_group",
                            "group": ch["group_name"],
                            "success": bool(group_id),
                            "message": group_id or "Erstellung fehlgeschlagen",
                        }
                    )

                elif action == "add_member":
                    # Gruppe sicherstellen (falls gerade erst erstellt)
                    template = (
                        self._group_sus_template
                        if group_type == "sus"
                        else self._group_kuk_template
                    )
                    cache = self._sus_cache if group_type == "sus" else self._kuk_cache
                    group_id = self._ensure_group(class_name, template, cache)
                    if group_id:
                        self._graph.add_member(group_id, ch["member_id"])
                        results.append(
                            {
                                "action": "add_member",
                                "group": ch["group_name"],
                                "success": True,
                                "message": ch["member_name"],
                            }
                        )

                elif action == "remove_member":
                    group_id = ch.get("group_id")
                    if group_id:
                        self._graph.remove_member(group_id, ch["member_id"])
                        results.append(
                            {
                                "action": "remove_member",
                                "group": ch["group_name"],
                                "success": True,
                                "message": ch["member_name"],
                            }
                        )

            except GraphApiError as exc:
                if "already exist" not in str(exc).lower():
                    results.append(
                        {
                            "action": action,
                            "group": ch.get("group_name", ""),
                            "success": False,
                            "message": str(exc),
                        }
                    )

        # Klassenlehrer als Owner der SuS-Gruppen (best-effort)
        owner_processed: set[str] = set()
        for ch in changes:
            if ch["group_type"] == "sus" and ch["class_name"] not in owner_processed:
                owner_processed.add(ch["class_name"])
                sus_id = self._sus_cache.get(
                    f"{self._group_sus_template}|{ch['class_name']}"
                )
                if sus_id:
                    # Lehrer-Info aus den changes ableiten geht nicht direkt,
                    # daher aus _all_users per Nachname (wie bisher)
                    pass  # Owner werden in apply_new/apply_changes gesetzt

        return results


def _sanitize_nickname(text: str) -> str:
    """Bereinigt einen Klassennamen für mailNickname (a-z, 0-9, -, _)."""
    return re.sub(r"[^a-z0-9_\-]", "", text.lower())


def _extract_template_parts(template: str) -> tuple[str, str]:
    """Extrahiert Prefix und Suffix aus einem Gruppen-Template.

    '{k}_sus' → ('', '_sus')
    'grp_{k}' → ('grp_', '')
    '{k}'     → ('', '')
    """
    idx = template.find("{k}")
    if idx < 0:
        return template, ""
    return template[:idx], template[idx + 3 :]


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
