# Fokus

Schwebende Worktimer-Pille für Windows: Fokus- und Pausentimer, Tagesleiste, Spotify-Steuerung (Web-Player in Chrome) und eine lokale Schnittstelle für Claude.

## Installieren

1. `Fokus-Setup-<version>.exe` aus dem neuesten Release ausführen. Die Pille erscheint oben rechts, das Symbol im Infobereich (unten rechts) blendet sie aus oder beendet sie.
2. Spotify: in `chrome://extensions` den Entwicklermodus einschalten, „Entpackte Erweiterung laden“ und `%LocalAppData%\Programs\Fokus\chrome-ext` wählen (Startmenü: „Fokus – Chrome-Erweiterung (Ordner)“). Danach den Spotify-Tab neu laden.

Bauen: `powershell -ExecutionPolicy Bypass -File build.ps1` (Tests, Fokus.exe, Setup in `dist\installer`).

## Bedienung

- Klick auf die Pille: Timer starten / anhalten. Ziehen: verschieben.
- Pfeil rechts: Panel mit Fokus/Pause, freier Zeit (−/+, Mausrad, 25/50/90), Heute-Leiste, Musikkarte.
- Hover über der Pille: zurück / Play-Pause / weiter. Mausrad: Spotify-Lautstärke.
- Klick auf die Heute-Leiste (oder Infobereich → „Auswertung“): Auswertungsfenster neben der Pille (Chrome-App-Fenster, sonst Edge InPrivate) mit Tag, Woche, Monat und Zeitraum (7/10/14/30 Tage); ← → blättert, T = heute, 1–4 = Ansicht. Arbeitstag wechselt um 4 Uhr; Lücken bis 90 min zwischen Blöcken zählen als Pause. Tagesziel für die Ringe: `tagesziel_h` in `config.json` (Standard 6).

## Stream-Banner (OBS)

Browserquelle mit URL `http://127.0.0.1:47650/banner`, Breite 900, Höhe 260 (skaliert mit der Breite, Hintergrund transparent). Zeigt Cover, Titel, Künstler, Fortschritt und Dauer des laufenden Spotify-Songs; Farben kommen aus dem Cover. Blendet aus, wenn nichts läuft oder 20 s pausiert ist.

## Schnittstelle (nur 127.0.0.1)

```
GET  http://127.0.0.1:47650/stand
GET  http://127.0.0.1:47650/tage        (je Arbeitstag: Arbeit, Pausen, Blöcke, Verteilung über den Tag)
GET  http://127.0.0.1:47650/auswertung  (Auswertungsseite)
GET  http://127.0.0.1:47650/banner      (Stream-Banner)
GET  http://127.0.0.1:47650/jetzt       (laufender Song inkl. Cover-Adresse und Position)
POST http://127.0.0.1:47650/timer    {"aktion": "start|anhalten|stop|pause|ziel", "minuten": 50, "titel": "..."}
POST http://127.0.0.1:47650/spotify  {"cmd": "playpause|weiter|zurueck|lautstaerke", "wert": 0.4}
```

Daten: `%APPDATA%\Fokus\bloecke.jsonl` (ein Block pro Zeile) und `config.json`.

## Entwickeln

```
py -3.11 -m venv .venv
.venv\Scripts\pip install pillow numpy pystray==0.19.5 pyinstaller
.venv\Scripts\python test_fokus.py
.venv\Scripts\pyinstaller --onefile --windowed --name Fokus --hidden-import pystray._win32 fokus.py
```
