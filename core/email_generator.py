"""Email-Generierung mit Sonderzeichen-Transliteration und Kollisionserkennung."""

from __future__ import annotations

import re

# Mapping für gängige Sonderzeichen im deutschsprachigen Schulkontext
_TRANSLITERATION: dict[str, str] = {
    # Deutsch
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "Ae",
    "Ö": "Oe",
    "Ü": "Ue",
    # Französisch / Romanisch
    "é": "e",
    "è": "e",
    "ê": "e",
    "ë": "e",
    "á": "a",
    "à": "a",
    "â": "a",
    "ó": "o",
    "ò": "o",
    "ô": "o",
    "ú": "u",
    "ù": "u",
    "û": "u",
    "í": "i",
    "ì": "i",
    "î": "i",
    "ï": "i",
    "ç": "c",
    "ñ": "n",
    # Osteuropäisch / Türkisch
    "ć": "c",
    "č": "c",
    "ń": "n",
    "ş": "s",
    "š": "s",
    "ž": "z",
    "ğ": "g",
    "ł": "l",
    "ř": "r",
    "ý": "y",
    "ź": "z",
    "ż": "z",
    "đ": "d",
    "ı": "i",
    # Skandinavisch
    "ø": "oe",
    "å": "a",
    "æ": "ae",
}


def transliterate(text: str) -> str:
    """Ersetzt Sonderzeichen durch ASCII-Äquivalente.

    Erkennt automatisch Großbuchstaben-Varianten (Ç → C, Ş → S, etc.)
    anhand der Lowercase-Einträge in der Tabelle.
    """
    result: list[str] = []
    for ch in text:
        if ch in _TRANSLITERATION:
            result.append(_TRANSLITERATION[ch])
        elif ch.lower() in _TRANSLITERATION:
            replacement = _TRANSLITERATION[ch.lower()]
            if ch.isupper():
                replacement = replacement.capitalize()
            result.append(replacement)
        else:
            result.append(ch)
    return "".join(result)


def email_candidates(
    first_name: str,
    last_name: str,
    class_name: str,
    domain: str,
    template: str = "{v}.{n}",
    max_counter: int = 99,
):
    """Erzeugt Adress-Kandidaten in fester Eskalationsreihenfolge.

    Bei häufigen Nachnamen (z.B. "Nguyen" in derselben Klasse) reicht das
    Template allein nicht. Die Kette eskaliert deshalb schrittweise —
    Beispiel für Template ``{k}.{n}``, Klasse AAV26S, Nguyen, "Thi Thuy":

        Stufe 0: aav26s.nguyen@...          (Template)
        Stufe 1: aav26s.nguyen.thi@...      (+ erster Vorname)
        Stufe 2: aav26s.nguyen.thithuy@...  (+ erste zwei Vornamen)
        Stufe 3: aav26s.nguyen.thi2@...     (+ erster Vorname + Zähler)

    Stufen ohne Datengrundlage werden übersprungen (z.B. Stufe 2 bei nur
    einem Vornamen). Die Reihenfolge ist rein datenabhängig und damit
    reproduzierbar — gleiche Eingabe, gleiche Kette.
    """
    v = _sanitize(transliterate(first_name))
    n = _sanitize(transliterate(last_name))
    k = _sanitize(transliterate(class_name))
    base = _render_local_part(template, v, n, k)

    parts = _name_parts(first_name)
    v1 = parts[0] if parts else ""
    v12 = "".join(parts[:2]) if len(parts) > 1 else ""

    emitted: set[str] = set()
    for local in _candidate_locals(base, v1, v12, max_counter):
        if local and local not in emitted:
            emitted.add(local)
            yield f"{local}@{domain}"


def _candidate_locals(base: str, v1: str, v12: str, max_counter: int):
    """Local-Parts der Eskalationsstufen (ohne Domain)."""
    yield base
    if v1:
        yield f"{base}.{v1}"
    if v12:
        yield f"{base}.{v12}"
    for i in range(2, max_counter + 1):
        yield f"{base}.{v1}{i}" if v1 else f"{base}.{i}"


def generate_email(
    first_name: str,
    last_name: str,
    domain: str,
    template: str = "{v}.{n}",
    existing_emails: set[str] | None = None,
    class_name: str = "",
) -> str | None:
    """Erzeugt eine freie Email-Adresse für einen einzelnen Schüler.

    Template-Platzhalter: ``{v}`` Vorname, ``{n}`` Nachname, ``{k}`` Klasse
    (alle transliteriert und lowercase).

    Nimmt den ersten freien Kandidaten aus :func:`email_candidates`.
    Returns None, wenn auch die Zähler-Stufe erschöpft ist.

    Für mehrere Schüler auf einmal :func:`assign_emails` verwenden — nur
    die Batch-Vergabe ist gegen die Reihenfolge der Quelldaten immun.
    """
    taken = {(e or "").lower() for e in (existing_emails or ())}
    for candidate in email_candidates(
        first_name, last_name, class_name, domain, template
    ):
        if candidate.lower() not in taken:
            return candidate
    return None


