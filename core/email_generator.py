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


def generate_email(
    first_name: str,
    last_name: str,
    domain: str,
    template: str = "{v}.{n}",
    existing_emails: set[str] | None = None,
) -> str:
    """Erzeugt eine Email-Adresse aus Vor-/Nachname + Domain.

    Template-Platzhalter:
        {v} = Vorname (transliteriert, lowercase)
        {n} = Nachname (transliteriert, lowercase)

    Bei Kollision mit existing_emails wird ein Zähler angehängt:
        h.mueller@domain → h.mueller2@domain → h.mueller3@domain
    """
    v = _sanitize(transliterate(first_name))
    n = _sanitize(transliterate(last_name))

    local_part = template.replace("{v}", v).replace("{n}", n)
    email = f"{local_part}@{domain}"

    if existing_emails is None or email not in existing_emails:
        return email

    counter = 2
    while True:
        candidate = f"{local_part}{counter}@{domain}"
        if candidate not in existing_emails:
            return candidate
        counter += 1


def _sanitize(text: str) -> str:
    """Lowercase, nur a-z, 0-9, Punkt und Bindestrich behalten."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9.\-]", "", text)
    return text
