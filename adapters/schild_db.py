from __future__ import annotations

import warnings

from adapters.base import AdapterBase
from core.models import (
    ConfigField,
    CourseAssignment,
    StudentRecord,
    TeacherRecord,
)

# ---------------------------------------------------------------------------
# SQL-Queries — Tabellen-/Spaltennamen gegen echtes SchILD-Schema prüfen!
# ---------------------------------------------------------------------------

_SQL_STUDENTS = """
    SELECT
        s.ID                AS school_internal_id,
        s.Vorname           AS first_name,
        s.Name              AS last_name,
        s.Geburtsdatum      AS dob,
        s.SchulEmail        AS email,
        s.Klasse            AS class_name
    FROM Schueler s
    WHERE s.Status = 2
    ORDER BY s.Name, s.Vorname
"""

_SQL_CLASS_TEACHERS = """
    SELECT
        k.Klasse            AS class_name,
        kl1.Nachname        AS teacher_1,
        kl2.Nachname        AS teacher_2
    FROM Klassen k
    LEFT JOIN K_Lehrer kl1 ON k.KlassenLehrer = kl1.Kuerzel
    LEFT JOIN K_Lehrer kl2 ON k.StvKlassenLehrer = kl2.Kuerzel
"""

_SQL_COURSES = """
    SELECT
        ld.Schueler_ID      AS student_id,
        f.Bezeichnung       AS course_name,
        kl.Nachname         AS teacher_name,
        ld.Kurs_ID          AS course_id
    FROM SchuelerLeistungsdaten ld
    LEFT JOIN EigeneSchule_Faecher f ON ld.Fach_ID = f.ID
    LEFT JOIN K_Lehrer kl ON ld.FachLehrer = kl.Kuerzel
    WHERE ld.Abschnitt_ID IN (
        SELECT ID FROM SchuelerLernabschnittsdaten
        WHERE Jahr = YEAR(CURDATE()) AND Abschnitt = 1
    )
"""

_SQL_TEACHERS = """
    SELECT
        kl.Vorname          AS first_name,
        kl.Nachname         AS last_name,
        kl.Geburtsdatum     AS dob,
        kl.Amtsbezeichnung  AS job_title
    FROM K_Lehrer kl
    WHERE kl.Sichtbar = '+'
    ORDER BY kl.Nachname, kl.Vorname
"""

_SQL_WRITE_BACK_EMAIL = """
    UPDATE Schueler
    SET SchulEmail = %s
    WHERE ID = %s
"""


