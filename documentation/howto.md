# Schild Spider — Benutzerhandbuch

## Installation

1. Die aktuelle `SchildSpider.exe` von der [Releases-Seite](https://github.com/beiti/schild-spider/releases) herunterladen
2. In einen beliebigen Ordner entpacken
3. `SchildSpider.exe` starten

Beim ersten Start wird eine `settings.json` im gleichen Verzeichnis erstellt.

---

## Ersteinrichtung

### 1. Einstellungen öffnen

Klicke oben rechts auf **"Einstellungen..."** um den Konfigurationsdialog zu öffnen.

### 2. Allgemein

- **Schulname**: Name der Schule (wird nur zur Anzeige verwendet)

### 3. Datenquelle konfigurieren

Wähle den Typ der Datenquelle aus:

**SchILD CSV-Export** (Standard):
- **CSV-Datei**: Pfad zur exportierten SchILD-CSV-Datei (`;`-getrennt, Encoding: ISO-8859-1)
- **Foto-Ordner**: Ordner mit Schülerfotos, benannt nach SchILD-ID (z.B. `1234.jpg`)

> **SchILD-Export erstellen:** In SchILD NRW unter `Datenaustausch > Export` die Schülerdaten als CSV exportieren. Benötigte Spalten: `Interne ID-Nummer`, `Vorname`, `Nachname`, `Geburtsdatum`, `E-Mail (Schule)`, `Klasse`.

### 4. Plugins aktivieren

Jedes Plugin muss einzeln aktiviert und konfiguriert werden:

**Hagen-ID (Schülerausweis-System):**
1. Wähle "Hagen-ID" in der Plugin-Liste
2. Setze den Haken bei **"Aktiviert"**
3. Trage die **API URL** ein (z.B. `https://ausweisapi.example.com`)
4. Trage den **API Key** ein (wird im Hagen-ID Admin-Dashboard unter Einstellungen > API-Zugang generiert)
5. Klicke **"Verbindung testen"** um die Konfiguration zu prüfen
6. **"Speichern"** klicken

---

## Synchronisation durchführen

### Schritt 1: Änderungen berechnen

1. Klicke **"Änderungen berechnen"**
2. Schild Spider liest die SchILD-Daten ein und vergleicht sie mit den Zielsystemen
3. Die Ergebnisse werden in der Vorschau angezeigt

### Schritt 2: Vorschau prüfen

Die Vorschau zeigt pro Plugin:

- **Neue Schüler**: In SchILD vorhanden, aber noch nicht im Zielsystem
- **Änderungen**: Daten haben sich geändert (Name, Klasse, E-Mail, etc.)
- **Abmeldungen**: Im Zielsystem vorhanden, aber nicht mehr in SchILD
- **Foto-Updates**: Neues oder geändertes Foto

Aufklappen zeigt die Details pro Schüler.

### Schritt 3: Änderungen anwenden

1. Prüfe die Vorschau sorgfältig
2. Klicke **"Änderungen anwenden"**
3. Bestätige den Dialog
4. Der Fortschritt wird im Log angezeigt

---

## Failsafe-Schutz

Wenn mehr als **15%** der Schüler im Zielsystem deaktiviert werden sollen, blockiert Schild Spider die Synchronisation automatisch. Das schützt vor versehentlichen Massen-Deaktivierungen (z.B. durch eine unvollständige CSV-Datei).

In diesem Fall:
1. Prüfe ob die CSV-Datei vollständig ist
2. Stelle sicher, dass alle Schüler korrekt exportiert wurden
3. Berechne die Änderungen erneut

---

## Fehlerbehebung

| Problem | Lösung |
|---------|--------|
| "CSV-Datei nicht gefunden" | Pfad in den Einstellungen prüfen |
| "Keine aktiven Plugins" | Mindestens ein Plugin in den Einstellungen aktivieren |
| "Authentifizierung fehlgeschlagen" | API Key prüfen und ggf. neu generieren |
| "Verbindung fehlgeschlagen" | API URL und Netzwerkverbindung prüfen |
| Failsafe-Warnung | CSV-Export auf Vollständigkeit prüfen |