def assign_emails(
    students: list[dict],
    domain: str,
    template: str = "{v}.{n}",
    taken_emails=None,
) -> dict[str, str | None]:
    """Vergibt Adressen für mehrere Schüler kollisionsfrei und deterministisch.

    Die Vergabe folgt NICHT der Reihenfolge der Eingabeliste, sondern der
    SchILD-ID. Derselbe Datenbestand ergibt damit immer dieselbe Zuordnung
    — egal ob die Quelldaten nach Name, Klasse oder gar nicht sortiert
    ankommen, und egal ob Vorschau oder Anwenden die Vergabe auslöst.

    students: Dicts mit school_internal_id, first_name, last_name, class_name
    taken_emails: bereits vergebene Adressen (Zielsystem + Quelldaten)
    Returns: {school_internal_id: email} — None, wenn keine Stufe frei war.
    """
    taken = {(e or "").strip().lower() for e in (taken_emails or ())} - {""}
    result: dict[str, str | None] = {}

    for student in sorted(students, key=_student_sort_key):
        sid = str(student.get("school_internal_id", ""))
        chosen: str | None = None
        for candidate in email_candidates(
            student.get("first_name", ""),
            student.get("last_name", ""),
            student.get("class_name", ""),
            domain,
            template,
        ):
            if candidate.lower() not in taken:
                chosen = candidate
                taken.add(candidate.lower())
                break
        result[sid] = chosen

    return result


def _student_sort_key(student: dict) -> tuple[int, int, str]:
    """Stabiler Sortierschlüssel: numerische IDs numerisch, sonst alphabetisch."""
    sid = str(student.get("school_internal_id", "")).strip()
    if sid.isdigit():
        return (0, int(sid), "")
    return (1, 0, sid)


def _name_parts(first_name: str) -> list[str]:
    """Zerlegt den Vornamen in einzelne, sanitisierte Bestandteile.

    "Thi Thuy" → ["thi", "thuy"], "Anne-Marie" → ["anne", "marie"]
    """
    raw = transliterate(first_name).replace("-", " ")
    parts = [re.sub(r"[^a-z0-9]", "", part.lower()) for part in raw.split()]
    return [p for p in parts if p]


