"""Fokus – schwebende Worktimer-Pille.

Pille (echtes Alpha, verschiebbar, immer oben), klappt per Knopf zu einem Panel
auf: Fokus/Pause, Zeit frei einstellen, Start/Anhalten/Stopp, Heute-Leiste.
Bloecke landen in %APPDATA%\\Fokus\\bloecke.jsonl. Lokale HTTP-Schnittstelle fuer Claude:

  GET  http://127.0.0.1:47650/stand
  POST http://127.0.0.1:47650/timer  {"aktion": "start|anhalten|stop|pause|ziel", "minuten": 50, "titel": "..."}
  POST http://127.0.0.1:47650/spotify {"cmd": "playpause|weiter|zurueck|lautstaerke", "wert": 0.4}

Spotify laeuft im Chrome-Tab; die Erweiterung in chrome-ext/ verbindet sich per
WebSocket (/ws) und fuehrt die Befehle im Tab aus.
"""
import base64
import hashlib
import struct
import urllib.request
import ctypes
import ctypes.wintypes as wt
import io
import json
import math
import os
import queue
import sys
import threading
import re
import time
import tkinter as tk
import urllib.parse
import winsound
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERSION = "0.1.2"
PORT = 47650
DIR = Path(os.environ.get("APPDATA", Path.home())) / "Fokus"
BLOECKE = DIR / "bloecke.jsonl"
CONFIG = DIR / "config.json"

CW, CH = 276, 48        # Pille zu (logische Pixel)
EW, EH = 344, 404       # Panel auf
M = 24                  # Rand fuer den Schatten
WW, WH = EW + 2 * M, EH + 2 * M

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000


# ---------------------------------------------------------------------------
# Timer (reine Logik, testbar mit eigener Uhr)
# ---------------------------------------------------------------------------

def heute_start(now):
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def lade_bloecke(pfad=BLOECKE):
    try:
        return [json.loads(z) for z in Path(pfad).read_text("utf-8").splitlines() if z.strip()]
    except FileNotFoundError:
        return []


class Timer:
    """status: aus | arbeit | angehalten | pause. Ein Block = ein durchgehendes Stueck
    Arbeit oder Pause; anhalten schliesst ihn, weiter oeffnet einen neuen."""

    def __init__(self, ablage=BLOECKE, uhr=time.time):
        self.ablage, self.uhr = Path(ablage), uhr
        self.status = "aus"
        self.ziel_min, self.pause_min = 50, 10
        self.titel, self.aufgabe_id = "", None
        self.seit = None          # Beginn des laufenden Blocks
        self.gesammelt = 0.0      # Sekunden der laufenden Phase aus geschlossenen Bloecken
        h0 = heute_start(self.uhr())
        self.heute = [(max(b["start"], h0), b["ende"], b.get("art", "arbeit"))
                      for b in lade_bloecke(self.ablage) if b["ende"] > h0]

    def start(self, minuten=None, titel=None, aufgabe_id=None):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        if minuten:
            self.ziel_min = int(minuten)
        if titel is not None:
            self.titel, self.aufgabe_id = titel, aufgabe_id
        self.gesammelt, self.seit, self.status = 0.0, self.uhr(), "arbeit"

    def pause(self, minuten=None):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        if minuten:
            self.pause_min = int(minuten)
        self.gesammelt, self.seit, self.status = 0.0, self.uhr(), "pause"

    def anhalten(self):
        """Umschalter: laufend -> angehalten -> laufend."""
        if self.status == "arbeit":
            self._block_schliessen()
            self.status = "angehalten"
        elif self.status == "angehalten":
            self.seit, self.status = self.uhr(), "arbeit"

    def stop(self):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        self.status, self.gesammelt, self.seit = "aus", 0.0, None

    def _block_schliessen(self):
        ende = self.uhr()
        art = "pause" if self.status == "pause" else "arbeit"
        self.gesammelt += ende - self.seit
        self.heute.append((self.seit, ende, art))
        self.ablage.parent.mkdir(parents=True, exist_ok=True)
        with self.ablage.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"start": round(self.seit, 1), "ende": round(ende, 1), "art": art,
                                "titel": self.titel, "aufgabe_id": self.aufgabe_id},
                               ensure_ascii=False) + "\n")
        self.seit = None

    def stand(self):
        now = self.uhr()
        h0 = heute_start(now)
        segmente = [s for s in self.heute if s[1] > h0]
        if self.seit is not None:
            segmente.append((self.seit, now, "pause" if self.status == "pause" else "arbeit"))
        lauf = self.gesammelt + (now - self.seit if self.seit is not None else 0.0)
        ziel = self.pause_min if self.status == "pause" else self.ziel_min
        return {
            "status": self.status,
            "titel": self.titel,
            "aufgabe_id": self.aufgabe_id,
            "ziel_min": self.ziel_min,
            "pause_min": self.pause_min,
            "lauf_s": int(lauf),
            "rest_s": int(ziel * 60 - lauf),
            "faellig": self.status != "aus" and lauf >= ziel * 60,
            "heute_s": int(sum(e - max(s, h0) for s, e, a in segmente if a == "arbeit")),
            "fokus_bloecke": sum(1 for s in segmente if s[2] == "arbeit"),
            "segmente": [[round(s, 1), round(e, 1), a] for s, e, a in segmente],
        }


def mmss(s):
    s = abs(int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def stunden(s):
    return f"{s // 3600}:{s // 60 % 60:02d} h" if s >= 3600 else f"{s // 60} min"


# ---------------------------------------------------------------------------
# Echtes Alpha-Fenster (aus FreeFlow: freeflow.py Ebene, gekuerzt)
# ---------------------------------------------------------------------------

class _BLEND(ctypes.Structure):
    _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                ("alpha", ctypes.c_ubyte), ("fmt", ctypes.c_ubyte)]


class _BMI(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("w", wt.LONG), ("h", wt.LONG), ("planes", wt.WORD),
                ("bits", wt.WORD), ("comp", wt.DWORD), ("sizeimg", wt.DWORD), ("xppm", wt.LONG),
                ("yppm", wt.LONG), ("used", wt.DWORD), ("imp", wt.DWORD)]


class Ebene:
    """Toplevel mit echtem Alpha per UpdateLayeredWindow. Nie Tk -alpha benutzen:
    das setzt SetLayeredWindowAttributes, danach scheitert jedes Update (Fehler 87).
    Eigene DLL-Instanzen, damit gesetzte argtypes niemand anderen stoeren.
    Durchsichtige Pixel sind klickdurchlaessig."""

    def __init__(self, top):
        self.u, self.g = u, g = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        u.GetDC.restype = wt.HDC
        g.CreateCompatibleDC.argtypes = [wt.HDC]
        g.CreateCompatibleDC.restype = wt.HDC
        g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g.SelectObject.restype = wt.HGDIOBJ
        g.DeleteObject.argtypes = [wt.HGDIOBJ]
        g.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wt.HANDLE, wt.DWORD]
        g.CreateDIBSection.restype = wt.HBITMAP
        u.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_void_p, ctypes.c_void_p, wt.HDC,
                                          ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
        top.update_idletasks()
        self.hwnd = int(top.frame(), 16)
        stil = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, stil | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._dc = u.GetDC(0)
        self._mem = g.CreateCompatibleDC(self._dc)
        self._bmp, self.bits, self.w, self.h = None, ctypes.c_void_p(), 0, 0

    def _groesse(self, w, h):
        if (w, h) == (self.w, self.h):
            return
        bmi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = self.g.CreateDIBSection(self._dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        self.g.SelectObject(self._mem, bmp)
        if self._bmp:
            self.g.DeleteObject(self._bmp)
        self._bmp, self.bits, self.w, self.h = bmp, bits, w, h

    def zeigen(self, bgra):
        """bgra: uint8 (h, w, 4), vormultipliziert."""
        h, w = bgra.shape[:2]
        self._groesse(w, h)
        ctypes.memmove(self.bits, bgra.ctypes.data, bgra.nbytes)
        size, src = wt.SIZE(w, h), wt.POINT(0, 0)
        return bool(self.u.UpdateLayeredWindow(self.hwnd, self._dc, None, ctypes.byref(size), self._mem,
                                               ctypes.byref(src), 0, ctypes.byref(_BLEND(0, 0, 255, 1)), 2))


# ---------------------------------------------------------------------------
# Malen: Formen als Abstandsfelder (numpy) -> kantenglatt ohne Supersampling
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def schrift(px, gewicht):
    """Segoe UI Variable mit echter Strichstaerke; Rueckfall auf statische Segoe-Schnitte."""
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\SegUIVar.ttf", px)
        f.set_variation_by_axes([gewicht])
        return f
    except (OSError, ValueError, AttributeError):
        return ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf" if gewicht >= 550 else
                                  r"C:\Windows\Fonts\segoeui.ttf", px)


