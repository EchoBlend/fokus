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

## Schnittstelle (nur 127.0.0.1)

```
GET  http://127.0.0.1:47650/stand
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
