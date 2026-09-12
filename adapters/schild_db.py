from __future__ import annotations

import atexit
import logging
import shutil
import tempfile
import warnings
from pathlib import Path

from adapters.base import AdapterBase
from core.models import (
    ConfigField,
    CourseAssignment,
    StudentRecord,
    TeacherRecord,
)

log = logging.getLogger(__name__)

# Fotos aus der DB werden hier zwischengespeichert (statt verstreuter,
# nie gelöschter NamedTemporaryFiles). Wird bei jedem load() geleert
# und beim Programmende komplett entfernt (Datenschutz).
_PHOTO_TMP_DIR = Path(tempfile.gettempdir()) / "schild-spider-photos"


def _cleanup_photo_dir() -> None:
    shutil.rmtree(_PHOTO_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup_photo_dir)

# ---------------------------------------------------------------------------
# SQL-Queries — basierend auf SchILD-NRW-Schema (MariaDB)
# ---------------------------------------------------------------------------

_SQL_EIGENESCHULE = """
    SELECT Schuljahr, SchuljahrAbschnitt
    FROM eigeneschule
    LIMIT 1
"""

_SQL_STUDENTS = """
    SELECT
        s.ID                AS school_internal_id,
        s.Vorname           AS first_name,
        s.Name              AS last_name,
        s.Geburtsdatum      AS dob,
        s.SchulEmail        AS email,
        s.Klasse            AS class_name,
        s.Geschlecht        AS gender
    FROM schueler s
    WHERE s.Status = 2
    ORDER BY s.Name, s.Vorname
"""

# Klassenlehrer kommen aus der Tabelle "versetzung" (Klassen-Tabelle).
# Eine Zeile pro Klasse, NICHT pro Schüler.
# KlassenlehrerKrz / StvKlassenlehrerKrz → k_lehrer.Kuerzel
_SQL_CLASS_TEACHERS = """
    SELECT
        v.Klasse              AS class_name,
        v.KlassenlehrerKrz    AS teacher_1_krz,
        kl1.Nachname          AS teacher_1,
        kl1.EMailDienstlich   AS teacher_1_email,
        v.StvKlassenlehrerKrz AS teacher_2_krz,
        kl2.Nachname          AS teacher_2,
        kl2.EMailDienstlich   AS teacher_2_email
    FROM versetzung v
    LEFT JOIN k_lehrer kl1 ON v.KlassenlehrerKrz = kl1.Kuerzel
    LEFT JOIN k_lehrer kl2 ON v.StvKlassenlehrerKrz = kl2.Kuerzel
"""

# Leistungsdaten — Zwei-Schritt-Verfahren:
# 1. Lernabschnitt-IDs für das Schuljahr/Halbjahr ermitteln
# 2. Leistungsdaten (Fächer/Kurse) über Abschnitt_ID laden

_SQL_ABSCHNITT_IDS = """
    SELECT
        la.ID           AS abschnitt_id,
        la.Schueler_ID  AS student_id
    FROM schuelerlernabschnittsdaten la
    WHERE la.Jahr = %s AND la.Abschnitt = %s
"""

_SQL_LEISTUNGSDATEN = """
    SELECT
        ld.Abschnitt_ID                   AS abschnitt_id,
        ld.FachLehrer                     AS fachlehrer_krz,
        f.Zeugnisbez                      AS course_name,
        kl.Nachname                       AS teacher_name,
        kl.EMailDienstlich                AS fachlehrer_email,
        ld.Kurs_ID                        AS kurs_id,
        ku.KurzBez                        AS kurs_bezeichnung,
        ku.Zeugnisbez                     AS kurs_zeugnisbez,
        kl2.Nachname                      AS kurs_teacher_name,
        ld.KursartAllg                    AS kursart
    FROM schuelerleistungsdaten ld
    LEFT JOIN eigeneschule_faecher f   ON ld.Fach_ID = f.ID
    LEFT JOIN k_lehrer kl              ON ld.FachLehrer = kl.Kuerzel
    LEFT JOIN Kurse ku                 ON ld.Kurs_ID = ku.ID
    LEFT JOIN k_lehrer kl2             ON ku.LehrerKrz = kl2.Kuerzel
    WHERE ld.Abschnitt_ID IN ({placeholders})
"""

