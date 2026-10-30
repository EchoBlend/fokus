"""Selbstcheck Fokus: python test_fokus.py"""
import json
import tempfile
import time
from pathlib import Path

import base64
import os
import socket
import struct
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from PIL import Image

from fokus import CW, EW, M, VERSION, Api, Spotify, Timer, lrc_zeilen, mmss, zeilen_teilen, zeichnen, zeile_bei


class Uhr:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def main():
    tmp = Path(tempfile.mkdtemp()) / "bloecke.jsonl"
    uhr = Uhr(1_800_000_000.0)          # fester Zeitpunkt (mittags)
    t = Timer(tmp, uhr)
    assert t.stand()["status"] == "aus" and t.stand()["heute_s"] == 0

    t.start(minuten=1, titel="Test")
    uhr.t += 30
    s = t.stand()
    assert s["lauf_s"] == 30 and s["rest_s"] == 30 and not s["faellig"], s

    t.anhalten()                         # Block 1 zu (30 s)
    uhr.t += 100                         # angehalten zaehlt nicht
    assert t.stand()["lauf_s"] == 30
    t.anhalten()                         # weiter
    uhr.t += 45
    s = t.stand()
    assert s["lauf_s"] == 75 and s["faellig"] and s["rest_s"] == -15, s

    t.pause(minuten=2)                   # Arbeit zu, Pause laeuft
    uhr.t += 60
    s = t.stand()
    assert s["status"] == "pause" and s["rest_s"] == 60 and s["heute_s"] == 75, s
    assert s["fokus_bloecke"] == 2 and len(s["segmente"]) == 3
    t.start()                            # Pause zu, neuer Fokus, Titel bleibt
    assert t.titel == "Test" and t.stand()["lauf_s"] == 0
    t.stop()

    zeilen = [json.loads(z) for z in tmp.read_text("utf-8").splitlines()]
    assert [(round(b["ende"] - b["start"]), b["art"]) for b in zeilen] == \
        [(30, "arbeit"), (45, "arbeit"), (60, "pause"), (0, "arbeit")]

    # neuer Timer liest den Tag aus der Ablage
    assert Timer(tmp, uhr).stand()["heute_s"] == 75
    assert mmss(75) == "1:15" and mmss(3725) == "1:02:05"

    # Malen: zu und auf, Ecken durchsichtig, Trefferflaechen da
    t.start(titel="Test")
    for m, erwartet in ((0.0, "aufklappen"), (1.0, "primaer")):
        ui = {"m": m, "anker": "r", "hover": None, "tab": "fokus", "puls": 0.0, "jetzt": uhr.t,
              "musik": {"verbunden": True, "titel": "Song", "kuenstler": "Band", "spielt": True,
                        "lautstaerke": 0.5}}
        t0 = time.perf_counter()
        bild, zonen = zeichnen(t.stand(), ui, 2.0, Image.new("RGB", (64, 64), (200, 30, 90)))
        ms = (time.perf_counter() - t0) * 1000
        assert bild[0, 0, 3] == 0 and any(z[0] == erwartet for z in zonen), zonen
        print(f"m={m}: {ms:.0f} ms")
    # zugeklappte Pille sitzt bei Anker rechts rechtsbuendig
    _, zonen = zeichnen(t.stand(), dict(ui, m=0.0), 1.0)
    knopf = next(z for z in zonen if z[0] == "aufklappen")
    assert abs(knopf[3] - (M + EW - 6)) < 2, knopf
    assert CW < EW
    assert {"cover", "aufklappen"} <= {z[0] for z in zonen}
    _, zonen = zeichnen(t.stand(), dict(ui, m=1.0), 1.0)
    assert {"playpause", "weiter", "zurueck", "lautstaerke"} <= {z[0] for z in zonen}
    # Favorit: Herz-Zone nur, wenn der Tab das Herz kennt; Hover malt das Herz aufs Cover
    assert "favorit" not in {z[0] for z in zonen}
    fav = dict(ui, m=1.0, musik=dict(ui["musik"], favorit=False))
    _, zonen = zeichnen(t.stand(), fav, 2.0, Image.new("RGB", (64, 64)))
    assert "favorit" in {z[0] for z in zonen}
    ohne, _ = zeichnen(t.stand(), fav, 2.0, Image.new("RGB", (64, 64)))
    mit, _ = zeichnen(t.stand(), dict(fav, hover="favorit"), 2.0, Image.new("RGB", (64, 64)))
    fz = next(z for z in zonen if z[0] == "favorit")
    hell = lambda b: (b[int(fz[2]):int(fz[4]), int(fz[1]):int(fz[3]), :3].min(axis=2) > 200).sum()
    assert hell(mit) > 100 and hell(ohne) == 0, (hell(mit), hell(ohne))     # weisses Herz nur beim Hover
    _, zonen = zeichnen(t.stand(), dict(fav, m=0.0, hv=1.0), 1.0)
    assert "favorit" in {z[0] for z in zonen}
    gesendet = []
    sp = Spotify()
    sp.ws = type("W", (), {"senden": lambda self, d: gesendet.append(d)})()
    sp.stand = {"favorit": False}
    assert sp.befehl("favorit") and sp.stand["favorit"] is True and gesendet[-1]["cmd"] == "favorit"
    assert sp.befehl("favorit") and sp.stand["favorit"] is False and len(gesendet) == 2   # zweiter Klick entfernt
    sp.stand = {"favorit": None}
    assert not sp.befehl("favorit") and len(gesendet) == 2
    # Erweiterung einer anderen Version: einmal neu laden lassen, nicht in Schleife
    sp.empfangen({"verbunden": True, "version": "alt"})
    sp.empfangen({"verbunden": True, "version": "alt"})
    assert [g["cmd"] for g in gesendet[2:]] == ["neuladen"], gesendet
    manifest = json.loads((Spotify.ORDNER / "manifest.json").read_text("utf-8"))
    assert manifest["version"] == VERSION, "chrome-ext/manifest.json: version = VERSION setzen"
    ws_test()
    print("ok")


