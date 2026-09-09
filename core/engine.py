from __future__ import annotations

from dataclasses import asdict

from core.models import ChangeSet, StudentRecord
from plugins.base import PluginBase


def compute_changeset(
    source: list[StudentRecord],
    plugin: PluginBase,
    max_suspend_percentage: float = 15.0,
) -> ChangeSet:
    """Vergleicht SchILD-Daten mit dem IST-Zustand eines Plugins und berechnet ein ChangeSet."""

    # Source als Dict indexiert nach school_internal_id
    source_map: dict[str, dict] = {}
    for student in source:
        d = asdict(student)
        d["_data_hash"] = plugin.compute_data_hash(d)
        source_map[student.school_internal_id] = d

    # Manifest vom Zielsystem holen
    manifest = plugin.get_manifest()
    target_map: dict[str, dict] = {s["school_internal_id"]: s for s in manifest}

    # Sekundäres Matching per Email (z.B. M365-User ohne employeeId)
    email_manifest: dict[str, dict] = getattr(plugin, "_email_manifest", {})
    if email_manifest:
        for sid, student in source_map.items():
            if sid not in target_map:
                email = (student.get("email") or "").lower()
                if email and email in email_manifest:
                    target_map[sid] = email_manifest[email]

    new: list[dict] = []
    changed: list[dict] = []
    photo_updates: list[dict] = []
    suspended: list[str] = []

    # Neue und geänderte Schüler finden
    for sid, student in source_map.items():
        target = target_map.get(sid)

        if target is None:
            # Schüler existiert nicht im Zielsystem
            new.append(student)
            continue

        # Daten-Hash vergleichen
        if student["_data_hash"] != target.get("data_hash", ""):
            student["_diff"] = describe_diff(student, target.get("fields"))
            changed.append(student)

        # Foto-Hash vergleichen (falls Foto vorhanden)
        if student.get("photo_path") and plugin.needs_photo_update(student, target):
            photo_updates.append(student)

    # Abgemeldete Schüler finden (im Zielsystem aber nicht mehr in SchILD)
    for sid, target in target_map.items():
        if sid not in source_map and target.get("is_active", True):
            suspended.append(sid)

    # Failsafe berechnen
    total_in_target = len(target_map)
    suspend_pct = (
        (len(suspended) / total_in_target * 100) if total_in_target > 0 else 0.0
    )
    requires_force = suspend_pct > max_suspend_percentage

    return ChangeSet(
        new=new,
        changed=changed,
        suspended=suspended,
        photo_updates=photo_updates,
        total_in_source=len(source_map),
        total_in_target=total_in_target,
        suspend_percentage=round(suspend_pct, 1),
        requires_force=requires_force,
    )


# Feld → Anzeigename für die Diff-Beschreibung in der Vorschau
_DIFF_LABELS: tuple[tuple[str, str], ...] = (
    ("email", "Email"),
    ("class_name", "Klasse"),
    ("last_name", "Nachname"),
    ("first_name", "Vorname"),
    ("dob", "Geburtsdatum"),
)


def describe_diff(student: dict, target_fields: dict | None) -> list[str]:
    """Beschreibt, was sich zwischen SchILD und Zielsystem geändert hat.

    Vergleicht nur Felder, die das Zielsystem im Manifest mitliefert
    (``fields``). Ohne Ist-Werte bleibt nur die generische Aussage.
    Returns: z.B. ["Email: alt@x.de → neu@x.de", "Klasse: 10a → 10b"]
    """
    if not target_fields:
        return ["Daten geändert"]

    parts: list[str] = []
    for key, label in _DIFF_LABELS:
        if key not in target_fields:
            continue
        new = str(student.get(key) or "").strip()
        old = str(target_fields.get(key) or "").strip()
        same = new.lower() == old.lower() if key == "email" else new == old
        if same:
            continue
        if not old:
            parts.append(f"{label} neu: {new}")
        elif not new:
            parts.append(f"{label} entfernt (war: {old})")
        else:
            parts.append(f"{label}: {old} → {new}")

    return parts or ["Daten geändert"]