# Statistik = '+' ist das Pflege-Feld, über das die Schule steuert wer in den
# Sync kommt. PersonTyp (LEHRKRAFT / PERSONAL) wird optional ergänzt —
# siehe Adapter-Config "person_typ".
_SQL_TEACHERS = """
    SELECT
        kl.ID                 AS teacher_id,
        kl.Kuerzel            AS kuerzel,
        kl.Vorname            AS first_name,
        kl.Nachname           AS last_name,
        kl.Geburtsdatum       AS dob,
        kl.Amtsbezeichnung    AS job_title,
        kl.EMailDienstlich    AS email
    FROM k_lehrer kl
    WHERE kl.Statistik = '+'
    {person_typ_filter}
    ORDER BY kl.Nachname, kl.Vorname
"""

_SQL_PHOTOS = """
    SELECT
        sf.Schueler_ID        AS student_id,
        sf.Foto               AS photo_blob
    FROM schuelerfotos sf
    WHERE sf.Schueler_ID IN ({placeholders})
"""

# Kategorie-Hierarchie pro Klasse: Abteilung, Fachklasse, Schulgliederung (BKIndexTyp)
# versetzung.Fachklasse_ID → eigeneschule_fachklassen (Bezeichnung, BKIndexTyp)
# eigeneschule_abt_kl.Klasse → eigeneschule_abteilungen (Bezeichnung)
_SQL_CLASS_HIERARCHY = """
    SELECT
        v.Klasse                     AS class_name,
        fk.Bezeichnung               AS fachklasse,
        fk.BKIndexTyp                AS schulgliederung,
        abt.Bezeichnung              AS abteilung
    FROM versetzung v
    LEFT JOIN eigeneschule_fachklassen fk ON v.Fachklasse_ID = fk.ID
    LEFT JOIN eigeneschule_abt_kl ak     ON ak.Klasse = v.Klasse
                                          AND ak.Sichtbar = '+'
    LEFT JOIN eigeneschule_abteilungen abt ON ak.Abteilung_ID = abt.ID
                                              AND abt.Sichtbar = '+'
"""

_SQL_WRITE_BACK_EMAIL = """
    UPDATE schueler
    SET SchulEmail = %s
    WHERE ID = %s
"""


