<p align="center">
  <img src="assets/schild-spider.png" alt="Schild Spider Logo" width="200">
</p>

# Schild Spider

Desktop-Tool zur automatisierten Synchronisation von Schülerdaten zwischen **SchILD NRW** und angebundenen Zielsystemen.

```
┌──────────────┐    ┌──────────┐    ┌──────────────┐
│   ADAPTER    │───>│   CORE   │───>│   PLUGINS    │
│   (Input)    │    │ (Engine) │    │  (Output)    │
└──────────────┘    └──────────┘    └──────────────┘
 SchILD CSV/DB       Diff-Engine     Hagen-ID API
                     ChangeSet       M365 Graph
                     Failsafes       Moodle (geplant)
```

## Features

- **Hub-and-Spoke-Architektur** — Adapter lesen, Plugins schreiben, die Core-Engine berechnet den Diff
- **Dry-Run-Prinzip** — Änderungen werden berechnet und in einer Vorschau angezeigt, bevor sie angewendet werden
- **Failsafe-Schutz** — Blockiert automatisch bei >15% Deaktivierungen (Schutz vor unvollständigen Datenexporten)
- **Plugin-System** — Jedes Plugin beschreibt sich selbst (Config-Felder, Verbindungstest), die GUI rendert dynamisch
- **Adapter-System** — Verschiedene Datenquellen (CSV-Export, DB-Zugriff) über einheitliche Schnittstelle

## Unterstützte Systeme

| Typ | System | Status |
|-----|--------|--------|
| Adapter | SchILD CSV-Export | Verfügbar |
| Adapter | SchILD DB (MariaDB/ODBC) | Verfügbar |
| Plugin | Hagen-ID (Schülerausweise) | Verfügbar |
| Plugin | Microsoft 365 (Graph API) | Verfügbar |
| Plugin | Moodle | Geplant |
| Plugin | Untis | Geplant |

## Schnellstart

### Voraussetzungen

- Python 3.12+
- Abhängigkeiten: `PySide6`, `requests`, `Pillow`

### Installation (Entwicklung)

```bash
git clone https://github.com/beitist/schild-spider.git
cd schild-spider
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### Konfiguration

Beim **ersten Start** öffnet sich ein Einrichtungs-Assistent:
1. Schulname eingeben
2. Datenquelle (Adapter) wählen
3. Gewünschte Zielsysteme (Plugins) aktivieren

Anschließend unter **„Einstellungen..."** die Pfade und API-Keys eintragen und die Verbindung testen.

> Bei Updates wird die `settings.json` automatisch migriert — bestehende Einstellungen bleiben erhalten.

### Starten

```bash
python main.py
```

### Windows .exe

Die vorkompilierte `.exe` ist auf der [Releases-Seite](https://github.com/beitist/schild-spider/releases) verfügbar. Wird automatisch per GitHub Actions bei jedem Tag gebaut.

## Projektstruktur

```
schild-spider/
├── main.py                  # Einstiegspunkt + Splash + Setup-Wizard
├── requirements.txt
│
├── core/
│   ├── models.py            # StudentRecord, ChangeSet, ConfigField
│   ├── engine.py            # Diff-Logik + Failsafe
│   ├── email_generator.py   # Email-Erzeugung mit Transliteration
│   ├── graph_client.py      # Microsoft Graph REST API Client
│   ├── paths.py             # Asset-Pfade (PyInstaller-kompatibel)
│   └── plugin_loader.py     # Registry, Settings-Versionierung, Migration
│
├── adapters/
│   ├── base.py              # AdapterBase (ABC)
│   ├── schild_csv.py        # CSV-Import
│   └── schild_db.py         # MariaDB/ODBC-Direktzugriff
│
├── plugins/
│   ├── base.py              # PluginBase (ABC)
│   ├── hagen_id.py          # Hagen-ID REST API
│   └── m365.py              # Microsoft 365 / Entra ID (Graph API)
│
├── gui/
│   ├── mainwindow.py        # Hauptfenster (3-Phasen-Workflow)
│   ├── settings_dialog.py   # Einstellungen + Plugin-Manager
│   ├── setup_wizard.py      # Erststart-Assistent
│   └── workers.py           # Hintergrund-Worker (Load, Compute, Apply)
│
├── documentation/
│   └── howto.md              # Benutzerhandbuch
│
└── .github/workflows/
    ├── build-exe.yml         # CI: PyInstaller Build → GitHub Release
    └── quality.yml           # CI: Ruff Lint + Format
