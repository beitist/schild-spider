"""Moodle-Plugin — Schüler-Accounts und Kurse in Moodle verwalten (REST API)."""

from __future__ import annotations

import hashlib
import re
import warnings

from core.moodle_client import MoodleApiError, MoodleClient
from core.models import ConfigField
from plugins.base import PluginBase


class MoodlePlugin(PluginBase):
    """Output-Plugin für Moodle (Web Services REST API, SSO via auth=oidc)."""

    def __init__(
        self,
        moodle_url: str,
        token: str,
        parent_category_id: int,
        course_shortname_template: str,
        course_fullname_template: str,
        role_student: int,
        role_teacher: int,
        template_course_id: int,
    ) -> None:
        self._moodle = MoodleClient(moodle_url, token)
        self._parent_category_id = parent_category_id
        self._shortname_tpl = course_shortname_template or "{k} {f} [{l}]"
        self._fullname_tpl = course_fullname_template or "{k} {f} [{l}]"
        self._role_student = role_student or 5
        self._role_teacher = role_teacher or 3
        self._template_course_id = template_course_id or 0

        # Caches (pro Lauf)
        self._all_moodle_users: list[dict] | None = None
        self._idnumber_to_user: dict[str, dict] = {}
        self._surname_to_user: dict[str, dict] = {}
        self._category_cache: dict[str, int] = {}  # name → id
        self._course_cache: dict[str, dict] = {}  # idnumber → course

    # --- Metadaten ---

    @classmethod
    def plugin_name(cls) -> str:
        return "Moodle"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="moodle_url",
                label="Moodle URL",
                field_type="url",
                placeholder="https://moodle.schule.de",
            ),
            ConfigField(
                key="token",
                label="Web Service Token",
                field_type="password",
            ),
            ConfigField(
                key="parent_category_id",
                label="Eltern-Kategorie-ID (0 = Top-Level)",
                placeholder="0",
                default="0",
                required=False,
            ),
            ConfigField(
                key="course_shortname_template",
                label="Kursname kurz ({k}=Klasse, {f}=Fach, {l}=Lehrer)",
                placeholder="{k} {f} [{l}]",
                default="{k} {f} [{l}]",
            ),
            ConfigField(
                key="course_fullname_template",
                label="Kursname lang ({k}=Klasse, {f}=Fach, {l}=Lehrer)",
                placeholder="{k} {f} [{l}]",
                default="{k} {f} [{l}]",
            ),
            ConfigField(
                key="role_student",
                label="Rollen-ID Teilnehmer/in",
                placeholder="5",
                default="5",
                required=False,
            ),
            ConfigField(
                key="role_teacher",
                label="Rollen-ID Trainer/in",
                placeholder="3",
                default="3",
                required=False,
            ),
            ConfigField(
                key="template_course_id",
                label="Vorlage-Kurs ID (0 = keine Vorlage)",
                placeholder="0",
                default="0",
                required=False,
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> MoodlePlugin:
        return cls(
            moodle_url=config.get("moodle_url", ""),
            token=config.get("token", ""),
            parent_category_id=int(config.get("parent_category_id", 0) or 0),
            course_shortname_template=config.get(
                "course_shortname_template", "{k} {f} [{l}]"
            ),
            course_fullname_template=config.get(
                "course_fullname_template", "{k} {f} [{l}]"
            ),
            role_student=int(config.get("role_student", 5) or 5),
            role_teacher=int(config.get("role_teacher", 3) or 3),
            template_course_id=int(config.get("template_course_id", 0) or 0),
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            info = self._moodle.get_site_info()
            site = info.get("sitename", "?")
            user = info.get("fullname", "?")
            return True, f"Verbunden: {site} (als {user})"
        except MoodleApiError as exc:
            return False, f"Moodle API Fehler: {exc}"
        except Exception as exc:
            return False, f"Verbindungsfehler: {exc}"

    # --- Lookups aufbauen ---

    def _load_users(self) -> None:
        """Lädt alle Moodle-User und baut Lookup-Dicts."""
        if self._all_moodle_users is not None:
            return

        # Alle User mit gesetztem idnumber (= unsere Schüler)
        # Plus alle User für Lehrer-Nachname-Suche
        # Moodle hat kein "get all users" — wir nutzen Kriterien
        # Trick: leerer Suchstring liefert alle (je nach Moodle-Config)
        try:
            self._all_moodle_users = self._moodle.get_users(
                criteria=[{"key": "email", "value": "%"}]
            )
        except MoodleApiError:
            # Fallback: auth=oidc User (unsere Schüler)
            self._all_moodle_users = self._moodle.get_users(
                criteria=[{"key": "auth", "value": "oidc"}]
            )

        self._idnumber_to_user = {}
        self._surname_to_user = {}
        for u in self._all_moodle_users:
            idnum = (u.get("idnumber") or "").strip()
            if idnum:
                self._idnumber_to_user[idnum] = u
            surname = (u.get("lastname") or "").strip().lower()
            if surname and surname not in self._surname_to_user:
                self._surname_to_user[surname] = u

    def _load_categories(self) -> None:
        """Lädt Kategorien und baut name→id Cache."""
        if self._category_cache:
            return
        try:
            cats = self._moodle.get_categories()
            for c in cats:
                self._category_cache[c["name"]] = c["id"]
        except MoodleApiError as exc:
            warnings.warn(f"Kategorien laden: {exc}")

    # --- Sync-Interface (Schüler-Accounts) ---

    def get_manifest(self) -> list[dict]:
        self._load_users()

        manifest: list[dict] = []
        for u in self._all_moodle_users or []:
            idnum = (u.get("idnumber") or "").strip()
            if not idnum:
                continue
            student_dict = {
                "first_name": u.get("firstname", ""),
                "last_name": u.get("lastname", ""),
                "class_name": u.get("department", ""),
                "email": u.get("email", ""),
            }
            manifest.append(
                {
                    "school_internal_id": idnum,
                    "data_hash": self.compute_data_hash(student_dict),
                    "is_active": not u.get("suspended", False),
                }
            )
        return manifest

    def compute_data_hash(self, student: dict) -> str:
        parts = "|".join(
            [
                student.get("first_name", "").lower(),
                student.get("last_name", "").lower(),
                student.get("class_name", "").lower(),
                student.get("email", "").lower(),
            ]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def apply_new(self, students: list[dict]) -> list[dict]:
        results: list[dict] = []
        for student in students:
            sid = student["school_internal_id"]
            email = (student.get("email") or "").strip()

            if not email:
                warnings.warn(
                    f"Schüler {sid} ({student.get('last_name', '')}) "
                    f"hat keine Email — Moodle-Konto kann nicht erstellt werden"
                )
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": "Keine Email vorhanden (erst M365-Sync?)",
                    }
                )
                continue

            try:
                user_data = {
                    "username": email.lower(),
                    "auth": "oidc",
                    "firstname": student.get("first_name", ""),
                    "lastname": student.get("last_name", ""),
                    "email": email,
                    "idnumber": sid,
                    "department": student.get("class_name", ""),
                }

                created = self._moodle.create_users(users=[user_data])
                moodle_id = created[0]["id"] if created else None

                results.append(
                    {
                        "school_internal_id": sid,
                        "success": True,
                        "message": f"Moodle-ID: {moodle_id}",
                    }
                )

            except MoodleApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    def apply_changes(self, students: list[dict]) -> list[dict]:
        self._load_users()
        results: list[dict] = []

        for student in students:
            sid = student["school_internal_id"]
            moodle_user = self._idnumber_to_user.get(sid)

            if not moodle_user:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": f"Moodle-User mit idnumber={sid} nicht gefunden",
                    }
                )
                continue

            try:
                updates: dict = {"id": moodle_user["id"]}
                changed = False

                if student.get("first_name", "") != moodle_user.get("firstname", ""):
                    updates["firstname"] = student["first_name"]
                    changed = True
                if student.get("last_name", "") != moodle_user.get("lastname", ""):
                    updates["lastname"] = student["last_name"]
                    changed = True
                if student.get("class_name", "") != moodle_user.get("department", ""):
                    updates["department"] = student["class_name"]
                    changed = True

                email = (student.get("email") or "").strip()
                if email and email.lower() != (moodle_user.get("email") or "").lower():
                    updates["email"] = email
                    changed = True

                if changed:
                    self._moodle.update_users(users=[updates])

                results.append(
                    {"school_internal_id": sid, "success": True, "message": ""}
                )

            except MoodleApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        self._load_users()
        results: list[dict] = []

        for sid in school_internal_ids:
            moodle_user = self._idnumber_to_user.get(sid)
            if not moodle_user:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": f"Moodle-User mit idnumber={sid} nicht gefunden",
                    }
                )
                continue

            try:
                self._moodle.update_users(
                    users=[{"id": moodle_user["id"], "suspended": 1}]
                )
                results.append(
                    {"school_internal_id": sid, "success": True, "message": ""}
                )
            except MoodleApiError as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results

    # --- Kurs-Sync (Compute + Apply getrennt für Preview) ---

    def _format_course_name(
        self, template: str, class_name: str, course_name: str, teacher_name: str
    ) -> str:
        """Formatiert einen Kursnamen nach Template."""
        return (
            template.replace("{k}", class_name)
            .replace("{f}", course_name)
            .replace("{l}", teacher_name)
            .strip()
        )

    @staticmethod
    def _make_idnumber(class_name: str, course_name: str, teacher_name: str) -> str:
        """Erzeugt einen eindeutigen idnumber für einen Kurs."""
        raw = f"{class_name}-{course_name}-{teacher_name}"
        return re.sub(r"[^a-z0-9_\-]", "", raw.lower())

    def compute_group_diff(
        self, all_students: list[dict], teachers: list[dict]
    ) -> list[dict]:
        """Berechnet geplante Kurs-/Einschreibungsänderungen (SOLL vs IST)."""
        self._load_users()
        self._load_categories()

        changes: list[dict] = []

        # Kurse aus Schüler-Daten sammeln: {(klasse, fach, lehrer) → [schüler_ids]}
        course_map: dict[tuple[str, str, str], set[str]] = {}
        for s in all_students:
            class_name = s.get("class_name", "")
            sid = s.get("school_internal_id", "")
            if not class_name or not sid:
                continue
            for course in s.get("courses", []):
                if isinstance(course, dict):
                    c_name = course.get("course_name", "")
                    t_name = course.get("teacher_name", "")
                else:
                    c_name = course.course_name
                    t_name = course.teacher_name
                if c_name and t_name:
                    key = (class_name, c_name, t_name)
                    course_map.setdefault(key, set()).add(sid)

        if not course_map:
            return changes

        # Klassen sammeln für Kategorie-Check
        classes_needed: set[str] = {k[0] for k in course_map}

        # Kategorie-Änderungen
        for class_name in sorted(classes_needed):
            if class_name not in self._category_cache:
                changes.append(
                    {
                        "id": f"cat:{_sanitize(class_name)}:create",
                        "group_type": "category",
                        "group_name": class_name,
                        "group_id": "",
                        "action": "create_group",
                        "member_name": "",
                        "member_id": "",
                        "class_name": class_name,
                        "parent_category_id": self._parent_category_id,
                        "display_text": "Kategorie anlegen",
                        "display_detail": class_name,
                    }
                )

        # Kurs- und Einschreibungs-Änderungen
        for (class_name, course_name, teacher_name), student_ids in sorted(
            course_map.items()
        ):
            idnumber = self._make_idnumber(class_name, course_name, teacher_name)
            shortname = self._format_course_name(
                self._shortname_tpl, class_name, course_name, teacher_name
            )
            fullname = self._format_course_name(
                self._fullname_tpl, class_name, course_name, teacher_name
            )

            # Kurs in Moodle suchen
            existing_course = self._course_cache.get(idnumber)
            if not existing_course:
                try:
                    found = self._moodle.get_courses_by_field("idnumber", idnumber)
                    if found:
                        existing_course = found[0]
                        self._course_cache[idnumber] = existing_course
                except MoodleApiError:
                    pass

            course_id = existing_course["id"] if existing_course else None
            category_id = self._category_cache.get(class_name, 0)

            if not existing_course:
                # Kurs erstellen
                changes.append(
                    {
                        "id": f"course:{idnumber}:create",
                        "group_type": "course",
                        "group_name": shortname,
                        "group_id": "",
                        "action": "create_group",
                        "member_name": "",
                        "member_id": "",
                        "class_name": shortname,
                        "course_idnumber": idnumber,
                        "course_shortname": shortname,
                        "course_fullname": fullname,
                        "category_name": class_name,
                        "category_id": category_id,
                        "display_text": "Kurs anlegen",
                        "display_detail": "",
                    }
                )

            # Einschreibungen prüfen
            enrolled_ids: set[int] = set()
            enrolled_teacher_ids: set[int] = set()
            if course_id:
                try:
                    enrolled = self._moodle.get_enrolled_users(course_id)
                    for eu in enrolled:
                        roles = [r.get("roleid") for r in eu.get("roles", [])]
                        if self._role_teacher in roles:
                            enrolled_teacher_ids.add(eu["id"])
                        if self._role_student in roles:
                            enrolled_ids.add(eu["id"])
                except MoodleApiError as exc:
                    warnings.warn(f"Einschreibungen für {shortname}: {exc}")

            # Lehrer-Einschreibung prüfen
            teacher_surname = teacher_name.strip().lower()
            teacher_user = self._surname_to_user.get(teacher_surname)
            if teacher_user:
                teacher_moodle_id = teacher_user["id"]
                if teacher_moodle_id not in enrolled_teacher_ids:
                    changes.append(
                        {
                            "id": f"course:{idnumber}:enrol-t:{teacher_moodle_id}",
                            "group_type": "course",
                            "group_name": shortname,
                            "group_id": str(course_id or ""),
                            "action": "add_member",
                            "member_name": teacher_name,
                            "member_id": str(teacher_moodle_id),
                            "class_name": shortname,
                            "course_id": course_id,
                            "course_idnumber": idnumber,
                            "role_id": self._role_teacher,
                            "display_text": teacher_name,
                            "display_detail": "Trainer einschreiben",
                        }
                    )
            else:
                if teacher_surname:
                    warnings.warn(
                        f"Lehrer '{teacher_name}' nicht in Moodle gefunden — "
                        f"kann nicht als Trainer für {shortname} eingeschrieben werden"
                    )

            # Schüler-Einschreibungen (SOLL vs IST)
            for sid in sorted(student_ids):
                student_user = self._idnumber_to_user.get(sid)
                if not student_user:
                    continue  # Schüler hat noch kein Moodle-Konto
                student_moodle_id = student_user["id"]
                student_name = (
                    f"{student_user.get('lastname', '')}, "
                    f"{student_user.get('firstname', '')}"
                )

                if student_moodle_id not in enrolled_ids:
                    changes.append(
                        {
                            "id": f"course:{idnumber}:enrol:{student_moodle_id}",
                            "group_type": "course",
                            "group_name": shortname,
                            "group_id": str(course_id or ""),
                            "action": "add_member",
                            "member_name": student_name,
                            "member_id": str(student_moodle_id),
                            "class_name": shortname,
                            "course_id": course_id,
                            "course_idnumber": idnumber,
                            "role_id": self._role_student,
                            "display_text": student_name,
                            "display_detail": "einschreiben",
                        }
                    )

            # Überzählige Schüler abmelden (nur bei bestehenden Kursen)
            if course_id:
                expected_moodle_ids: set[int] = set()
                for sid in student_ids:
                    su = self._idnumber_to_user.get(sid)
                    if su:
                        expected_moodle_ids.add(su["id"])

                for extra_id in sorted(enrolled_ids - expected_moodle_ids):
                    # Name auflösen
                    extra_name = str(extra_id)
                    for u in self._all_moodle_users or []:
                        if u["id"] == extra_id:
                            extra_name = (
                                f"{u.get('lastname', '')}, {u.get('firstname', '')}"
                            )
                            break

                    changes.append(
                        {
                            "id": f"course:{idnumber}:unenrol:{extra_id}",
                            "group_type": "course",
                            "group_name": shortname,
                            "group_id": str(course_id),
                            "action": "remove_member",
                            "member_name": extra_name,
                            "member_id": str(extra_id),
                            "class_name": shortname,
                            "course_id": course_id,
                            "course_idnumber": idnumber,
                            "display_text": extra_name,
                            "display_detail": "abmelden",
                        }
                    )

        return changes

    def apply_group_changes(self, changes: list[dict]) -> list[dict]:
        """Führt die vom User ausgewählten Kurs-/Einschreibungsänderungen aus."""
        self._load_categories()
        results: list[dict] = []

        for ch in changes:
            action = ch["action"]
            group_type = ch.get("group_type", "")

            try:
                if group_type == "category" and action == "create_group":
                    # Kategorie erstellen
                    cat_name = ch["group_name"]
                    parent_id = ch.get("parent_category_id", self._parent_category_id)
                    created = self._moodle.create_categories(
                        categories=[{"name": cat_name, "parent": parent_id}]
                    )
                    if created:
                        self._category_cache[cat_name] = created[0]["id"]
                    results.append(
                        {
                            "action": "create_category",
                            "group": cat_name,
                            "success": True,
                            "message": f"ID: {created[0]['id']}" if created else "",
                        }
                    )

                elif group_type == "course" and action == "create_group":
                    # Kurs erstellen
                    cat_name = ch.get("category_name", "")
                    cat_id = self._category_cache.get(
                        cat_name, ch.get("category_id", 0)
                    )

                    course_data: dict = {
                        "fullname": ch.get("course_fullname", ch["group_name"]),
                        "shortname": ch.get("course_shortname", ch["group_name"]),
                        "categoryid": cat_id or 1,  # 1 = Misc als Fallback
                        "idnumber": ch["course_idnumber"],
                    }

                    created = self._moodle.create_courses(courses=[course_data])
                    if created:
                        self._course_cache[ch["course_idnumber"]] = created[0]
                    results.append(
                        {
                            "action": "create_course",
                            "group": ch["group_name"],
                            "success": True,
                            "message": f"ID: {created[0]['id']}" if created else "",
                        }
                    )

                elif action == "add_member":
                    # Einschreibung
                    course_id = ch.get("course_id")
                    if not course_id:
                        # Kurs wurde evtl. gerade erst erstellt
                        cached = self._course_cache.get(ch.get("course_idnumber", ""))
                        course_id = cached["id"] if cached else None

                    if not course_id:
                        # Nochmal in Moodle suchen
                        try:
                            found = self._moodle.get_courses_by_field(
                                "idnumber", ch.get("course_idnumber", "")
                            )
                            if found:
                                course_id = found[0]["id"]
                        except MoodleApiError:
                            pass

                    if course_id:
                        role_id = ch.get("role_id", self._role_student)
                        self._moodle.enrol_users(
                            enrolments=[
                                {
                                    "roleid": role_id,
                                    "userid": int(ch["member_id"]),
                                    "courseid": int(course_id),
                                }
                            ]
                        )
                        results.append(
                            {
                                "action": "enrol",
                                "group": ch["group_name"],
                                "success": True,
                                "message": ch["member_name"],
                            }
                        )
                    else:
                        results.append(
                            {
                                "action": "enrol",
                                "group": ch["group_name"],
                                "success": False,
                                "message": f"Kurs nicht gefunden: {ch.get('course_idnumber', '')}",
                            }
                        )

                elif action == "remove_member":
                    # Abmeldung
                    course_id = ch.get("course_id")
                    if course_id:
                        self._moodle.unenrol_users(
                            enrolments=[
                                {
                                    "userid": int(ch["member_id"]),
                                    "courseid": int(course_id),
                                }
                            ]
                        )
                        results.append(
                            {
                                "action": "unenrol",
                                "group": ch["group_name"],
                                "success": True,
                                "message": ch["member_name"],
                            }
                        )

            except MoodleApiError as exc:
                results.append(
                    {
                        "action": action,
                        "group": ch.get("group_name", ""),
                        "success": False,
                        "message": str(exc),
                    }
                )

        return results


def _sanitize(text: str) -> str:
    """Bereinigt Text für IDs (a-z, 0-9, -, _)."""
    return re.sub(r"[^a-z0-9_\-]", "", text.lower())