def _rahmen(text):
    """Client-Rahmen: maskiert, wie ein Browser."""
    d, maske = text.encode(), os.urandom(4)
    n = len(d)
    kopf = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
    return kopf + maske + bytes(x ^ maske[i % 4] for i, x in enumerate(d))


def _lesen(sock):
    b1, b2 = sock.recv(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", sock.recv(2))[0]
    daten = b""
    while len(daten) < n:
        daten += sock.recv(n - len(daten))
    return json.loads(daten)


def ws_test():
    """Erweiterung simulieren: verbinden, Stand melden, Befehl ueber /spotify bekommen."""
    class StubApp:
        spotify = Spotify()
        letzter_stand = {}
    Api.app = StubApp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    s = socket.create_connection(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    zeilen = ["GET /ws HTTP/1.1", "Host: x", "Upgrade: websocket", "Connection: Upgrade",
              f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "Origin: chrome-extension://abc"]
    s.sendall(("\r\n".join(zeilen) + "\r\n\r\n").encode())
    kopf = b""
    while b"\r\n\r\n" not in kopf:
        kopf += s.recv(1)
    assert b"101" in kopf.split(b"\r\n")[0], kopf
    assert _lesen(s) == {"cmd": "stand"}                      # App fragt sofort nach dem Stand
    s.sendall(_rahmen(json.dumps({"verbunden": True, "version": VERSION, "titel": "Lied", "spielt": False, "lautstaerke": 0.3})))
    time.sleep(0.2)
    assert StubApp.spotify.stand["titel"] == "Lied"

    def antworten():
        cmd = _lesen(s)
        assert cmd == {"cmd": "lautstaerke", "wert": 0.8}, cmd
        s.sendall(_rahmen(json.dumps({"verbunden": True, "version": VERSION, "titel": "Lied", "spielt": False, "lautstaerke": 0.8})))
    th = threading.Thread(target=antworten)
    th.start()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/spotify", method="POST",
                                 data=json.dumps({"cmd": "lautstaerke", "wert": 0.8}).encode())
    antwort = json.loads(urllib.request.urlopen(req, timeout=5).read())
    th.join(3)
    assert antwort["lautstaerke"] == 0.8 and "bild" not in antwort, antwort

    # Webseiten duerfen nicht steuern
    req = urllib.request.Request(f"http://127.0.0.1:{port}/spotify", method="POST",
                                 data=b'{"cmd":"playpause"}', headers={"Origin": "https://boese.example"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "fremde Seite durfte steuern"
    except urllib.error.HTTPError as e:
        assert e.code == 403
    s.close()
    time.sleep(0.2)
    assert StubApp.spotify.stand == {"verbunden": False}       # Trennung bemerkt
    srv.shutdown()

    # Songtext: LRC-Zeilen und aktuelle Zeile
    z = lrc_zeilen("[00:01.00]Eins\n[00:03.50][00:09.00]Zwei\n[00:05.00]\n[ar:Kuenstler]")
    assert z == [(1.0, "Eins"), (3.5, "Zwei"), (5.0, ""), (9.0, "Zwei")], z
    g = zeilen_teilen([(10.0, "aaaa bbbb cccc dddd"), (20.0, "kurz")], lambda x: len(x) <= 10)
    assert [(round(t), x) for t, x in g] == [(10, "aaaa bbbb"), (15, "cccc dddd"), (20, "kurz")], g
    assert zeile_bei(z, 0.5) is None
    assert zeile_bei(z, 2) == "Eins"
    assert zeile_bei(z, 4) == "Zwei"
    assert zeile_bei(z, 6) is None          # Instrumental-Luecke
    assert zeile_bei(z, 99) == "Zwei"

    # Kompass-Export: buendeln je Tag+Titel, Pausen/Kurzes raus, Merker zaehlt
    from fokus_kompass import offen
    t0 = time.mktime((2026, 10, 30, 9, 0, 0, 0, 0, -1))
    bl = [{"start": t0, "ende": t0 + 600, "art": "arbeit", "titel": "A", "aufgabe_id": 7},
          {"start": t0 + 700, "ende": t0 + 1300, "art": "arbeit", "titel": "A", "aufgabe_id": 7},
          {"start": t0 + 1300, "ende": t0 + 1900, "art": "pause", "titel": "A", "aufgabe_id": 7},
          {"start": t0 + 2000, "ende": t0 + 2060, "art": "arbeit", "titel": "", "aufgabe_id": None}]
    o = offen(bl, 0)
    assert o["bis"] == t0 + 2060
    assert [(p["titel"], p["minuten"], p["bloecke"], p["von"]) for p in o["posten"]] == [("A", 20, 2, "09:00")], o
    assert offen(bl, t0 + 600)["posten"][0]["minuten"] == 10
    assert offen(bl, t0 + 2060) == {"bis": t0 + 2060, "posten": []}
    print("ok")


if __name__ == "__main__":
    main()