def _sanitize(text: str) -> str:
    """Lowercase, nur a-z, 0-9, Punkt und Bindestrich behalten."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9.\-]", "", text)
    return text


def _render_local_part(template: str, v: str, n: str, k: str) -> str:
    """Setzt die (bereits sanitisierten) Werte ins Template ein."""
    return template.replace("{v}", v).replace("{n}", n).replace("{k}", k)


def _template_pattern(template: str, v: str, n: str) -> re.Pattern[str]:
    """Baut aus dem Template ein Regex, das den Klassenteil einfängt.

    Vor-/Nachname sind literal (bekannt), ``{k}`` wird zur Capture-Gruppe.
    Ein optionales Kollisions-Suffix (``.thithuy``, ``.thi2``) ist erlaubt.
    """
    parts = re.split(r"(\{k\}|\{v\}|\{n\})", template)
    out: list[str] = []
    for part in parts:
        if part == "{k}":
            out.append(r"(?P<k>[a-z0-9.\-]+?)")
        elif part == "{v}":
            out.append(re.escape(v))
        elif part == "{n}":
            out.append(re.escape(n))
        elif part:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + r"(?:\.[a-z0-9]+)?$")


def analyze_email(
    current: str,
    first_name: str,
    last_name: str,
    class_name: str,
    domain: str,
    template: str = "{v}.{n}",
) -> dict | None:
    """Prüft, ob eine bestehende Email noch zum Schema (Template) passt.

    Returns:
        None  — Adresse passt (auch mit Kollisions-Suffix) oder ist nicht
                prüfbar (leer, fremde Domain → wird nicht angefasst).
        dict  — {"reason": "class_change", "old_class": "10a"} wenn der
                Klassenteil der Adresse nicht mehr zur Klasse passt, oder
                {"reason": "mismatch", "old_class": ""} bei sonstiger
                Abweichung (z.B. Namensänderung, manuell vergebene Adresse).
    """
    current = (current or "").strip().lower()
    if not current or "@" not in current:
        return None
    local, _, cur_domain = current.rpartition("@")
    if cur_domain != domain.strip().lower():
        return None

    v = _sanitize(transliterate(first_name))
    n = _sanitize(transliterate(last_name))
    k = _sanitize(transliterate(class_name))
    base = _render_local_part(template, v, n, k)

    # Passt die Adresse zu irgendeiner Stufe der Eskalationskette?
    for candidate in email_candidates(
        first_name, last_name, class_name, domain, template
    ):
        if local == candidate.rpartition("@")[0]:
            return None

    # Altbestand aus früheren Versionen: Template + erste drei Buchstaben
    # des Vornamens. Nicht mehr neu vergeben, aber weiterhin gültig.
    if v[:3] and local == f"{base}.{v[:3]}":
        return None

    match = _template_pattern(template, v, n).match(local)
    old_k = match.groupdict().get("k") if match else None
    if old_k and old_k != k:
        return {"reason": "class_change", "old_class": old_k}
    return {"reason": "mismatch", "old_class": ""}


# ---------------------------------------------------------------------------
# Prüfung aller Quell-Emails (läuft bei jedem Laden der Quelldaten)
# ---------------------------------------------------------------------------

# Befunde ohne Handlungsbedarf
EMAIL_OK = "ok"
EMAIL_EMPTY = "empty"  # vergibt das Plugin beim Anlegen
EMAIL_FOREIGN = "foreign"  # andere Domain, wird nicht angefasst
# Befunde mit Handlungsbedarf (erscheinen in "findings")
EMAIL_CLASS_CHANGE = "class_change"
EMAIL_MISMATCH = "mismatch"
EMAIL_DUPLICATE = "duplicate"
EMAIL_COLLISION = "collision"


def check_emails(students: list[dict], domain: str, template: str = "{v}.{n}") -> dict:
    """Prüft ALLE Quell-Emails gegen das Schema und auf Dubletten.

    Jeder Schüler (mit ID und Klasse) bekommt genau einen Status:

        ok            passt zum Schema (auch mit Kollisions-Suffix)
        empty         keine Adresse — vergibt das Plugin beim Anlegen
        foreign       andere Domain — wird nicht angefasst
        class_change  trägt eine fremde Klasse (nach Klassenwechsel)
        mismatch      weicht anders ab (Namensänderung, manuell vergeben)
        duplicate     dieselbe Adresse hat schon ein anderer Schüler
        collision     braucht eine neue Adresse, aber alle Stufen belegt

    Bei Dubletten behält die niedrigste SchILD-ID die Adresse (gleiche
    Regel wie bei ``assign_emails``), alle anderen bekommen einen
    Vorschlag. Welches Konto in Microsoft 365 die Adresse wirklich
    besitzt, weiß die Ladephase nicht — deshalb werden Dubletten nie
    automatisch korrigiert, sondern nur vorgeschlagen.

    Returns:
        {"status": {sid: status}, "findings": [vorschlag, ...]}
        Vorschläge haben die Felder school_internal_id, first_name,
        last_name, class_name, old_email, email, reason, old_class, checked.
    """
    own_domain = domain.strip().lower()
    status: dict[str, str] = {}
    pending: list[tuple[dict, str, str]] = []  # (schüler, grund, alte klasse)
    ok_by_email: dict[str, list[dict]] = {}

    for s in students:
        sid = str(s.get("school_internal_id", "")).strip()
        klass = s.get("class_name", "")
        if not sid or not klass:
            continue

        current = (s.get("email") or "").strip()
        if not current:
            status[sid] = EMAIL_EMPTY
            continue
        if current.lower().rpartition("@")[2] != own_domain:
            status[sid] = EMAIL_FOREIGN
            continue

        finding = analyze_email(
            current,
            s.get("first_name", ""),
            s.get("last_name", ""),
            klass,
            domain,
            template,
        )
        if finding is None:
            status[sid] = EMAIL_OK
            ok_by_email.setdefault(current.lower(), []).append(s)
        else:
            status[sid] = finding["reason"]
            pending.append((s, finding["reason"], finding.get("old_class", "")))

    # Dubletten unter den schemakonformen Adressen: jede für sich passt,
    # aber zwei Konten können nicht dieselbe Adresse haben.
    for group in ok_by_email.values():
        if len(group) < 2:
            continue
        for s in sorted(group, key=_student_sort_key)[1:]:
            status[str(s.get("school_internal_id", "")).strip()] = EMAIL_DUPLICATE
            pending.append((s, EMAIL_DUPLICATE, ""))

    if not pending:
        return {"status": status, "findings": []}

    # Belegt bleibt jede Adresse eines Schülers, der sie BEHÄLT. Nicht über
    # "alle minus ersetzte" rechnen: Teilen sich ein veralteter und ein
    # gültiger Schüler eine Adresse, würde sie sonst fälschlich frei.
    changing = {str(s.get("school_internal_id", "")).strip() for s, _, _ in pending}
    taken = {
        (s.get("email") or "").strip().lower()
        for s in students
        if str(s.get("school_internal_id", "")).strip() not in changing
    } - {""}
    assigned = assign_emails([s for s, _, _ in pending], domain, template, taken)

    findings: list[dict] = []
    for s, reason, old_class in pending:
        sid = str(s.get("school_internal_id", "")).strip()
        new_email = assigned.get(sid)
        if not new_email:
            reason = EMAIL_COLLISION
            status[sid] = EMAIL_COLLISION
        findings.append(
            {
                "school_internal_id": sid,
                "first_name": s.get("first_name", ""),
                "last_name": s.get("last_name", ""),
                "class_name": s.get("class_name", ""),
                "old_email": (s.get("email") or "").strip(),
                "email": new_email or "",
                "reason": reason,
                "old_class": old_class,
                # Nur Klassenwechsel sind eindeutig — alles andere
                # entscheidet der Anwender bewusst.
                "checked": reason == EMAIL_CLASS_CHANGE,
            }
        )
    return {"status": status, "findings": findings}
