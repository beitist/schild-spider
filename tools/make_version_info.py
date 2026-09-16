"""Erzeugt die Windows-Versionsinformation für PyInstaller (--version-file).

Liest Name und Version aus ``core/version.py``, damit die Angaben in den
Datei-Eigenschaften der EXE nie von der App abweichen. Aufruf im CI vor
dem Build:

    python tools/make_version_info.py version_info.txt

Hintergrund: Virenscanner-Heuristiken bewerten Programme ohne Hersteller-
und Versionsangaben misstrauischer. Unter Windows erscheinen die Angaben
in den Datei-Eigenschaften im Reiter "Details".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.version import (  # noqa: E402
    APP_COPYRIGHT,
    APP_LICENSE,
    APP_NAME,
    APP_VERSION,
)

_REPO_URL = "https://github.com/beitist/schild-spider"


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """'0.7.7-dev' → (0, 7, 7, 0). Windows erwartet genau vier Zahlen."""
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0])][:4]
    numbers += [0] * (4 - len(numbers))
    return numbers[0], numbers[1], numbers[2], numbers[3]


def render() -> str:
    """Versionsinfo im Python-Format, das PyInstaller einliest."""
    vt = version_tuple(APP_VERSION)
    strings = {
        "CompanyName": APP_NAME,
        "FileDescription": f"{APP_NAME} - Synchronisation SchILD NRW",
        "FileVersion": APP_VERSION,
        "InternalName": "SchildSpider",
        "LegalCopyright": f"{APP_COPYRIGHT}, Lizenz {APP_LICENSE}",
        "OriginalFilename": "SchildSpider.exe",
        "ProductName": APP_NAME,
        "ProductVersion": APP_VERSION,
        "Comments": _REPO_URL,
    }
    string_structs = "\n".join(
        f"        StringStruct({key!r}, {value!r})," for key, value in strings.items()
    )
    # 0407 = Deutsch, 04B0 = Unicode (1200)
    return f"""# Automatisch erzeugt von tools/make_version_info.py, nicht von Hand ändern.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt},
    prodvers={vt},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('040704B0', [
{string_structs}
      ]),
    ]),
    VarFileInfo([VarStruct('Translation', [0x0407, 1200])]),
  ],
)
"""


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "version_info.txt")
    target.write_text(render(), encoding="utf-8")
    print(f"{target}: {APP_NAME} {APP_VERSION} {version_tuple(APP_VERSION)}")


if __name__ == "__main__":
    main()