@lru_cache(maxsize=16)
def symbolschrift(px):
    for f in ("SegoeIcons.ttf", "segmdl2.ttf"):
        try:
            return ImageFont.truetype(rf"C:\Windows\Fonts\{f}", px)
        except OSError:
            pass
    return schrift(px, 400)


@lru_cache(maxsize=8)
def schrift_symbol(px):
    return ImageFont.truetype(r"C:\Windows\Fonts\seguisym.ttf", px)


AUF, ZU, PLAY, PAUSE, STOP, PLUS, MINUS = "\uE70E", "\uE70D", "\uE768", "\uE769", "\uE71A", "\uE710", "\uE738"
ZURUECK, WEITER, MIKRO = "\uE892", "\uE893", "\uE720"


def _glatt(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _box_abstand(xx, yy, x0, y0, x1, y1, r):
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    r = min(r, hw, hh)
    qx, qy = np.abs(xx - cx) - (hw - r), np.abs(yy - cy) - (hh - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


class Leinwand:
    """Vormultiplizierte RGBA-Flaeche (float 0..1). Jede Form rechnet nur in ihrem
    Rechteck — sonst kostet eine 60-fps-Animation bei 200 % Skalierung zu viel."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.a = np.zeros((4, h, w), np.float32)     # Ebenen getrennt: numpy rechnet zusammenhaengend

    def _feld(self, x0, y0, x1, y1):
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.w, int(math.ceil(x1))), min(self.h, int(math.ceil(y1)))
        if x1 <= x0 or y1 <= y0:
            return None
        # Zeile und Spalte statt voller Gitter: numpy rechnet per Broadcasting
        xx = np.arange(x0, x1, dtype=np.float32)[None, :] + 0.5
        yy = np.arange(y0, y1, dtype=np.float32)[:, None] + 0.5
        return (slice(y0, y1), slice(x0, x1)), xx, yy

    def _ueber(self, sl, farbe, cov):
        """farbe: (r,g,b,a) 0..255 oder Array (...,3) 0..255 mit Alpha in cov."""
        if isinstance(farbe, tuple):
            cov = cov * (farbe[3] / 255.0)
            kanaele = [cov * (k / 255.0) for k in farbe[:3]]
        else:
            kanaele = [cov * (farbe[..., i] / 255.0) for i in range(3)]
        rest = 1 - cov
        for i, k in enumerate(kanaele + [cov]):
            ziel = self.a[i][sl]
            ziel *= rest
            ziel += k

    def box(self, x0, y0, x1, y1, r, farbe, maske=None):
        f = self._feld(x0 - 1, y0 - 1, x1 + 1, y1 + 1)
        if f is None:
            return
        sl, xx, yy = f
        cov = np.clip(0.5 - _box_abstand(xx, yy, x0, y0, x1, y1, r), 0, 1)
        self._ueber(sl, farbe, cov)

    def schatten(self, x0, y0, x1, y1, r, weich, farbe):
        f = self._feld(x0 - 2 * weich, y0 - 2 * weich, x1 + 2 * weich, y1 + 2 * weich)
        if f is None:
            return
        sl, xx, yy = f
        cov = 1 - _glatt(-weich, weich, _box_abstand(xx, yy, x0, y0, x1, y1, r))
        self._ueber(sl, farbe, cov)

    def rand(self, x0, y0, x1, y1, r, oben, unten, dicke=1.0):
        """Feine Kante, oben heller als unten (Glas-Lichtkante)."""
        f = self._feld(x0 - 1, y0 - 1, x1 + 1, y1 + 1)
        if f is None:
            return
        sl, xx, yy = f
        d = _box_abstand(xx, yy, x0, y0, x1, y1, r)
        cov = np.clip(dicke - np.abs(d + dicke / 2 + 0.25), 0, 1)
        t = np.clip((yy - y0) / max(1.0, y1 - y0), 0, 1)
        cov *= oben + (unten - oben) * np.sqrt(t)
        self._ueber(sl, (255, 255, 255, 255), cov)

    def kreis(self, cx, cy, r, farbe):
        self.box(cx - r, cy - r, cx + r, cy + r, r, farbe)

    def schein(self, cx, cy, r, farbe, clip=None):
        """Weicher Lichtfleck; clip=(x0,y0,x1,y1,rad) begrenzt ihn auf eine Form."""
        f = self._feld(cx - r, cy - r, cx + r, cy + r)
        if f is None:
            return
        sl, xx, yy = f
        cov = (1 - _glatt(0, r, np.hypot(xx - cx, yy - cy))) ** 2
        if clip:
            cov *= np.clip(0.5 - _box_abstand(xx, yy, *clip), 0, 1)
        self._ueber(sl, farbe, cov)

    def ring(self, cx, cy, R, dicke, anteil, c1, c2, spur, alpha=1.0):
        """Fortschrittsring mit Farbverlauf, runden Enden und leuchtendem Kopf."""
        f = self._feld(cx - R - dicke * 3, cy - R - dicke * 3, cx + R + dicke * 3, cy + R + dicke * 3)
        if f is None:
            return
        sl, xx, yy = f
        dx, dy = xx - cx, yy - cy
        dist = np.hypot(dx, dy)
        band = np.clip(dicke / 2 + 0.5 - np.abs(dist - R), 0, 1)
        self._ueber(sl, spur[:3] + (int(spur[3] * alpha),), band)
        if anteil <= 0:
            return
        anteil = min(anteil, 1.0)
        w = (np.arctan2(dx, -dy) % (2 * np.pi)) / (2 * np.pi)          # 0 oben, im Uhrzeigersinn
        bogen = band * np.clip((anteil - w) * 2 * np.pi * R + 0.5, 0, 1)
        hx, hy = cx + R * math.sin(anteil * 2 * math.pi), cy - R * math.cos(anteil * 2 * math.pi)
        kopf = np.clip(dicke / 2 + 0.5 - np.hypot(xx - hx, yy - hy), 0, 1)
        anfang = np.clip(dicke / 2 + 0.5 - np.hypot(xx - cx, yy - (cy - R)), 0, 1)
        t = np.clip(w / anteil, 0, 1)[..., None]
        t = np.where((kopf > 0)[..., None] & (w < 0.25 * anteil)[..., None], 1.0, t)
        farbe = np.array(c1[:3], np.float32) * (1 - t) + np.array(c2[:3], np.float32) * t
        # Schein um den Kopf
        glanz = (1 - _glatt(0, dicke * 2.6, np.hypot(xx - hx, yy - hy))) ** 2 * 0.55 * alpha
        self._ueber(sl, c2[:3] + (255,), glanz)
        self._ueber(sl, farbe, np.maximum(np.maximum(bogen, kopf), anfang) * alpha)

    def text(self, x, y, s, font, farbe, anker="ls"):
        if not s or farbe[3] <= 0:
            return
        l, t, r, b = font.getbbox(s, anchor=anker)
        maske = Image.new("L", (max(1, r - l), max(1, b - t)))
        ImageDraw.Draw(maske).text((-l, -t), s, font=font, fill=255, anchor=anker)
        x0, y0 = int(round(x)) + l, int(round(y)) + t
        f = self._feld(x0, y0, x0 + maske.width, y0 + maske.height)
        if f is None:
            return
        sl, _, _ = f
        m = np.asarray(maske, np.float32) / 255.0
        m = m[sl[0].start - y0:sl[0].stop - y0, sl[1].start - x0:sl[1].stop - x0]
        self._ueber(sl, farbe, m)

    def bild(self, x0, y0, groesse, r, img, alpha=1.0):
        """Quadratisches Bild (PIL) mit runden Ecken."""
        n = max(1, round(groesse))
        px = np.asarray(img.convert("RGB").resize((n, n), Image.BILINEAR), np.float32)
        x0, y0 = round(x0), round(y0)
        f = self._feld(x0, y0, x0 + n, y0 + n)
        if f is None:
            return
        sl, xx, yy = f
        cov = np.clip(0.5 - _box_abstand(xx, yy, x0, y0, x0 + n, y0 + n, r), 0, 1) * alpha
        self._ueber(sl, px[sl[0].start - y0:sl[0].stop - y0, sl[1].start - x0:sl[1].stop - x0], cov)

    def bgra(self):
        return (np.stack([self.a[2], self.a[1], self.a[0], self.a[3]], -1) * 255 + 0.5).astype(np.uint8)


# Farben (Apple-System-Toene, dunkel)
WEISS = (245, 245, 247)
GRAU = (142, 142, 150)
TOENE = {
    "arbeit": ((48, 209, 88), (172, 240, 130)),
    "faellig": ((255, 149, 0), (255, 214, 10)),
    "pause": ((10, 132, 255), (100, 210, 255)),
    "angehalten": ((110, 110, 118), (170, 170, 178)),
    "aus": ((110, 110, 118), (170, 170, 178)),
}


def a(rgb, alpha):
    return tuple(rgb) + (max(0, min(255, int(alpha))),)


def ton(st):
    if st["faellig"]:
        return TOENE["pause" if st["status"] == "pause" else "faellig"]
    return TOENE[st["status"]]


def kuerzen(s, font, breite):
    while s and font.getlength(s) > breite:
        s = s[:-2] + "…"
    return s


_GRUND = {}   # Schatten + Glas je Ruhelage


def zeichnen(st, ui, z, cover=None):
    """st = Timer.stand(); ui = {m, anker, hover, tab, puls, jetzt, musik}; cover = PIL-Bild.
    Gibt (BGRA, Trefferflaechen) zurueck; Flaechen in Fensterpixeln."""
    S = lambda v: v * z
    lw = Leinwand(round(S(WW)), round(S(WH)))
    m = ui["m"]
    w, h = CW + (EW - CW) * m, CH + (EH - CH) * m
    x0 = M if ui["anker"] == "l" else M + EW - w
    y0 = M
    r = min(h / 2, CH / 2 + (26 - CH / 2) * min(1.0, max(0.0, m)) * 1.0)
    X0, Y0, X1, Y1, R = S(x0), S(y0), S(x0 + w), S(y0 + h), S(r)
    zonen = []
    hov = ui["hover"]

    def zone(name, *rect):
        zonen.append((name,) + tuple(S(v) for v in rect))
        return name == hov

    c1, c2 = ton(st)
    puls = ui["puls"]

    # Grund: Schatten, Glas, Lichtkante, Farbschein
    grund_key = (round(m, 4), ui["anker"], z)
    if grund_key in _GRUND:
        lw.a[:] = _GRUND[grund_key]
    else:
        lw.schatten(X0, Y0 + S(8), X1, Y1 + S(8), R, S(18), (0, 0, 0, 120))
        lw.schatten(X0, Y0 + S(1), X1, Y1 + S(1), R, S(3), (0, 0, 0, 90))
        lw.box(X0, Y0, X1, Y1, R, (16, 16, 19, 238))
        lw.rand(X0, Y0, X1, Y1, R, 0.24, 0.05, dicke=max(1.0, S(0.8)))
        if m in (0.0, 1.0):                       # nur Ruhelagen merken
            _GRUND[grund_key] = lw.a.copy()
    ringx, ringy = (x0 + CH / 2, y0 + CH / 2) if m < 0.5 else (x0 + 20 + 40, y0 + 20 + 40)
    if st["status"] != "aus":
        lw.schein(S(ringx), S(ringy), S(70 + 90 * m), a(c1, 46 + 40 * puls), clip=(X0, Y0, X1, Y1, R))

    anteil = 1.0 if st["faellig"] else st["lauf_s"] / max(1, (st["pause_min"] if st["status"] == "pause"
                                                            else st["ziel_min"]) * 60)
    if st["status"] == "aus":
        anteil = 0.0

    if st["status"] == "aus":
        gross, klein = mmss(st["heute_s"]), "heute · Klick startet"
    elif st["faellig"]:
        gross = "+" + mmss(-st["rest_s"])
        klein = "Pause vorbei" if st["status"] == "pause" else "Zeit für eine Pause"
    else:
        gross = mmss(st["rest_s"])
        klein = {"arbeit": st["titel"] or "bis zur Pause", "angehalten": "angehalten",
                 "pause": "Pause"}[st["status"]]

    # --- zu ---
    ca = max(0.0, 1 - m * 2.6)
    if ca > 0:
        cx, cy = x0 + CH / 2, y0 + CH / 2
        lw.ring(S(cx), S(cy), S(13), S(3.6), anteil, c1, c2, a(WEISS, 26), ca)
        if st["faellig"] and puls:
            lw.ring(S(cx), S(cy), S(18), S(1.6), 1.0, c1, c2, (0, 0, 0, 0), ca * puls * 0.7)
        tx = x0 + CH + 2
        lw.text(S(tx), S(y0 + 26), gross, schrift(round(S(18)), 640), a(WEISS, 255 * ca))
        fk = schrift(round(S(11)), 460)
        mu = ui["musik"]
        hv = ui.get("hv", 0.0) if mu.get("verbunden") else 0.0     # Maus ueber der Pille
        frei = w - CH - 46 - (44 + 70 * hv if mu.get("verbunden") else 0)
        if ui.get("zeile"):
            lw.text(S(tx), S(y0 + 40), kuerzen(ui["zeile"], fk, S(frei)), fk, a(WEISS, 225 * ca))
        else:
            lw.text(S(tx), S(y0 + 40), kuerzen(klein, fk, S(frei)), fk, a(GRAU, 255 * ca))
        if mu.get("verbunden"):
            # beim Hover rueckt das Cover nach links, zurueck/weiter blenden daneben ein
            kx, ky, kg = x0 + w - 86 - 34 * hv, y0 + 9, 30
            an = zone("cover", kx - 4, ky - 4, kx + kg + 4, ky + kg + 4)
            spielt = mu.get("spielt")
            if cover is not None:
                lw.bild(S(kx), S(ky), S(kg), S(7), cover, ca * (1.0 if spielt and not an else 0.45))
            else:
                lw.box(S(kx), S(ky), S(kx + kg), S(ky + kg), S(7), a(TOENE["arbeit"][0], 60 * ca))
            if an or not spielt or cover is None:
                musik_symbol(lw, S, kx + kg / 2, ky + kg / 2, 5.5, spielt, a(WEISS, 235 * ca))
            if hv > 0.02:
                for name, sym_, sx in (("zurueck", ZURUECK, kx - 17), ("weiter", WEITER, kx + kg + 17)):
                    an = zone(name, sx - 13, ky, sx + 13, ky + kg) if hv > 0.5 else False
                    lw.kreis(S(sx), S(ky + kg / 2), S(13), a(WEISS, (36 if an else 0) * ca * hv))
                    lw.text(S(sx), S(ky + kg / 2), sym_, symbolschrift(round(S(10))),
                            a(WEISS if an else (200, 200, 206), 255 * ca * hv), "mm")
        bx, by = x0 + w - 24, y0 + CH / 2
        an = zone("aufklappen", bx - 18, by - 18, bx + 18, by + 18)
        lw.kreis(S(bx), S(by), S(13), a(WEISS, (40 if an else 18) * ca))
        lw.text(S(bx), S(by), ZU, symbolschrift(round(S(10))), a(WEISS if an else GRAU, 255 * ca), "mm")

    # --- auf ---
    ea0 = max(0.0, min(1.0, (m - 0.3) / 0.4))
    ea = ea0

    def reihe(unten):
        """Zeile blendet erst ein, wenn die wachsende Pille sie schon umschliesst."""
        return ea0 * max(0.0, min(1.0, (y0 + h - unten - 6) / 18))
    if ea > 0:
        P = 20
        L, Rr = x0 + P, x0 + w - P
        ein = lambda v: v * ea
        ea = reihe(y0 + P + 80)
        cx, cy = x0 + P + 40, y0 + P + 40
        lw.ring(S(cx), S(cy), S(36), S(7), anteil, c1, c2, a(WEISS, 22), ea)
        if st["faellig"] and puls:
            lw.ring(S(cx), S(cy), S(45), S(2), 1.0, c1, c2, (0, 0, 0, 0), ea * puls * 0.7)
        if st["status"] in ("arbeit", "pause"):
            for dx in (-5, 5):
                lw.box(S(cx + dx - 2.5), S(cy - 9), S(cx + dx + 2.5), S(cy + 9), S(1.5), a(WEISS, ein(220)))
        else:
            lw.text(S(cx + 2), S(cy), "▶", schrift_symbol(round(S(19))), a(WEISS, ein(220)), "mm")
        zone("ring", cx - 36, cy - 36, cx + 36, cy + 36)
        tx = x0 + P + 96
        lw.text(S(tx), S(y0 + P + 46), gross, schrift(round(S(40)), 620), a(WEISS, ein(255)))
        fk = schrift(round(S(12)), 460)
        lw.text(S(tx + 2), S(y0 + P + 68), kuerzen(klein, fk, S(Rr - tx - 40)), fk, a(GRAU, ein(255)))
        bx, by = Rr - 14, y0 + P + 14
        an = zone("zuklappen", bx - 16, by - 16, bx + 16, by + 16)
        lw.kreis(S(bx), S(by), S(14), a(WEISS, ein(42 if an else 18)))
        lw.text(S(bx), S(by), AUF, symbolschrift(round(S(10))), a(WEISS if an else GRAU, ein(255)), "mm")

        # Fokus | Pause
        ea = reihe(y0 + 144)
        sy0, sy1 = y0 + 112, y0 + 144
        lw.box(S(L), S(sy0), S(Rr), S(sy1), S(11), a(WEISS, ein(14)))
        mitte = (L + Rr) / 2
        for name, text, lx0, lx1 in (("tab_fokus", "Fokus", L, mitte), ("tab_pause", "Pause", mitte, Rr)):
            an = zone(name, lx0, sy0, lx1, sy1)
            aktiv = ui["tab"] == name[4:]
            if aktiv:
                lw.box(S(lx0 + 3), S(sy0 + 3), S(lx1 - 3), S(sy1 - 3), S(8), a((58, 58, 64), ein(255)))
                lw.rand(S(lx0 + 3), S(sy0 + 3), S(lx1 - 3), S(sy1 - 3), S(8), 0.16 * ea, 0.03 * ea)
            lw.text(S((lx0 + lx1) / 2), S((sy0 + sy1) / 2 + 0.5), text, schrift(round(S(12.5)), 600),
                    a(WEISS if aktiv or an else GRAU, ein(255)), "mm")

        # Zeit einstellen
        ea = reihe(y0 + 200)
        pause_tab = ui["tab"] == "pause"
        wert = st["pause_min"] if pause_tab else st["ziel_min"]
        zy = y0 + 182
        for name, sym_, bx in (("minus", MINUS, L + 18), ("plus", PLUS, L + 150)):
            an = zone(name, bx - 18, zy - 18, bx + 18, zy + 18)
            lw.kreis(S(bx), S(zy), S(17), a(WEISS, ein(44 if an else 20)))
            lw.text(S(bx), S(zy), sym_, symbolschrift(round(S(11))), a(WEISS, ein(230)), "mm")
        zone("zeit", L + 38, zy - 20, L + 130, zy + 20)
        zf, ef = schrift(round(S(26)), 620), schrift(round(S(12)), 500)
        zs, es = str(wert), " min"
        gesamt = zf.getlength(zs) + ef.getlength(es)
        zx = S(L + 84) - gesamt / 2
        lw.text(zx, S(zy + 9), zs, zf, a(WEISS, ein(255)))
        lw.text(zx + zf.getlength(zs), S(zy + 9), es, ef, a(GRAU, ein(255)))
        chips = (5, 10, 15) if pause_tab else (25, 50, 90)
        cw_, gap = 40, 6
        cx_ = Rr - 3 * cw_ - 2 * gap
        tc1 = TOENE["pause" if pause_tab else "arbeit"][0]
        for v in chips:
            an = zone(f"chip:{v}", cx_, zy - 15, cx_ + cw_, zy + 15)
            gewaehlt = v == wert
            fuell = a(tc1, ein(64)) if gewaehlt else a(WEISS, ein(30 if an else 14))
            lw.box(S(cx_), S(zy - 15), S(cx_ + cw_), S(zy + 15), S(9), fuell)
            lw.text(S(cx_ + cw_ / 2), S(zy + 0.5), str(v), schrift(round(S(12.5)), 600),
                    a(WEISS if gewaehlt or an else GRAU, ein(255)), "mm")
            cx_ += cw_ + gap

        # Hauptknopf + Stopp
        ea = reihe(y0 + 258)
        by0, by1 = y0 + 216, y0 + 258
        text, art = primaer(st, ui["tab"])
        an = zone("primaer", L, by0, Rr - 54, by1)
        if art == "neutral":
            fuell, schrift_farbe = a(WEISS, ein(36 if an else 22)), a(WEISS, ein(255))
        else:
            tc = TOENE[art][0]
            hell = 1.12 if an else 1.0
            fuell = a(tuple(min(255, int(c * hell)) for c in tc), ein(255))
            schrift_farbe = a((8, 8, 10) if art == "arbeit" else WEISS, ein(255))
        lw.box(S(L), S(by0), S(Rr - 54), S(by1), S(14), fuell)
        lw.rand(S(L), S(by0), S(Rr - 54), S(by1), S(14), 0.22 * ea, 0.0)
        lw.text(S((L + Rr - 54) / 2), S((by0 + by1) / 2 + 0.5), text, schrift(round(S(14)), 640),
                schrift_farbe, "mm")
        sx, sy = Rr - 21, (by0 + by1) / 2
        aktiv = st["status"] != "aus"
        an = zone("stopp", sx - 21, sy - 21, sx + 21, sy + 21) and aktiv
        lw.kreis(S(sx), S(sy), S(21), a(WEISS, ein(40 if an else 18)))
        lw.box(S(sx - 6), S(sy - 6), S(sx + 6), S(sy + 6), S(2.5),
               a((255, 105, 97) if an else (WEISS if aktiv else GRAU), ein(235 if aktiv else 90)))

        # Heute
        ea = reihe(y0 + 298)
        hy = y0 + 282
        fk = schrift(round(S(11)), 500)
        n = st["fokus_bloecke"]
        info = f"Heute {stunden(st['heute_s'])} · {n} {'Block' if n == 1 else 'Blöcke'}"
        lw.text(S(L), S(hy), info, fk, a(GRAU, ein(255)))
        jetzt = datetime.fromtimestamp(ui["jetzt"])
        lw.text(S(Rr), S(hy), jetzt.strftime("%H:%M"), fk, a(GRAU, ein(170)), "rs")
        tl0, tl1 = hy + 8, hy + 14
        lw.box(S(L), S(tl0), S(Rr), S(tl1), S(3), a(WEISS, ein(16)))
        h0 = heute_start(ui["jetzt"])
        segs = st["segmente"]
        von = min([6.0] + [(s - h0) / 3600 for s, _, _ in segs])
        bis = max([22.0, (ui["jetzt"] - h0) / 3600 + 0.5] + [(e - h0) / 3600 for _, e, _ in segs])
        sk = lambda t: L + ((t - h0) / 3600 - von) / (bis - von) * (Rr - L)
        for s, e, art_ in segs:
            xa, xb = sk(s), max(sk(e), sk(s) + 1.5)
            lw.box(S(xa), S(tl0), S(xb), S(tl1), S(2), a(TOENE["pause" if art_ == "pause" else "arbeit"][0],
                                                          ein(255)))
        xn = sk(ui["jetzt"])
        lw.box(S(xn - 0.75), S(tl0 - 3), S(xn + 0.75), S(tl1 + 3), S(0.75), a(WEISS, ein(200)))

        # Musik
        ea = reihe(y0 + EH - 24)   # letzte Zeile: voll sichtbar, sobald ganz offen
        mu = ui["musik"]
        ky0, ky1 = y0 + 312, y0 + 388
        lw.box(S(L), S(ky0), S(Rr), S(ky1), S(16), a(WEISS, ein(12)))
        lw.rand(S(L), S(ky0), S(Rr), S(ky1), S(16), 0.10 * ea, 0.0)
        if not mu.get("verbunden"):
            lw.text(S(L + 16), S(ky0 + 32), "Spotify nicht verbunden", schrift(round(S(13)), 600),
                    a(WEISS, ein(220)))
            lw.text(S(L + 16), S(ky0 + 52), "Erweiterung laden: V:\\Workflow\\chrome-ext",
                    schrift(round(S(11)), 460), a(GRAU, ein(255)))
        else:
            kx, kyy, kg = L + 12, ky0 + 12, 52
            if cover is not None:
                lw.bild(S(kx), S(kyy), S(kg), S(10), cover, ea)
            else:
                lw.box(S(kx), S(kyy), S(kx + kg), S(kyy + kg), S(10), a(TOENE["arbeit"][0], ein(60)))
            tx = kx + kg + 12
            ft, fa = schrift(round(S(13)), 620), schrift(round(S(11.5)), 460)
            breite = S(Rr - 120 - tx)
            lw.text(S(tx), S(ky0 + 30), kuerzen(mu.get("titel") or "–", ft, breite), ft, a(WEISS, ein(255)))
            if ui.get("zeile"):
                lw.text(S(tx), S(ky0 + 46), kuerzen(ui["zeile"], fa, S(Rr - 16 - tx)), fa, a(WEISS, ein(235)))
            else:
                lw.text(S(tx), S(ky0 + 46), kuerzen(mu.get("kuenstler") or "", fa, breite), fa, a(GRAU, ein(255)))
            my = ky0 + 30
            for name, sx in (("zurueck", Rr - 98), ("playpause", Rr - 62), ("weiter", Rr - 26)):
                an = zone(name, sx - 17, my - 17, sx + 17, my + 17)
                if name == "playpause":
                    lw.kreis(S(sx), S(my), S(16), a(WEISS, ein(255 if an else 232)))
                    musik_symbol(lw, S, sx, my, 5.5, mu.get("spielt"), a((12, 12, 14), ein(255)))
                else:
                    lw.kreis(S(sx), S(my), S(14), a(WEISS, ein(36 if an else 0)))
                    lw.text(S(sx), S(my), ZURUECK if name == "zurueck" else WEITER, symbolschrift(round(S(11))),
                            a(WEISS if an else (200, 200, 206), ein(255)), "mm")
            # Lautstaerke: Trefferflaeche = exakte Reglerbreite, damit Klick-x direkt der Wert ist
            vx0, vx1, vy = tx, Rr - 48, ky0 + 64
            mx, text_an = Rr - 26, ui.get("text_an")          # Songtext ein/aus
            an = zone("text", mx - 14, vy - 12, mx + 14, vy + 12)
            lw.kreis(S(mx), S(vy), S(12), a(TOENE["arbeit"][0] if text_an else WEISS,
                                            ein(70 if text_an else (36 if an else 0))))
            lw.text(S(mx), S(vy), MIKRO, symbolschrift(round(S(11))),
                    a(WEISS if text_an or an else (200, 200, 206), ein(255)), "mm")
            an = zone("lautstaerke", vx0, vy - 10, vx1, vy + 10)
            v = mu.get("lautstaerke")
            lw.box(S(vx0), S(vy - 2), S(vx1), S(vy + 2), S(2), a(WEISS, ein(30)))
            if v is not None:
                vk = vx0 + (vx1 - vx0) * max(0.0, min(1.0, v))
                lw.box(S(vx0), S(vy - 2), S(vk), S(vy + 2), S(2), a(WEISS, ein(230 if an else 200)))
                lw.kreis(S(vk), S(vy), S(6 if an else 5), a(WEISS, ein(255)))

    return lw.bgra(), zonen


def musik_symbol(lw, S, cx, cy, gr, spielt, farbe):
    """Pause (zwei Balken) wenn Musik laeuft, sonst Play-Dreieck."""
    if spielt:
        for dx in (-gr * 0.45, gr * 0.45):
            lw.box(S(cx + dx - gr * 0.24), S(cy - gr * 0.8), S(cx + dx + gr * 0.24), S(cy + gr * 0.8),
                   S(gr * 0.15), farbe)
    else:
        lw.text(S(cx + gr * 0.18), S(cy), "\u25B6", schrift_symbol(round(S(gr * 2.2))), farbe, "mm")


def primaer(st, tab):
    """Text und Farbe des Hauptknopfs; Logik dazu in App._primaer."""
    s = st["status"]
    if tab == "pause":
        return ("Pause beenden", "neutral") if s == "pause" else ("Pause starten", "pause")
    if s == "arbeit":
        return "Anhalten", "neutral"
    if s == "angehalten":
        return "Weiter", "arbeit"
    return "Fokus starten", "arbeit"


# ---------------------------------------------------------------------------
# Spotify (ueber die Chrome-Erweiterung) + HTTP/WebSocket fuer Claude und Erweiterung
# ---------------------------------------------------------------------------

class WsVerbindung:
    """Minimaler WebSocket-Server-Teil (RFC 6455): Textrahmen lesen/schreiben, Ping, Close."""

    def __init__(self, rfile, sock):
        self.r, self.s, self.lock = rfile, sock, threading.Lock()

    def _genau(self, n):
        daten = self.r.read(n)
        if len(daten) < n:
            raise EOFError
        return daten

    def lesen(self):
        """Naechste Textnachricht oder None bei Close."""
        teile = b""
        while True:
            b1, b2 = self._genau(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._genau(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._genau(8))[0]
            if n > 1 << 20:
                return None
            maske = self._genau(4) if b2 & 0x80 else b"\0\0\0\0"
            daten = bytes(x ^ maske[i % 4] for i, x in enumerate(self._genau(n)))
            if op == 8:
                return None
            if op == 9:
                self._schicken(10, daten)
                continue
            if op == 10:
                continue
            teile += daten
            if b1 & 0x80:
                return teile.decode("utf-8", "replace")

    def _schicken(self, op, daten):
        n = len(daten)
        kopf = bytes([0x80 | op]) + (bytes([n]) if n < 126 else
                                       bytes([126]) + struct.pack(">H", n) if n < 65536 else
                                       bytes([127]) + struct.pack(">Q", n))
        with self.lock:
            self.s.sendall(kopf + daten)

    def senden(self, obj):
        self._schicken(1, json.dumps(obj).encode("utf-8"))


def lrc_zeilen(lrc):
    """'[01:02.50] Text' -> [(62.5, 'Text'), ...] sortiert; Leerzeilen bleiben (Instrumental-Luecke)."""
    zeilen = []
    for roh in (lrc or "").splitlines():
        marken = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", roh)
        text = re.sub(r"\[[^\]]*\]", "", roh).strip()
        zeilen += [(int(mi) * 60 + float(se), text) for mi, se in marken]
    return sorted(zeilen)


def zeile_bei(zeilen, t):
    """Zeile, die zur Sekunde t laeuft, sonst None."""
    aktuell = None
    for z, text in zeilen:
        if z > t:
            break
        aktuell = text
    return aktuell or None


def songtext_laden(titel, kuenstler, dauer):
    """Synchroner Songtext von lrclib.net (frei, ohne Schluessel); [] wenn keiner da ist."""
    kopf = {"User-Agent": f"Fokus/{VERSION} (Echoblend)"}
    q = {"track_name": titel, "artist_name": (kuenstler or "").split(",")[0].strip()}
    suche = urllib.parse.urlencode(q)
    if dauer:
        q["duration"] = int(dauer)
    for url in ("https://lrclib.net/api/get?" + urllib.parse.urlencode(q),
                "https://lrclib.net/api/search?" + suche):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=kopf), timeout=8) as r:
                d = json.loads(r.read(2_000_000))
        except Exception:
            continue
        for treffer in (d if isinstance(d, list) else [d]):
            if isinstance(treffer, dict) and treffer.get("syncedLyrics"):
                return lrc_zeilen(treffer["syncedLyrics"])
    return []


class Spotify:
    """Stand des Spotify-Tabs + Befehle dorthin. Wird aus HTTP-Threads und dem
    Tk-Thread benutzt; nur einfache Zuweisungen, der GIL reicht."""
    BEFEHLE = ("playpause", "weiter", "zurueck", "lautstaerke", "stand")

    def __init__(self):
        self.ws, self.stand, self.cover, self._cover_url = None, {"verbunden": False}, None, None
        self._pos_t = time.time()
        self.text_an, self.text, self._text_fuer = False, [], None     # Songtext: an/aus, Zeilen, fuer welchen Song

    def verbunden(self, ws):
        self.ws = ws
        try:
            ws.senden({"cmd": "stand"})
        except OSError:
            pass

    def getrennt(self, ws):
        if self.ws is ws:
            self.ws, self.stand = None, {"verbunden": False}

    def empfangen(self, d):
        if not isinstance(d, dict):
            return
        self.stand = {k: d.get(k) for k in ("verbunden", "titel", "kuenstler", "spielt", "lautstaerke", "bild",
                                            "pos", "dauer", "gefunden")}
        self._pos_t = time.time()
        self._text_pruefen()
        url = d.get("bild")
        if url != self._cover_url:
            self._cover_url, self.cover = url, None
            threading.Thread(target=self._cover_laden, args=(url,), daemon=True).start()

    def _cover_laden(self, url):
        # Adresse kommt aus der Webseite: nur Spotifys Bild-CDN laden
        if not (isinstance(url, str) and url.startswith("https://") and
                url.split("/")[2].endswith(".scdn.co")):
            return
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                img = Image.open(io.BytesIO(r.read(2_000_000))).convert("RGB")
            img.thumbnail((160, 160))
            if self._cover_url == url:
                self.cover = img
        except Exception:
            pass

    def position(self):
        """Sekunden im Song, zwischen den Meldungen des Tabs weitergezaehlt."""
        pos = self.stand.get("pos")
        if not isinstance(pos, (int, float)):
            return None
        return pos + (time.time() - self._pos_t if self.stand.get("spielt") else 0.0)

    def text_umschalten(self, an=None):
        self.text_an = (not self.text_an) if an is None else bool(an)
        self._text_pruefen()

    def _text_pruefen(self):
        """Songtext holen, wenn er an ist und ein anderer Song laeuft."""
        song = (self.stand.get("titel"), self.stand.get("kuenstler"))
        if not self.text_an or not song[0] or song == self._text_fuer:
            return
        self._text_fuer, self.text = song, []

        def holen():
            zeilen = songtext_laden(song[0], song[1], self.stand.get("dauer"))
            if self._text_fuer == song:
                self.text = zeilen
        threading.Thread(target=holen, daemon=True).start()

    def zeile(self):
        """Aktuelle Songtextzeile oder None (aus, nicht gefunden, Instrumental)."""
        t = self.position()
        return zeile_bei(self.text, t + 0.3) if self.text_an and self.text and t is not None else None

    def befehl(self, cmd, wert=None):
        if cmd not in self.BEFEHLE or not self.ws:
            return False
        if cmd == "lautstaerke":
            wert = max(0.0, min(1.0, float(wert)))
            self.stand["lautstaerke"] = wert             # sofort zeigen, Tab zieht nach
        elif cmd == "playpause" and self.stand.get("spielt") is not None:
            self.stand["spielt"] = not self.stand["spielt"]
        try:
            self.ws.senden({"cmd": cmd, "wert": wert})
            return True
        except OSError:
            return False

    def fuer_claude(self):
        return dict({k: v for k, v in self.stand.items() if k != "bild"}, songtext=self.text_an,
                    zeile=self.zeile())


class Api(BaseHTTPRequestHandler):
    app = None
    AKTIONEN = ("start", "anhalten", "stop", "pause", "ziel")

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fremde_seite(self):
        """Webseiten duerfen nicht steuern (CSRF auf localhost). curl schickt kein Origin,
        die Erweiterung chrome-extension://."""
        o = self.headers.get("Origin")
        return bool(o) and not o.startswith("chrome-extension://")

    def do_GET(self):
        if self._fremde_seite():
            return self._json({"fehler": "verboten"}, 403)
        pfad = self.path.rstrip("/")
        if pfad == "/stand":
            return self._json(self.app.letzter_stand)
        if pfad == "/ws":
            return self._websocket()
        self._json({"fehler": "unbekannt"}, 404)

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            return self._json({"fehler": "kein WebSocket"}, 400)
        antwort = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                                .encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", antwort)
        self.end_headers()
        self.wfile.flush()
        ws = WsVerbindung(self.rfile, self.connection)
        sp = self.app.spotify
        sp.verbunden(ws)
        try:
            while (text := ws.lesen()) is not None:
                try:
                    sp.empfangen(json.loads(text))
                except json.JSONDecodeError:
                    pass
        except (EOFError, OSError):
            pass
        finally:
            sp.getrennt(ws)
            self.close_connection = True

    def do_POST(self):
        if self._fremde_seite():
            return self._json({"fehler": "verboten"}, 403)
        try:
            n = int(self.headers.get("content-length") or 0)
            daten = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"fehler": "kein JSON"}, 400)
        pfad = self.path.rstrip("/")
        if pfad == "/timer":
            if daten.get("aktion") not in self.AKTIONEN:
                return self._json({"fehler": "aktion: " + " | ".join(self.AKTIONEN)}, 400)
            antwort = queue.Queue()
            self.app.befehle.put((daten, antwort))
            try:
                return self._json(antwort.get(timeout=3))
            except queue.Empty:
                return self._json({"fehler": "App antwortet nicht"}, 503)
        if pfad == "/spotify":
            sp = self.app.spotify
            if daten.get("cmd") not in Spotify.BEFEHLE:
                return self._json({"fehler": "cmd: " + " | ".join(Spotify.BEFEHLE)}, 400)
            try:
                ok = sp.befehl(daten["cmd"], daten.get("wert"))
            except (TypeError, ValueError):
                return self._json({"fehler": "wert: 0..1"}, 400)
            if not ok:
                return self._json({"fehler": "Spotify nicht verbunden"}, 503)
            time.sleep(0.6)                                  # Tab meldet neuen Stand
            return self._json(sp.fuer_claude())
        self._json({"fehler": "unbekannt"}, 404)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def lade_config():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def speichere_config(cfg):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), "utf-8")
    os.replace(tmp, CONFIG)


