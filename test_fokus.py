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
from types import SimpleNamespace

import numpy as np
from PIL import Image

from fokus import CW, EW, M, TOENE, VERSION, Api, App, primaer, ton, Spotify, Timer, lrc_zeilen, mmss, zeilen_teilen, zeichnen, zeile_bei


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

    # Stoppuhr: zaehlt nach oben, wird nie faellig, landet als Arbeitsblock
    t.start(titel="Offen", offen=True)
    uhr.t += 5000                        # weit ueber ziel_min (1)
    s = t.stand()
    assert s["offen"] and not s["faellig"] and s["lauf_s"] == 5000, s
    from fokus import anzeige
    assert anzeige(s) == ("1:23:20", "Offen"), anzeige(s)
    t.stop()
    assert not t.offen and json.loads(tmp.read_text("utf-8").splitlines()[-1])["art"] == "arbeit"
    tmp.write_text("\n".join(tmp.read_text("utf-8").splitlines()[:-1]) + "\n", "utf-8")

    # Pausen-Stoppuhr: zaehlt hoch, nie faellig, anhaltbar, landet als Pausenblock
    t.pause(offen=True)
    uhr.t += 5000
    s = t.stand()
    assert s["offen"] and s["art"] == "pause" and not s["faellig"] and anzeige(s) == ("1:23:20", "Pause"), s
    t.anhalten()
    assert anzeige(t.stand()) == ("1:23:20", "angehalten")
    t.anhalten()
    assert t.status == "pause" and t.offen
    t.stop()
    assert json.loads(tmp.read_text("utf-8").splitlines()[-1])["art"] == "pause"
    tmp.write_text("\n".join(tmp.read_text("utf-8").splitlines()[:-2]) + "\n", "utf-8")

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
    # Songwechsel in der kleinen Pille: nur ueberblenden, der Cover-Umriss bleibt stehen (sonst zittert er)
    cz = next(z for z in zonen if z[0] == "cover")
    bunt = lambda b: np.argwhere(np.ptp(b[int(cz[2]):int(cz[4]), int(cz[1]) - 20:int(cz[3]) + 20, :3], axis=2) > 60)
    rot, blau = Image.new("RGB", (64, 64), (220, 30, 30)), Image.new("RGB", (64, 64), (30, 30, 220))
    ruhe = bunt(zeichnen(t.stand(), dict(ui, m=0.0), 1.0, blau)[0])
    for cp in (0.3, 0.6):
        mitten = bunt(zeichnen(t.stand(), dict(ui, m=0.0, cp=cp), 1.0, blau, rot)[0])
        assert (mitten.min(0) == ruhe.min(0)).all() and (mitten.max(0) == ruhe.max(0)).all(), (cp, mitten.min(0), ruhe.min(0))
    _, zonen = zeichnen(t.stand(), dict(ui, m=1.0), 1.0)
    assert {"playpause", "weiter", "zurueck", "lautstaerke", "auswertung"} <= {z[0] for z in zonen}
    ohne, _ = zeichnen(t.stand(), dict(ui, m=1.0), 1.0)
    mit, _ = zeichnen(t.stand(), dict(ui, m=1.0, hover="auswertung"), 1.0)
    assert (mit != ohne).any()                                    # Hover zeigt "Auswertung"
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
    # Songtext-Takt: Position ab dem Zeitpunkt, den der Tab mitschickt (pos_t), nicht ab Empfang
    sp2 = Spotify()
    sp2.empfangen({"verbunden": True, "version": VERSION, "pos": 10.25, "pos_t": (time.time() - 2) * 1000, "spielt": True})
    assert abs(sp2.position() - 12.25) < 0.05, sp2.position()
    sp2.empfangen({"verbunden": True, "version": VERSION, "pos": 12.4, "pos_t": (time.time() - 5) * 1000, "spielt": False})
    assert sp2.position() == 12.4                      # angehalten: steht
    manifest = json.loads((Spotify.ORDNER / "manifest.json").read_text("utf-8"))
    assert manifest["version"] == VERSION, "chrome-ext/manifest.json: version = VERSION setzen"
    # Pause verhaelt sich wie Arbeit: anhalten, weiter (bleibt Pause, Zeit laeuft weiter)
    pu = Uhr(1_800_000_000.0)
    pt = Timer(Path(tempfile.mkdtemp()) / "b.jsonl", pu)
    pt.pause(30)
    pu.t += 60
    pt.anhalten()
    pu.t += 500                          # angehalten zaehlt nicht
    s = pt.stand()
    assert s["status"] == "angehalten" and s["art"] == "pause" and s["rest_s"] == 30 * 60 - 60, s
    assert primaer(s, "pause") == ("Weiter", "pause") and ton(s) == TOENE["angehalten"]
    pt.anhalten()
    assert pt.status == "pause" and ton(pt.stand()) == TOENE["pause"]
    assert primaer(pt.stand(), "pause") == ("Anhalten", "neutral")
    # Klick auf die zugeklappte Pille in der Pause haelt an, startet keinen Fokus
    befehle = []
    pille = SimpleNamespace(timer=pt, feder=SimpleNamespace(ziel=0), ausfuehren=befehle.append)
    App._klick(pille, None)
    assert befehle == [{"aktion": "anhalten"}], befehle
    ws_test()
    auswertung_test()
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
    zeilen = ["GET /ws HTTP/1.1", "Host: 127.0.0.1", "Upgrade: websocket", "Connection: Upgrade",
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
    sp = Spotify()                          # Umschalten nie vor dem Zeitstempel
    sp.text_an, sp.text = True, [(0.0, "a"), (5.0, "b")]
    sp.stand = {"pos": 5.0, "spielt": False}
    assert sp.index() == 0
    sp.stand = {"pos": 5.5, "spielt": False}
    assert sp.index() == 1

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


def auswertung_test():
    """tage(): 4-Uhr-Grenze, Luecken bis 90 min = Pause, Pause-Block nicht doppelt; /tage, /auswertung, fremder Host."""
    from fokus import tage, PAUSE_MAX_S
    t0 = time.mktime((2026, 11, 2, 22, 0, 0, 0, 0, -1))           # Mo 22:00
    bl = [{"start": t0, "ende": t0 + 3600, "art": "arbeit", "titel": "A"},            # 22:00-23:00
          {"start": t0 + 3600, "ende": t0 + 4200, "art": "pause"},                     # Pause-Block in der Luecke
          {"start": t0 + 4800, "ende": t0 + 4 * 3600, "art": "arbeit"},                # 23:20-02:00 (Nacht)
          {"start": t0 + 4 * 3600 + PAUSE_MAX_S + 60, "ende": t0 + 4 * 3600 + PAUSE_MAX_S + 660, "art": "arbeit"}]  # nach 91 min
    d = tage(bl)
    assert list(d) == ["2026-11-02"], d.keys()                    # alles vor 4 Uhr = Vortag
    t = d["2026-11-02"]
    assert t["arbeit_s"] == 3600 + 9600 + 600 and t["pause_s"] == 1200, t   # 91-min-Luecke ist keine Pause
    assert t["beginn_h"] == 22 and abs(t["ende_h"] - (27 + 41 / 60)) < 1e-3, t
    assert t["stunden"][22] == 3600 and t["stunden"][23] == 2400 and t["stunden"][0] == 3600 and t["stunden"][3] == 600
    assert t["bloecke"][0] == [22.0, 23.0, "A"] and t["pausen"] == [[23.0, round(23 + 1 / 3, 4)]], t
    g = tage([{"start": t0 + 5 * 3600 + 3000, "ende": t0 + 6 * 3600 + 600, "art": "arbeit"}])   # 03:50-04:10
    assert {k: v["arbeit_s"] for k, v in g.items()} == {"2026-11-02": 600, "2026-11-03": 600}, g
    g = tage([{"start": t0, "ende": t0 + 600, "art": "arbeit"}, {"start": t0 + 600.7, "ende": t0 + 1200, "art": "arbeit"}])
    assert len(g["2026-11-02"]["bloecke"]) == 1 and g["2026-11-02"]["pausen"] == [], g   # 0,7 s Neustart = ein Block

    ablage = Path(tempfile.mkdtemp()) / "b.jsonl"
    ablage.write_text("".join(json.dumps(b) + "\n" for b in bl), "utf-8")
    uhr = Uhr(t0 + 6 * 3600)                                      # Di 04:00 -> Arbeitstag 03.11.
    StubApp = SimpleNamespace(timer=Timer(ablage, uhr), cfg={"tagesziel_h": 5})
    StubApp.timer.start()
    uhr.t += 600                                                  # laufender Block zaehlt schon mit
    Api.app = StubApp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/tage", timeout=5).read())
    assert a["heute"] == "2026-11-03" and a["laeuft"] and a["ziel_h"] == 5, a
    assert a["tage"]["2026-11-03"]["arbeit_s"] == 600 and a["tage"]["2026-11-02"]["arbeit_s"] == 13800, a
    seite = urllib.request.urlopen(f"http://127.0.0.1:{port}/auswertung", timeout=5).read().decode("utf-8")
    assert "<title>Fokus · Auswertung</title>" in seite
    try:                                                          # DNS-Rebinding: fremder Hostname
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/tage",
                                                      headers={"Host": "boese.example"}), timeout=5)
        assert False, "fremder Host durfte lesen"
    except urllib.error.HTTPError as e:
        assert e.code == 403
    srv.shutdown()


if __name__ == "__main__":
    main()
