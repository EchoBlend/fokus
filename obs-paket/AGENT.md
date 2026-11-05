# Auftrag: Fokus-Stream-Banner in OBS einrichten

Für den Agenten auf Timos Stream-PC (dort, wo OBS läuft). Sprich mit Timo Deutsch, kurz, in Stichpunkten.

**Ziel:** OBS zeigt unten links ein Banner mit dem laufenden Spotify-Song (Cover, Titel, Künstler, Fortschritt, Dauer).

**So funktioniert es:**
- Die Fokus-App (lokal, `127.0.0.1:47650`) liefert das Banner aus.
- Die Fokus-Chrome-Erweiterung liest den Spotify-Web-Player (`open.spotify.com` in Chrome).
- Die Spotify-Desktop-App wird **nicht** erkannt.

**Paketinhalt:**
- `Fokus-Setup-0.2.15.exe`
- diese Datei

## 1. Fokus installieren

```
Fokus-Setup-0.2.15.exe /VERYSILENT
```

- Ohne Admin-Rechte. Installiert nach `%LOCALAPPDATA%\Programs\Fokus` und trägt Fokus in den Autostart ein.
- Eine ältere Fokus-Version wird dabei ersetzt.
- Danach `%LOCALAPPDATA%\Programs\Fokus\Fokus.exe` starten. Oben rechts erscheint die Pille; Timo kann sie über das Symbol im Infobereich ausblenden.
- Prüfen: `curl http://127.0.0.1:47650/jetzt` muss JSON liefern.

## 2. Chrome-Erweiterung

Das macht Timo selbst, nicht du:
1. `chrome://extensions` öffnen und den Entwicklermodus einschalten.
2. „Entpackte Erweiterung laden“ und `%LOCALAPPDATA%\Programs\Fokus\chrome-ext` wählen. Das muss im selben Chrome-Profil passieren, in dem Spotify läuft.
3. Den Spotify-Tab mit F5 neu laden.

Ist die Fokus-Erweiterung dort schon geladen (gleiche Erweiterung), reicht es, sie auf diesen Ordner umzustellen bzw. mit ↻ neu zu laden.

## 3. Browserquelle in OBS anlegen

Bevorzugt über **obs-websocket** (Protokoll v5, ab OBS 28 eingebaut).

- Einstellungen stehen in `%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json` (`server_enabled`, `server_port`, `auth_required`, `server_password`).
- Ist der Server aus: Timo fragen, dann OBS schließen, `server_enabled: true` setzen und OBS wieder starten.
- Das Passwort nie ändern und nie im Chat ausgeben.

Ablauf:
1. `GetCurrentProgramScene` → `sceneName`; `GetVideoSettings` → `baseWidth`, `baseHeight`.
2. Größe berechnen: `w = round(baseWidth * 0.3125)` (600 bei 1920 Breite), `h = round(w * 260 / 900)`.
3. `GetInputList` prüfen:
   - Gibt es schon eine Quelle „Now Playing“: nur `SetInputSettings` aufrufen, keine zweite anlegen.
   - Gibt es eine alte, kaputte Now-Playing-Quelle (Text-Datei, Widget o. Ä.): Timo fragen, ob sie weg soll. Nicht selbst löschen.
4. `CreateInput`:
   ```json
   {"sceneName": "<sceneName>", "inputName": "Now Playing", "inputKind": "browser_source", "sceneItemEnabled": true,
    "inputSettings": {"url": "http://127.0.0.1:47650/banner", "width": w, "height": h,
                      "fps_custom": true, "fps": 60, "shutdown": false, "restart_when_active": false, "reroute_audio": false}}
   ```
   Custom CSS nicht anfassen. Der Hintergrund ist schon transparent.
5. `SetSceneItemTransform`: `positionX 0`, `positionY baseHeight - h`, `scaleX/scaleY 1`. Den Rand bringt das Banner selbst mit.

Größe ändern:
- Immer über `width`/`height` der Quelle, Verhältnis 900:260. Das Banner skaliert mit der Breite und bleibt so scharf.
- Nicht über die Skalierung in OBS.
- Unter 600 Breite wird die Schrift zu klein.

**Ohne websocket:** Szenen-JSON nicht von Hand bearbeiten. OBS überschreibt die Datei beim Beenden, und neuere Versionen rechnen mit eigenen Feldern (`pos_rel`, `scale_rel`). Stattdessen führst du Timo durch:
1. Quellen → + → Browser → Name „Now Playing“.
2. URL, Breite und Höhe wie oben eintragen.
3. „Quelle herunterfahren, wenn nicht sichtbar“ aus lassen → OK.
4. Die Quelle unten links an den Rand ziehen.

## 4. Prüfen

- `/jetzt` sollte bei laufendem Song so aussehen:
  - `verbunden: true`
  - `titel` ist gesetzt
  - `gefunden.position` ist `"leiste"`
- Steht dort `verbunden: false`, ist die Erweiterung nicht geladen oder der Spotify-Tab nicht neu geladen. Das Timo sagen.
- Dann `GetSourceScreenshot` mit `{"sourceName": "Now Playing", "imageFormat": "png", "imageWidth": w}` und das Bild ansehen:
  - Läuft ein Song, ist eine Karte mit Cover, Titel, Künstler und Zeitleiste zu sehen.
  - Läuft nichts oder ist mehr als 20 s pausiert, ist das Bild leer. Das ist so gewollt.
- Die Schrift lädt von Google Fonts, dafür braucht der PC Internet.

## 5. Rückmeldung an Timo

- Szene, Größe und Position der Quelle
- Was der Screenshot zeigt
- Was er noch selbst tun muss (Erweiterung laden, Spotify-Tab neu laden)
