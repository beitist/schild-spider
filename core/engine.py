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
    target_map: dict[str, dict] = {
        s["school_internal_id"]: s for s in manifest
    }

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
            changed.append(student)

        # Foto-Hash vergleichen (falls Foto vorhanden)
        if student.get("photo_path"):
            local_photo_hash = _compute_photo_hash_if_available(
                plugin, student["photo_path"]
            )
            remote_photo_hash = target.get("photo_hash", "")
            if local_photo_hash and local_photo_hash != remote_photo_hash:
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


def _compute_photo_hash_if_available(
    plugin: PluginBase, photo_path: str
) -> str | None:
    """Berechnet den Photo-Hash über das Plugin, falls die Methode existiert."""
    if hasattr(plugin, "compute_photo_hash"):
        return plugin.compute_photo_hash(photo_path)
    return None