class SchildDbAdapter(AdapterBase):
    """Liest Schülerdaten direkt aus der SchILD-Datenbank (MariaDB/ODBC)."""

    def __init__(
        self,
        db_host: str,
        db_port: str,
        db_name: str,
        db_user: str,
        db_password: str,
    ) -> None:
        self.db_host = db_host
        self.db_port = db_port or "3306"
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password

    # --- Metadaten ---

    @classmethod
    def adapter_name(cls) -> str:
        return "SchILD Datenbank (ODBC)"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="db_host",
                label="Server",
                placeholder="localhost",
            ),
            ConfigField(
                key="db_port",
                label="Port",
                placeholder="3306",
                default="3306",
            ),
            ConfigField(
                key="db_name",
                label="Datenbank",
                placeholder="schild_nrw",
            ),
            ConfigField(
                key="db_user",
                label="Benutzer",
                placeholder="schild",
            ),
            ConfigField(
                key="db_password",
                label="Passwort",
                field_type="password",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> SchildDbAdapter:
        return cls(
            db_host=config.get("db_host", ""),
            db_port=config.get("db_port", "3306"),
            db_name=config.get("db_name", ""),
            db_user=config.get("db_user", ""),
            db_password=config.get("db_password", ""),
        )

    # --- Verbindung ---

    def _connect(self):
        """Stellt eine ODBC-Verbindung zur SchILD-DB her."""
        try:
            import pyodbc
        except ImportError as exc:
            raise ImportError(
                "pyodbc ist nicht installiert. Bitte installieren: pip install pyodbc"
            ) from exc

        conn_str = (
            f"DRIVER={{MariaDB ODBC 3.1 Driver}};"
            f"SERVER={self.db_host};"
            f"PORT={self.db_port};"
            f"DATABASE={self.db_name};"
            f"UID={self.db_user};"
            f"PWD={self.db_password};"
            f"CHARSET=utf8mb4;"
        )
        return pyodbc.connect(conn_str)

    def test_connection(self) -> tuple[bool, str]:
        """Testet die Verbindung und gibt Schülerzahl zurück."""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM Schueler WHERE Status = 2")
            count = cursor.fetchone()[0]
            conn.close()
            return (True, f"Verbunden. {count} aktive Schüler gefunden.")
        except Exception as exc:
            return (False, f"Verbindungsfehler: {exc}")

    # --- Interface ---

    def load(self) -> list[StudentRecord]:
        conn = self._connect()
        cursor = conn.cursor()

        # 1. Schüler laden
        cursor.execute(_SQL_STUDENTS)
        columns = [col[0] for col in cursor.description]
        raw_students = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # 2. Klassenlehrer laden
        cursor.execute(_SQL_CLASS_TEACHERS)
        ct_cols = [col[0] for col in cursor.description]
        class_teachers: dict[str, dict] = {}
        for row in cursor.fetchall():
            ct = dict(zip(ct_cols, row))
            class_teachers[ct["class_name"]] = ct

        # 3. Kurszuordnungen laden
        cursor.execute(_SQL_COURSES)
        course_cols = [col[0] for col in cursor.description]
        courses_by_student: dict[str, list[CourseAssignment]] = {}
        for row in cursor.fetchall():
            c = dict(zip(course_cols, row))
            sid = str(c["student_id"])
            assignment = CourseAssignment(
                course_name=c.get("course_name", "") or "",
                teacher_name=c.get("teacher_name", "") or "",
                course_id=str(c.get("course_id", "") or ""),
            )
            courses_by_student.setdefault(sid, []).append(assignment)

        conn.close()

        # 4. Zusammenbauen
        students: list[StudentRecord] = []
        skipped = 0

        for raw in raw_students:
            sid = str(raw.get("school_internal_id", "")).strip()
            if not sid:
                skipped += 1
                continue

            ct = class_teachers.get(raw.get("class_name", ""), {})
            dob = self._format_date(raw.get("dob"))

            students.append(
                StudentRecord(
                    school_internal_id=sid,
                    first_name=(raw.get("first_name") or "").strip(),
                    last_name=(raw.get("last_name") or "").strip(),
                    dob=dob,
                    email=(raw.get("email") or "").strip(),
                    class_name=(raw.get("class_name") or "").strip(),
                    class_teacher_1=(ct.get("teacher_1") or "").strip(),
                    class_teacher_2=(ct.get("teacher_2") or "").strip(),
                    courses=courses_by_student.get(sid, []),
                )
            )

        if skipped > 0:
            warnings.warn(f"{skipped} Schüler-Zeilen ohne ID übersprungen.")

        return students

    def load_teachers(self) -> list[TeacherRecord]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(_SQL_TEACHERS)
        columns = [col[0] for col in cursor.description]

        teachers: list[TeacherRecord] = []
        for row in cursor.fetchall():
            raw = dict(zip(columns, row))
            last_name = (raw.get("last_name") or "").strip()
            dob = self._format_date(raw.get("dob"))
            if not last_name or not dob:
                continue
            teachers.append(
                TeacherRecord(
                    first_name=(raw.get("first_name") or "").strip(),
                    last_name=last_name,
                    dob=dob,
                    job_title=(raw.get("job_title") or "").strip(),
                )
            )

        conn.close()
        return teachers

    # --- Write-back ---

    def supports_write_back(self) -> bool:
        return True

    def write_back(self, updates: list[dict]) -> list[dict]:
        """Schreibt Daten zurück (z.B. generierte Emails).

        updates: [{"school_internal_id": "123", "email": "m.mueller@schule.de"}]
        """
        conn = self._connect()
        cursor = conn.cursor()
        results: list[dict] = []

        for update in updates:
            sid = update.get("school_internal_id", "")
            try:
                if "email" in update:
                    cursor.execute(_SQL_WRITE_BACK_EMAIL, (update["email"], sid))
                results.append(
                    {"school_internal_id": sid, "success": True, "message": ""}
                )
            except Exception as exc:
                results.append(
                    {
                        "school_internal_id": sid,
                        "success": False,
                        "message": str(exc),
                    }
                )

        conn.commit()
        conn.close()
        return results

    # --- Hilfsmethoden ---

    @staticmethod
    def _format_date(value) -> str:
        """Konvertiert DB-Datumswert nach ISO-String (YYYY-MM-DD)."""
        if value is None:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        s = str(value).strip()
        if "." in s:
            parts = s.split(".")
            if len(parts) == 3:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return s