class SchildDbAdapter(AdapterBase):
    """Liest Schülerdaten direkt aus der SchILD-Datenbank (MariaDB)."""

    def __init__(
        self,
        db_host: str,
        db_port: str,
        db_name: str,
        db_user: str,
        db_password: str,
        schuljahr: str,
        abschnitt: str,
        person_typ: str = "",
    ) -> None:
        self.db_host = db_host
        self.db_port = db_port or "3306"
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password
        self.schuljahr = schuljahr
        self.abschnitt = abschnitt or "1"
        self.person_typ = (person_typ or "").strip()

    # --- Metadaten ---

    @classmethod
    def adapter_name(cls) -> str:
        return "SchILD Datenbank (MariaDB)"

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
            ConfigField(
                key="schuljahr",
                label="Schuljahr",
                placeholder="2025",
            ),
            ConfigField(
                key="abschnitt",
                label="Halbjahr (Abschnitt)",
                placeholder="1",
                default="1",
            ),
            ConfigField(
                key="person_typ",
                label="Lehrer: PersonTyp-Filter",
                placeholder="leer = alle (z.B. LEHRKRAFT)",
                required=False,
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
            schuljahr=config.get("schuljahr", ""),
            abschnitt=config.get("abschnitt", "1"),
            person_typ=config.get("person_typ", ""),
        )

    # --- Verbindung ---

    def _connect(self):
        """Stellt eine Verbindung zur SchILD-DB her (pymysql)."""
        try:
            import pymysql
        except ImportError as exc:
            raise ImportError(
                "pymysql ist nicht installiert. Bitte installieren: pip install pymysql"
            ) from exc

        return pymysql.connect(
            # FOUND_ROWS: cursor.rowcount zählt GEFUNDENE statt geänderter
            # Zeilen. Nur so bedeutet rowcount == 0 beim Write-back
            # eindeutig "ID nicht gefunden" und nicht "Wert war schon gleich".
            client_flag=pymysql.constants.CLIENT.FOUND_ROWS,
            host=self.db_host,
            port=int(self.db_port),
            database=self.db_name,
            user=self.db_user,
            password=self.db_password,
            charset="utf8mb4",
            # Timeouts: nicht erreichbarer Server soll die GUI nicht
            # minutenlang einfrieren; Read großzügig wegen Foto-BLOBs.
            connect_timeout=8,
            read_timeout=120,
            write_timeout=120,
        )

    def test_connection(self) -> tuple[bool, str]:
        """Testet die Verbindung und gibt Schülerzahl zurück."""
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM schueler WHERE Status = 2")
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

        # 2. Klassenlehrer pro Klasse laden (versetzung = Klassen-Tabelle)
        cursor.execute(_SQL_CLASS_TEACHERS)
        ct_cols = [col[0] for col in cursor.description]
        class_teachers_by_class: dict[str, dict] = {}
        for row in cursor.fetchall():
            ct = dict(zip(ct_cols, row))
            klass = (ct.get("class_name") or "").strip()
            if klass:
                class_teachers_by_class[klass] = ct

        # 3. Kategorie-Hierarchie pro Klasse laden
        cursor.execute(_SQL_CLASS_HIERARCHY)
        hier_cols = [col[0] for col in cursor.description]
        hierarchy_by_class: dict[str, dict] = {}
        for row in cursor.fetchall():
            h = dict(zip(hier_cols, row))
            klass = (h.get("class_name") or "").strip()
            if klass and klass not in hierarchy_by_class:
                hierarchy_by_class[klass] = h

        # 4. Kurszuordnungen laden — Zwei-Schritt-Verfahren
        # Schuljahr/Abschnitt: Config-Wert oder Fallback aus eigeneschule
        schuljahr = self.schuljahr.strip()
        abschnitt = self.abschnitt.strip()
        if not schuljahr or not abschnitt:
            cursor.execute(_SQL_EIGENESCHULE)
            es_row = cursor.fetchone()
            if es_row:
                es_cols = [col[0] for col in cursor.description]
                es = dict(zip(es_cols, es_row))
                if not schuljahr:
                    schuljahr = str(es.get("Schuljahr") or "").strip()
                if not abschnitt:
                    abschnitt = str(es.get("SchuljahrAbschnitt") or "").strip()
            log.info(
                "Schuljahr/Abschnitt aus Config leer → Fallback aus eigeneschule:"
                " Schuljahr=%s, Halbjahr=%s",
                schuljahr,
                abschnitt,
            )

        # 4a: Abschnitt-IDs für das Schuljahr/Halbjahr ermitteln
        cursor.execute(_SQL_ABSCHNITT_IDS, (schuljahr, abschnitt))
        abschnitt_cols = [col[0] for col in cursor.description]
        abschnitt_to_student: dict[int, str] = {}
        for row in cursor.fetchall():
            r = dict(zip(abschnitt_cols, row))
            abschnitt_to_student[r["abschnitt_id"]] = str(r["student_id"])

        log.info(
            "Lernabschnitte: %d Abschnitte für Schuljahr=%s, Halbjahr=%s",
            len(abschnitt_to_student),
            schuljahr,
            abschnitt,
        )

        # 4b: Leistungsdaten für diese Abschnitte laden
        courses_by_student: dict[str, list[CourseAssignment]] = {}
        if abschnitt_to_student:
            abschnitt_ids = list(abschnitt_to_student.keys())
            placeholders = ",".join(["%s"] * len(abschnitt_ids))
            sql = _SQL_LEISTUNGSDATEN.replace("{placeholders}", placeholders)
            cursor.execute(sql, abschnitt_ids)
            course_cols = [col[0] for col in cursor.description]

            raw_fachlehrer_count = 0
            raw_fachlehrer_empty = 0

            for row in cursor.fetchall():
                c = dict(zip(course_cols, row))
                sid = abschnitt_to_student.get(c["abschnitt_id"])
                if not sid:
                    continue

                # Diagnostik: FachLehrer-Kürzel zählen
                krz = (c.get("fachlehrer_krz") or "").strip()
                raw_fachlehrer_count += 1
                if not krz:
                    raw_fachlehrer_empty += 1

                # Lehrkraft: FachLehrer bevorzugt, Fallback auf Kurs-Lehrer
                teacher = (c.get("teacher_name") or "").strip() or (
                    c.get("kurs_teacher_name") or ""
                ).strip()

                assignment = CourseAssignment(
                    course_name=(c.get("course_name") or "").strip(),
                    teacher_name=teacher,
                    course_id=str(c.get("kurs_id") or ""),
                    kurs_bezeichnung=(c.get("kurs_bezeichnung") or "").strip(),
                    kurs_zeugnisbez=(c.get("kurs_zeugnisbez") or "").strip(),
                    kursart=(c.get("kursart") or "").strip(),
                    teacher_kuerzel=krz,
                    teacher_email=(c.get("fachlehrer_email") or "").strip(),
                )
                courses_by_student.setdefault(sid, []).append(assignment)

            log.info(
                "Leistungsdaten: %d Zeilen, davon %d ohne FachLehrer-Kürzel",
                raw_fachlehrer_count,
                raw_fachlehrer_empty,
            )

        # Diagnostik: Kurse/Lehrer-Daten
        total_courses = sum(len(v) for v in courses_by_student.values())
        with_teacher = sum(
            1 for clist in courses_by_student.values() for c in clist if c.teacher_name
        )
        log.info(
            "Kurse geladen: %d Zuordnungen für %d Schüler, davon %d mit Lehrkraft"
            " (Schuljahr=%s, Abschnitt=%s)",
            total_courses,
            len(courses_by_student),
            with_teacher,
            schuljahr,
            abschnitt,
        )

        # Diagnostik: Fachzuordnungen pro Klasse
        sid_to_class: dict[str, str] = {}
        for raw in raw_students:
            sid = str(raw.get("school_internal_id", "")).strip()
            klass = (raw.get("class_name") or "").strip()
            if sid and klass:
                sid_to_class[sid] = klass
        class_course_counts: dict[
            str, tuple[int, int]
        ] = {}  # {klasse: (total, mit_lehrer)}
        for sid, clist in courses_by_student.items():
            klass = sid_to_class.get(sid, "?")
            prev_total, prev_with = class_course_counts.get(klass, (0, 0))
            class_course_counts[klass] = (
                prev_total + len(clist),
                prev_with + sum(1 for c in clist if c.teacher_name),
            )
        for klass in sorted(class_course_counts):
            total, with_t = class_course_counts[klass]
            log.info(
                "  Klasse %s: %d Fachzuordnungen, %d mit Lehrkraft",
                klass,
                total,
                with_t,
            )

        # 5. Fotos laden
        student_ids = [
            str(raw.get("school_internal_id", "")).strip()
            for raw in raw_students
            if str(raw.get("school_internal_id", "")).strip()
        ]
        photos_by_sid = self._load_photos(cursor, student_ids)

        conn.close()

        # 6. Zusammenbauen
        students: list[StudentRecord] = []
        skipped = 0

        for raw in raw_students:
            sid = str(raw.get("school_internal_id", "")).strip()
            if not sid:
                skipped += 1
                continue

            class_name = (raw.get("class_name") or "").strip()
            ct = class_teachers_by_class.get(class_name, {})
            hier = hierarchy_by_class.get(class_name, {})
            dob = self._format_date(raw.get("dob"))

            students.append(
                StudentRecord(
                    school_internal_id=sid,
                    first_name=(raw.get("first_name") or "").strip(),
                    last_name=(raw.get("last_name") or "").strip(),
                    dob=dob,
                    email=(raw.get("email") or "").strip(),
                    class_name=class_name,
                    photo_path=photos_by_sid.get(sid),
                    gender=str(raw.get("gender") or "").strip(),
                    class_teacher_1=(ct.get("teacher_1") or "").strip(),
                    class_teacher_2=(ct.get("teacher_2") or "").strip(),
                    class_teacher_1_krz=(ct.get("teacher_1_krz") or "").strip(),
                    class_teacher_2_krz=(ct.get("teacher_2_krz") or "").strip(),
                    class_teacher_1_email=(ct.get("teacher_1_email") or "").strip(),
                    class_teacher_2_email=(ct.get("teacher_2_email") or "").strip(),
                    abteilung=(hier.get("abteilung") or "").strip(),
                    fachklasse=(hier.get("fachklasse") or "").strip(),
                    schulgliederung=(hier.get("schulgliederung") or "").strip(),
                    courses=courses_by_student.get(sid, []),
                )
            )

        if skipped > 0:
            warnings.warn(f"{skipped} Schüler-Zeilen ohne ID übersprungen.")

        return students

    def load_teachers(self) -> list[TeacherRecord]:
        conn = self._connect()
        cursor = conn.cursor()

        # PersonTyp ist optional — leer bedeutet "alle mit Statistik = '+'".
        if self.person_typ:
            sql = _SQL_TEACHERS.format(person_typ_filter="AND kl.PersonTyp = %s")
            cursor.execute(sql, (self.person_typ,))
        else:
            cursor.execute(_SQL_TEACHERS.format(person_typ_filter=""))

        columns = [col[0] for col in cursor.description]

        teachers: list[TeacherRecord] = []
        skipped_no_dob = 0
        for row in cursor.fetchall():
            raw = dict(zip(columns, row))
            last_name = (raw.get("last_name") or "").strip()
            dob = self._format_date(raw.get("dob"))
            if not last_name:
                continue
            if not dob:
                # dob steckt im data_hash und im API-Payload — ohne Datum
                # würde der Datensatz bei jedem Lauf als "geändert" gelten.
                skipped_no_dob += 1
                continue
            teachers.append(
                TeacherRecord(
                    first_name=(raw.get("first_name") or "").strip(),
                    last_name=last_name,
                    dob=dob,
                    job_title=(raw.get("job_title") or "").strip(),
                    email=(raw.get("email") or "").strip(),
                    teacher_id=str(raw.get("teacher_id") or "").strip(),
                    kuerzel=(raw.get("kuerzel") or "").strip(),
                )
            )

        if skipped_no_dob:
            warnings.warn(
                f"{skipped_no_dob} Lehrkräfte ohne Geburtsdatum übersprungen "
                f"(k_lehrer.Geburtsdatum leer)."
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
                email = (update.get("email") or "").strip()
                if not email:
                    results.append(
                        {
                            "school_internal_id": sid,
                            "success": False,
                            "message": "Keine Email im Update — übersprungen",
                        }
                    )
                    continue

                cursor.execute(_SQL_WRITE_BACK_EMAIL, (email, sid))
                # Ohne diese Prüfung meldet ein ins Leere laufendes UPDATE
                # (ID existiert nicht) stillen Erfolg — die Adresse wäre
                # nie in SchILD gelandet, ohne dass es jemand merkt.
                if cursor.rowcount == 0:
                    results.append(
                        {
                            "school_internal_id": sid,
                            "success": False,
                            "message": (
                                f"Kein Schüler mit ID {sid} in der Tabelle "
                                f"'schueler' gefunden — nicht gespeichert"
                            ),
                        }
                    )
                    continue

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
    def _load_photos(cursor, student_ids: list[str]) -> dict[str, str]:
        """Lädt Fotos aus schuelerfotos und speichert sie als Temp-Dateien.

        Die Dateien liegen in einem eigenen Verzeichnis, das vor jedem
        Lauf geleert wird — so sammeln sich keine alten Fotos an.
        Gibt ein Dict {student_id: temp_file_path} zurück.
        """
        if not student_ids:
            return {}

        # Reste vom letzten Lauf (oder Absturz) entfernen
        _cleanup_photo_dir()
        _PHOTO_TMP_DIR.mkdir(parents=True, exist_ok=True)

        placeholders = ",".join(["%s"] * len(student_ids))
        sql = _SQL_PHOTOS.replace("{placeholders}", placeholders)
        cursor.execute(sql, student_ids)

        photos: dict[str, str] = {}
        photo_cols = [col[0] for col in cursor.description]
        for row in cursor.fetchall():
            p = dict(zip(photo_cols, row))
            sid = str(p["student_id"])
            blob = p.get("photo_blob")
            if not blob:
                continue
            photo_file = _PHOTO_TMP_DIR / f"{sid}.jpg"
            photo_file.write_bytes(blob)
            photos[sid] = str(photo_file)

        return photos

    @staticmethod
    def _format_date(value) -> str:
        """Konvertiert DB-Datumswert (DATETIME) nach ISO-String (YYYY-MM-DD)."""
        if value is None:
            return ""
        # pymysql gibt datetime.datetime oder datetime.date zurück
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        s = str(value).strip()
        # Fallback: "DD.MM.YYYY" → "YYYY-MM-DD"
        if "." in s:
            parts = s.split(".")
            if len(parts) == 3:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
        # Fallback: "YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DD"
        if " " in s:
            return s.split(" ")[0]
        return s