```

## Microsoft 365 / Entra ID einrichten

Das M365-Plugin nutzt die **Microsoft Graph REST API** mit Application Permissions (Client Credentials Flow). Folgende Einrichtung ist einmalig im Azure-Portal nötig:

### 1. App Registration erstellen

1. [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**
2. Name: z.B. `Schild Spider`
3. Supported account types: **Single tenant** (nur diese Organisation)
4. Redirect URI: leer lassen
5. **Register** klicken

### 2. Client Secret erzeugen

1. In der neuen App → **Certificates & secrets** → **New client secret**
2. Beschreibung und Gültigkeit wählen → **Add**
3. **Value** sofort kopieren (wird nur einmal angezeigt!)

### 3. API-Berechtigungen setzen

Unter **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**:

| Permission | Wofür |
|---|---|
| `User.ReadWrite.All` | Schüler-Accounts anlegen, ändern, deaktivieren |
| `Directory.ReadWrite.All` | Gruppen + Lizenzen verwalten |
| `GroupMember.ReadWrite.All` | Gruppen-Mitgliedschaften pflegen |

Danach **Grant admin consent** klicken (erfordert Global Admin / Privileged Role Admin).

### 4. IDs notieren

Aus der App-Übersicht (**Overview**) benötigst du:
- **Application (client) ID**
- **Directory (tenant) ID**
- Den zuvor kopierten **Client Secret Value**

Diese drei Werte trägst du in Schild Spider unter **Einstellungen → Microsoft 365** ein.

### 5. Optionale SKU-ID für Lizenz-Zuweisung

Falls Schüler automatisch eine Lizenz erhalten sollen (z.B. A1 for Students), benötigst du die **SKU-ID**. Diese findest du über:
- Azure Portal → **Licenses** → **All products** → Produkt anklicken → Properties → **Object ID**
- Oder per Graph API: `GET /subscribedSkus`

Lässt du das Feld leer, erfolgt keine automatische Lizenzzuweisung.

---

## Eigenes Plugin / Adapter entwickeln

### Plugin

1. Neue Datei in `plugins/` erstellen
2. Von `PluginBase` erben und alle Methoden implementieren:
   - `plugin_name()`, `config_schema()`, `from_config()`, `test_connection()`
   - `get_manifest()`, `compute_data_hash()`, `apply_new()`, `apply_changes()`, `apply_suspend()`
3. In `core/plugin_loader.py` → `_PLUGIN_REGISTRY` eintragen

### Adapter

1. Neue Datei in `adapters/` erstellen
2. Von `AdapterBase` erben:
   - `adapter_name()`, `config_schema()`, `from_config()`
   - `load() -> list[StudentRecord]`
3. In `core/plugin_loader.py` → `_ADAPTER_REGISTRY` eintragen

Die GUI zeigt neue Adapter und Plugins automatisch mit den richtigen Eingabefeldern an.

### Windows EXE bauen

Neue Adapter/Plugins werden automatisch in die EXE eingebundelt (`--collect-submodules` in der Build-Config). Einfach einen neuen Git-Tag pushen — GitHub Actions baut die EXE und erstellt ein Release:

```bash
git tag v1.0
git push origin main --tags
```

## Lizenz

[GPL v3](LICENSE)

Abhängigkeiten unterliegen ihren eigenen Lizenzen (PySide6: LGPL v3, requests: Apache 2.0, Pillow: HPND).