class Feder:
    """Gedaempfte Feder fuer das Auf-/Zuklappen (leichtes Ueberschwingen)."""

    def __init__(self, k=260.0, daempfung=0.72):
        self.x = self.v = self.ziel = 0.0
        self.k, self.d = k, daempfung

    def schritt(self, dt):
        k = self.k
        c = 2 * math.sqrt(k) * self.d
        self.v += (k * (self.ziel - self.x) - c * self.v) * dt
        self.x = max(-0.04, min(1.06, self.x + self.v * dt))
        if abs(self.ziel - self.x) < 0.002 and abs(self.v) < 0.02:
            self.x, self.v = self.ziel, 0.0

    @property
    def ruhig(self):
        return self.x == self.ziel and self.v == 0.0


class App:
    def __init__(self):
        # Name = AppMutex im Installer: Setup erkennt die laufende App und laesst sie schliessen
        self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Echoblend.Fokus")
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", PORT), Api)
        except OSError:
            sys.exit("Fokus läuft schon (Port belegt).")
        Api.app = self
        self.befehle = queue.Queue()
        self.spotify = Spotify()
        self._laut_t = 0.0
        self.cfg = lade_config()
        self.timer = Timer()
        self.timer.ziel_min = int(self.cfg.get("ziel_min", 50))
        self.timer.pause_min = int(self.cfg.get("pause_min", 10))
        self.spotify.text_umschalten(self.cfg.get("songtext", False))
        self.letzter_stand = self.timer.stand()

        self.feder, self.anker, self.tab = Feder(), "r", "fokus"
        self.hfeder = Feder(k=500.0, daempfung=1.0)        # Hover-Einblendung der Musikknoepfe
        self.hover, self.zonen, self._schluessel = None, [], None
        self._klang, self._druck, self._zu_job, self._t = False, None, None, time.perf_counter()

        self.root = tk.Tk()
        self.root.withdraw()
        self.top = tk.Toplevel(self.root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry("1x1+0+0")
        self.ebene = Ebene(self.top)
        self.z = ctypes.windll.user32.GetDpiForWindow(self.ebene.hwnd) / 96 or 1.0
        gm = ctypes.windll.user32.GetSystemMetrics
        self.virt = gm(76), gm(77), gm(78), gm(79)
        cw, ch = round(CW * self.z), round(CH * self.z)
        vx, vy, vw, vh = self.virt
        # px/py = linke obere Ecke der zugeklappten Pille, in den sichtbaren Bereich geholt
        self.px = min(max(self.cfg.get("px", self.root.winfo_screenwidth() - cw - round(24 * self.z)), vx),
                      vx + vw - cw)
        self.py = min(max(self.cfg.get("py", round(40 * self.z)), vy), vy + vh - ch)
        self._platzieren()

        t = self.top
        t.bind("<ButtonPress-1>", self._druecken)
        t.bind("<B1-Motion>", self._ziehen)
        t.bind("<ButtonRelease-1>", self._loslassen)
        t.bind("<Button-3>", self._menue)
        t.bind("<Motion>", self._bewegen)
        t.bind("<Leave>", self._verlassen)
        t.bind("<Enter>", self._betreten)
        t.bind("<MouseWheel>", self._rad)

        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sichtbar, self.im_takt, self._ende = True, queue.Queue(), False
        self.tray = self._tray()
        self.tray.run_detached()
        self._takt()

    # --- Fensterlage ---
    def _versatz(self):
        """Wo die zugeklappte Pille im (grossen, meist durchsichtigen) Fenster liegt."""
        x = M if self.anker == "l" else M + EW - CW
        return round(x * self.z), round(M * self.z)

    def _platzieren(self):
        ox, oy = self._versatz()
        self.top.geometry(f"{round(WW * self.z)}x{round(WH * self.z)}+{self.px - ox}+{self.py - oy}")

    def aufklappen(self, auf=True):
        if auf and self.feder.ziel == 0:
            vx, _, vw, _ = self.virt
            self.anker = "r" if self.px + CW * self.z / 2 > vx + vw / 2 else "l"
            self._platzieren()
            st = self.timer.stand()
            self.tab = "pause" if st["status"] == "pause" or (st["faellig"] and st["status"] == "arbeit") \
                else "fokus"
        self.feder.ziel = 1.0 if auf else 0.0
        self._zu_abbrechen()

    # --- Befehle (Tk-Thread) ---
    def ausfuehren(self, daten):
        a_ = daten.get("aktion")
        mn = daten.get("minuten")
        if a_ == "start":
            self.timer.start(mn, daten.get("titel"), daten.get("aufgabe_id"))
        elif a_ == "pause":
            self.timer.pause(mn)
        elif a_ == "anhalten":
            self.timer.anhalten()
        elif a_ == "stop":
            self.timer.stop()
        elif a_ == "ziel" and mn:
            self._zeit_setzen(int(mn), daten.get("art", "fokus"))
        if a_ in ("start", "pause"):
            self._klang = False
            self.cfg["ziel_min"], self.cfg["pause_min"] = self.timer.ziel_min, self.timer.pause_min
            speichere_config(self.cfg)
        return self.timer.stand()

    def _zeit_setzen(self, minuten, art):
        minuten = max(1, min(240, minuten))
        if art == "pause":
            self.timer.pause_min = minuten
        else:
            self.timer.ziel_min = minuten
        self.cfg["ziel_min"], self.cfg["pause_min"] = self.timer.ziel_min, self.timer.pause_min
        speichere_config(self.cfg)

    def _primaer(self):
        s = self.timer.status
        if self.tab == "pause":
            self.ausfuehren({"aktion": "stop" if s == "pause" else "pause"})
        elif s in ("arbeit", "angehalten"):
            self.ausfuehren({"aktion": "anhalten"})
        else:
            self.ausfuehren({"aktion": "start"})

    def _klick(self, zone):
        if zone == "text":
            self.spotify.text_umschalten()
            self.cfg["songtext"] = self.spotify.text_an
            speichere_config(self.cfg)
            return
        if zone in ("cover", "playpause", "weiter", "zurueck"):
            self.spotify.befehl("playpause" if zone == "cover" else zone)
            return
        if self.feder.ziel == 0:
            if zone == "aufklappen":
                self.aufklappen()
            elif self.timer.status in ("arbeit", "angehalten"):
                self.ausfuehren({"aktion": "anhalten"})
            else:
                self.ausfuehren({"aktion": "start"})
            return
        schritt = 1 if self.tab == "pause" else 5
        wert = self.timer.pause_min if self.tab == "pause" else self.timer.ziel_min
        if zone == "zuklappen":
            self.aufklappen(False)
        elif zone in ("tab_fokus", "tab_pause"):
            self.tab = zone[4:]
        elif zone == "minus":
            self._zeit_setzen(wert - schritt, self.tab)
        elif zone == "plus":
            self._zeit_setzen(wert + schritt, self.tab)
        elif zone and zone.startswith("chip:"):
            self._zeit_setzen(int(zone[5:]), self.tab)
        elif zone in ("primaer", "ring"):
            self._primaer()
        elif zone == "stopp":
            self.ausfuehren({"aktion": "stop"})

    # --- Maus ---
    def _treffer(self, x, y):
        for name, x0, y0, x1, y1 in reversed(self.zonen):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def _bewegen(self, e):
        h = self._treffer(e.x, e.y)
        if h != self.hover:
            self.hover = h
            self.top.configure(cursor="hand2" if h else "")

    def _betreten(self, e):
        self._zu_abbrechen()
        self.hfeder.ziel = 1.0

    def _verlassen(self, e):
        self.hover = None
        self.hfeder.ziel = 0.0
        if self.feder.ziel == 1 and not self._druck:
            self._zu_job = self.root.after(6000, lambda: self.aufklappen(False))

    def _zu_abbrechen(self):
        if self._zu_job:
            self.root.after_cancel(self._zu_job)
            self._zu_job = None

    def _rad(self, e):
        if self.hover in ("cover", "lautstaerke", "playpause", "weiter", "zurueck") or self.feder.ziel == 0:
            v = self.spotify.stand.get("lautstaerke")
            if v is not None:
                self.spotify.befehl("lautstaerke", v + (0.05 if e.delta > 0 else -0.05))
        elif self.feder.ziel == 1:
            schritt = (1 if self.tab == "pause" else 5) * (1 if e.delta > 0 else -1)
            wert = self.timer.pause_min if self.tab == "pause" else self.timer.ziel_min
            self._zeit_setzen(wert + schritt, self.tab)

    def _laut_aus_x(self, x, sofort=False):
        z = next((r for r in self.zonen if r[0] == "lautstaerke"), None)
        if not z:
            return
        v = (x - z[1]) / max(1, z[3] - z[1])
        jetzt = time.perf_counter()
        if sofort or jetzt - self._laut_t > 0.06:          # beim Ziehen nicht jeden Pixel schicken
            self._laut_t = jetzt
            self.spotify.befehl("lautstaerke", v)

    def _druecken(self, e):
        if self._treffer(e.x, e.y) == "lautstaerke":
            self._druck = "regler"
            self._laut_aus_x(e.x, sofort=True)
            return
        self._druck = (e.x_root, e.y_root, self.px, self.py, False)

    def _ziehen(self, e):
        if self._druck == "regler":
            return self._laut_aus_x(e.x)
        if not self._druck:
            return
        x0, y0, px, py, gezogen = self._druck
        dx, dy = e.x_root - x0, e.y_root - y0
        if not gezogen and abs(dx) + abs(dy) > 5:
            self._druck = (x0, y0, px, py, True)
        if self._druck[4]:
            self.px, self.py = px + dx, py + dy
            self._platzieren()

    def _loslassen(self, e):
        d, self._druck = self._druck, None
        if d == "regler":
            self._laut_aus_x(e.x, sofort=True)
        elif d and d[4]:
            self.cfg["px"], self.cfg["py"] = self.px, self.py
            speichere_config(self.cfg)
        else:
            self._klick(self._treffer(e.x, e.y))
        self._malen()

    def _menue(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Aufklappen", command=self.aufklappen)
        m.add_command(label="Stopp", command=lambda: self.ausfuehren({"aktion": "stop"}))
        m.add_separator()
        m.add_command(label="Ausblenden", command=lambda: self.zeigen(False))
        m.add_command(label="Fokus beenden", command=self.beenden)
        m.tk_popup(e.x_root, e.y_root)

    def zeigen(self, an=None):
        """Pille ein-/ausblenden; der Timer laeuft weiter."""
        self.sichtbar = (not self.sichtbar) if an is None else an
        ctypes.windll.user32.ShowWindow(self.ebene.hwnd, 8 if self.sichtbar else 0)   # SW_SHOWNA / SW_HIDE
        self.tray.update_menu()

    def _tray(self):
        """Symbol im Infobereich (unten rechts). pystray ruft aus eigenem Faden;
        alles, was Tk anfasst, geht ueber im_takt in den Tk-Faden."""
        import pystray
        bild = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(bild)
        d.ellipse([4, 4, 60, 60], fill=(16, 16, 19, 255))
        d.arc([14, 14, 50, 50], -90, 200, fill=TOENE["arbeit"][0], width=7)
        spaeter = lambda f: (lambda icon, item: self.im_takt.put(f))
        menue = pystray.Menu(
            pystray.MenuItem(lambda i: "Ausblenden" if self.sichtbar else "Einblenden",
                             spaeter(lambda: self.zeigen()), default=True),
            pystray.MenuItem("Aufklappen", spaeter(lambda: (self.zeigen(True), self.aufklappen()))),
            pystray.MenuItem("Timer stoppen", spaeter(lambda: self.ausfuehren({"aktion": "stop"}))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Fokus beenden", spaeter(self.beenden)),
        )
        return pystray.Icon("Fokus", bild, f"Fokus {VERSION}", menue)

    # --- Takt ---
    def _takt(self):
        jetzt = time.perf_counter()
        dt, self._t = min(0.05, jetzt - self._t), jetzt
        while not self.befehle.empty():
            daten, antwort = self.befehle.get_nowait()
            antwort.put(self.ausfuehren(daten))
        while not self.im_takt.empty():
            self.im_takt.get_nowait()()
            if self._ende:
                return
        self.feder.schritt(dt)
        self.hfeder.schritt(dt)
        st = self.timer.stand()
        self.letzter_stand = dict(st, spotify=self.spotify.fuer_claude())
        if st["faellig"] and not self._klang:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            self._klang = True
        self._malen()
        schnell = not (self.feder.ruhig and self.hfeder.ruhig)
        self.root.after(15 if schnell else (50 if st["faellig"] else 200), self._takt)

    def _malen(self):
        st = self.timer.stand()
        puls = (0.5 + 0.5 * math.sin(time.time() * 3.2)) if st["faellig"] else 0.0
        sp = self.spotify
        ui = {"m": self.feder.x, "anker": self.anker, "hover": self.hover, "tab": self.tab,
              "puls": round(puls, 2), "jetzt": time.time() // 60 * 60,
              "musik": sp.stand, "cover_da": sp.cover is not None, "hv": round(self.hfeder.x, 3),
              "text_an": sp.text_an, "zeile": sp.zeile()}
        schluessel = json.dumps([st, ui], sort_keys=True)
        if schluessel == self._schluessel:
            return
        self._schluessel = schluessel
        if self.feder.ruhig:
            bild, self.zonen = zeichnen(st, ui, self.z, sp.cover)
        else:
            # ponytail: waehrend der Feder halbe Aufloesung + hochskalieren (~3x schneller);
            # die Bewegung verdeckt die Unschaerfe, in Ruhe wird scharf gemalt
            klein, zonen = zeichnen(st, ui, self.z / 2, sp.cover)
            groesse = (round(WW * self.z), round(WH * self.z))
            bild = np.asarray(Image.fromarray(klein, "RGBA").resize(groesse, Image.BILINEAR))
            self.zonen = [(n,) + tuple(v * 2 for v in r) for n, *r in zonen]
        self.ebene.zeigen(np.ascontiguousarray(bild))

    def beenden(self):
        self._ende = True
        self.timer.stop()
        self.tray.stop()
        threading.Thread(target=self.server.shutdown, daemon=True).start()   # blockiert sonst bei offener WS
        self.root.destroy()

    def lauf(self):
        self.root.mainloop()


if __name__ == "__main__":
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    App().lauf()
