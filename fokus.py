"""Fokus – schwebende Worktimer-Pille.

Pille (echtes Alpha, verschiebbar, immer oben), klappt per Knopf zu einem Panel
auf: Fokus/Pause, Zeit frei einstellen, Start/Anhalten/Stopp, Heute-Leiste.
Bloecke landen in %APPDATA%\\Fokus\\bloecke.jsonl. Lokale HTTP-Schnittstelle fuer Claude:

  GET  http://127.0.0.1:47650/stand
  POST http://127.0.0.1:47650/timer  {"aktion": "start|stoppuhr|anhalten|stop|pause|ziel", "minuten": 50, "titel": "..."}
                                    (stoppuhr = zaehlt ohne Ziel nach oben; "art": "pause" = Pausen-Stoppuhr)
  POST http://127.0.0.1:47650/spotify {"cmd": "playpause|weiter|zurueck|lautstaerke|favorit", "wert": 0.4}

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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

VERSION = "0.2.10"
PORT = 47650
DIR = Path(os.environ.get("APPDATA", Path.home())) / "Fokus"
BLOECKE = DIR / "bloecke.jsonl"
CONFIG = DIR / "config.json"

CW, CH = 276, 48        # Pille zu (logische Pixel)
EW, EH = 344, 404       # Panel auf
LH = 124                # Songtext-Karte, waechst unter dem Panel auf
M = 24                  # Rand fuer den Schatten
WW, WH = EW + 2 * M, EH + LH + 2 * M

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
        self.art = "arbeit"       # Phase des laufenden/angehaltenen Blocks: arbeit | pause
        self.offen = False        # Stoppuhr: zaehlt ohne Ziel nach oben, nie faellig
        self.seit = None          # Beginn des laufenden Blocks
        self.gesammelt = 0.0      # Sekunden der laufenden Phase aus geschlossenen Bloecken
        h0 = heute_start(self.uhr())
        self.heute = [(max(b["start"], h0), b["ende"], b.get("art", "arbeit"))
                      for b in lade_bloecke(self.ablage) if b["ende"] > h0]

    def start(self, minuten=None, titel=None, aufgabe_id=None, offen=False):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        self.offen = offen
        if minuten:
            self.ziel_min = int(minuten)
        if titel is not None:
            self.titel, self.aufgabe_id = titel, aufgabe_id
        self.gesammelt, self.seit, self.status, self.art = 0.0, self.uhr(), "arbeit", "arbeit"

    def pause(self, minuten=None, offen=False):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        if minuten:
            self.pause_min = int(minuten)
        self.offen = offen
        self.gesammelt, self.seit, self.status, self.art = 0.0, self.uhr(), "pause", "pause"

    def anhalten(self):
        """Umschalter: laufend -> angehalten -> laufend (Arbeit wie Pause)."""
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
            self.status = "angehalten"
        elif self.status == "angehalten":
            self.seit, self.status = self.uhr(), self.art

    def stop(self):
        if self.status in ("arbeit", "pause"):
            self._block_schliessen()
        self.status, self.gesammelt, self.seit, self.offen, self.art = "aus", 0.0, None, False, "arbeit"

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
        ziel = self.pause_min if self.art == "pause" else self.ziel_min
        return {
            "status": self.status,
            "art": self.art,
            "titel": self.titel,
            "aufgabe_id": self.aufgabe_id,
            "ziel_min": self.ziel_min,
            "pause_min": self.pause_min,
            "lauf_s": int(lauf),
            "rest_s": int(ziel * 60 - lauf),
            "offen": self.offen,
            "faellig": self.status != "aus" and not self.offen and lauf >= ziel * 60,
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

    def zeigen(self, bgra, voll_h=None):
        """bgra: uint8 (h, w, 4), vormultipliziert. voll_h: feste Fensterhoehe — das Bild darf
        niedriger sein (nur der benutzte Teil wird gemalt), das Fenster aendert nie seine Groesse;
        wechselnde Groessen kaempfen sonst mit Tk und lassen die Pille zittern."""
        h, w = bgra.shape[:2]
        if voll_h and voll_h > h:
            if getattr(self, "_puffer", None) is None or self._puffer.shape[:2] != (voll_h, w):
                self._puffer, self._benutzt = np.zeros((voll_h, w, 4), np.uint8), voll_h
            self._puffer[:h] = bgra
            if self._benutzt > h:
                self._puffer[h:self._benutzt] = 0
            self._benutzt, bgra, h = h, self._puffer, voll_h
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
HERZ, HERZ_VOLL = "\uEB51", "\uEB52"
STOPPUHR = "\uE916"


def _glatt(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _box_abstand(xx, yy, x0, y0, x1, y1, r):
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    r = min(r, hw, hh)
    qx, qy = np.abs(xx - cx) - (hw - r), np.abs(yy - cy) - (hh - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def _subpixel(m, x, y):
    """PIL setzt Text nur auf ganze Pixel — in Bewegung springt er sonst neben den weich
    gleitenden Flaechen. Verschiebt die Maske um den Bruchteil von x/y (linear), +1 px Rand;
    sie gehoert dann an (floor(x), floor(y))."""
    fx, fy = x - math.floor(x), y - math.floor(y)
    m = np.pad(m, ((0, 1), (0, 1)))
    if fx > 0.01:
        m[:, 1:] = m[:, 1:] * (1 - fx) + m[:, :-1] * fx
        m[:, 0] *= 1 - fx
    if fy > 0.01:
        m[1:] = m[1:] * (1 - fy) + m[:-1] * fy
        m[0] *= 1 - fy
    return m


class Leinwand:
    """Vormultiplizierte RGBA-Flaeche (float 0..1). Jede Form rechnet nur in ihrem
    Rechteck — sonst kostet eine 60-fps-Animation bei 200 % Skalierung zu viel."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.a = np.zeros((4, h, w), np.float32)     # Ebenen getrennt: numpy rechnet zusammenhaengend
        self.flips = []                              # Klappzahlen + Hintergrund darunter (flip_nachmalen)

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

    def _form(self, x0, y0, x1, y1, r, aussen, k, innen, farbe, deckung):
        """Abgerundete Form: Abstandsfeld nur am Rand (Streifen + Eckbloecke). Das Kreuz im Inneren,
        mehr als k Pixel vom Rand, hat die feste Deckung innen (None = nichts) — beim grossen
        Panel spart das den Grossteil der Rechnung."""
        W, H = self.w, self.h
        kx = lambda v: min(W, max(0, v))
        ky = lambda v: min(H, max(0, v))
        xa, xb = kx(math.ceil(x0 + k)), kx(math.ceil(x0 + r + k))
        xc, xd = kx(int(x1 - r - k)), kx(int(x1 - k))
        ya, yb = ky(math.ceil(y0 + k)), ky(math.ceil(y0 + r + k))
        yc, yd = ky(int(y1 - r - k)), ky(int(y1 - k))
        ox0, oy0, ox1, oy1 = x0 - aussen, y0 - aussen, x1 + aussen, y1 + aussen
        if not (xa <= xb <= xc <= xd and ya <= yb <= yc <= yd):
            rand = [(ox0, oy0, ox1, oy1)]
        else:
            if innen is not None:
                for ys, xs in ((slice(yb, yc), slice(xa, xd)), (slice(ya, yb), slice(xb, xc)),
                               (slice(yc, yd), slice(xb, xc))):
                    if ys.stop > ys.start and xs.stop > xs.start:
                        self._ueber((ys, xs), farbe, innen)
            rand = [(ox0, oy0, ox1, ya), (ox0, yd, ox1, oy1), (ox0, ya, xa, yd), (xd, ya, ox1, yd),
                    (xa, ya, xb, yb), (xc, ya, xd, yb), (xa, yc, xb, yd), (xc, yc, xd, yd)]
        for rect in rand:
            f = self._feld(*rect)
            if f is not None:
                sl, xx, yy = f
                self._ueber(sl, farbe, deckung(xx, yy))

    def box(self, x0, y0, x1, y1, r, farbe, maske=None):
        self._form(x0, y0, x1, y1, r, 1, 1, 1.0, farbe,
                   lambda xx, yy: np.clip(0.5 - _box_abstand(xx, yy, x0, y0, x1, y1, r), 0, 1))

    def schatten(self, x0, y0, x1, y1, r, weich, farbe):
        self._form(x0, y0, x1, y1, r, 2 * weich, weich + 1, 1.0, farbe,
                   lambda xx, yy: 1 - _glatt(-weich, weich, _box_abstand(xx, yy, x0, y0, x1, y1, r)))

    def rand(self, x0, y0, x1, y1, r, oben, unten, dicke=1.0):
        """Feine Kante, oben heller als unten (Glas-Lichtkante)."""
        def deckung(xx, yy):
            d = _box_abstand(xx, yy, x0, y0, x1, y1, r)
            cov = np.clip(dicke - np.abs(d + dicke / 2 + 0.25), 0, 1)
            return cov * (oben + (unten - oben) * np.sqrt(np.clip((yy - y0) / max(1.0, y1 - y0), 0, 1)))
        self._form(x0, y0, x1, y1, r, 1, 1.5 * dicke + 1, None, (255, 255, 255, 255), deckung)

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

    def text(self, x, y, s, font, farbe, anker="ls", blur=0.0, clip=None):
        """blur in Pixeln (weich ein-/ausblenden); clip=(x0,y0,x1,y1) schneidet ab."""
        if not s or farbe[3] <= 0:
            return
        rand = int(math.ceil(blur * 2))
        l, t, r, b = font.getbbox(s, anchor=anker)
        maske = Image.new("L", (max(1, r - l) + 2 * rand, max(1, b - t) + 2 * rand))
        ImageDraw.Draw(maske).text((rand - l, rand - t), s, font=font, fill=255, anchor=anker)
        if blur > 0.3:
            maske = maske.filter(ImageFilter.GaussianBlur(blur))
        m = _subpixel(np.asarray(maske, np.float32) / 255.0, x, y)
        x0, y0 = math.floor(x) + l - rand, math.floor(y) + t - rand
        x1, y1 = x0 + m.shape[1], y0 + m.shape[0]
        if clip:
            f = self._feld(max(x0, clip[0]), max(y0, clip[1]), min(x1, clip[2]), min(y1, clip[3]))
        else:
            f = self._feld(x0, y0, x1, y1)
        if f is None:
            return
        sl, _, _ = f
        m = m[sl[0].start - y0:sl[0].stop - y0, sl[1].start - x0:sl[1].stop - x0]
        self._ueber(sl, farbe, m)

    def flip(self, x, y, alt, neu, font, farbe, p, merken=True):
        """Klappzahl (Flip-Clock ohne Karten): geaenderte Zeichen klappen oben weg (0-50 %,
        ease-in) und unten auf (50-100 %, ease-out), wie satoriui/flip-clock."""
        if merken:
            # Hintergrund unter der Zahl merken: Folgebilder malen nur diese Flaeche neu (flip_nachmalen)
            _, t, _, b = font.getbbox("0123456789:+", anchor="ls")     # eng: nichts Spaeteres ueberdecken
            breite = max(font.getlength(alt or ""), font.getlength(neu))
            x0, y0 = max(0, int(x) - 2), max(0, int(y + t) - 2)
            x1, y1 = min(self.w, int(x + breite) + 3), min(self.h, int(y + b) + 2)
            if x1 > x0 and y1 > y0:
                self.flips.append((x0, y0, x1, y1, self.a[:, y0:y1, x0:x1].copy(), (x, y, alt, neu, font, farbe)))
        if p >= 1 or not alt or alt == neu:
            return self.text(x, y, neu, font, farbe)
        if len(alt) != len(neu):
            return self._klappe(x, y, alt, neu, font, farbe, p)
        for i, (ca_, cn) in enumerate(zip(alt, neu)):
            xi = x + font.getlength(neu[:i])
            if ca_ == cn:
                self.text(xi, y, cn, font, farbe)
            else:
                self._klappe(xi, y, ca_, cn, font, farbe, p)

    def _klappe(self, x, y, alt, neu, font, farbe, p):
        asc, desc = font.getmetrics()
        breite = int(math.ceil(max(font.getlength(alt), font.getlength(neu)))) + 2
        def maske(s):
            m = Image.new("L", (breite, asc + desc))
            ImageDraw.Draw(m).text((0, asc), s, font=font, fill=255, anchor="ls")
            return np.asarray(m, np.float32) / 255.0
        A, N = maske(alt), maske(neu)
        _, t, _, b = font.getbbox("0", anchor="ls")
        H = A.shape[0]
        g = int(round(asc + (t + b) / 2))                   # Scharnier: Mitte der Ziffer
        zeilen = np.arange(H, dtype=np.float32)
        cov, hell = np.where((zeilen < g)[:, None], N, A), np.ones(H, np.float32)
        if p < 0.5:                                          # obere Klappe (alt) faellt nach vorn
            c = math.cos((p / 0.5) ** 2 * math.pi / 2)
            if c > 0.02:
                rein = (zeilen < g) & (zeilen >= g - g * c)
                quelle = np.clip(g - (g - zeilen) / c, 0, H - 1).astype(int)
                cov[rein] = A[quelle[rein]]
                hell[rein] = 0.55 + 0.45 * c
        else:                                                # untere Klappe (neu) klappt auf
            q = 1 - (1 - (p - 0.5) / 0.5) ** 2
            c = math.sin(q * math.pi / 2)
            if c > 0.02:
                rein = (zeilen >= g) & (zeilen < g + (H - g) * c)
                quelle = np.clip(g + (zeilen - g) / c, 0, H - 1).astype(int)
                cov[rein] = N[quelle[rein]]
                hell[rein] = 0.55 + 0.45 * c
        cov = _subpixel(cov * hell[:, None], x, y)
        x0, y0 = math.floor(x), math.floor(y) - asc
        f = self._feld(x0, y0, x0 + cov.shape[1], y0 + cov.shape[0])
        if f is None:
            return
        sl, _, _ = f
        self._ueber(sl, farbe, cov[sl[0].start - y0:sl[0].stop - y0, sl[1].start - x0:sl[1].stop - x0])

    def rolle(self, x, y, alt, neu, font, farbe, p, richtung, hub):
        """Rollende Ziffern (appica number-field): geaenderte Stellen fahren um hub px,
        plus = neue von unten, minus = von oben. Stellen von rechts gezaehlt."""
        if p >= 1 or alt is None or alt == neu:
            return self.text(x, y, neu, font, farbe)
        _, t, _, b = font.getbbox("0", anchor="ls")       # Zelle = Ziffernhoehe, wie overflow-hidden
        clip = (x - 200, y + t - 3, x + font.getlength(neu) + 200, y + b + 3)
        hub = clip[3] - clip[1]
        n = max(len(alt), len(neu))
        alt, neu = alt.rjust(n), neu.rjust(n)
        breite = font.getlength(neu.strip())
        x_rechts = x + breite
        zw = font.getlength("0")                          # Segoe-Ziffern sind gleich breit
        for i in range(n):
            xi = x_rechts - (n - i) * zw
            ca_, cn = alt[i], neu[i]
            if ca_ == cn:
                self.text(xi, y, cn, font, farbe)
                continue
            for s, dy, al in ((ca_, -richtung * hub * p, 1 - p), (cn, richtung * hub * (1 - p), p)):
                if s.strip():
                    self.text(xi, y + dy, s, font, farbe[:3] + (int(farbe[3] * al),), clip=clip)

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

    def bgra(self, ys=slice(None), xs=slice(None)):
        a = self.a[:, ys, xs]
        out, tmp = np.empty(a.shape[1:] + (4,), np.uint8), np.empty(a.shape[1:], np.float32)
        for i, k in enumerate((2, 1, 0, 3)):             # kanalweise: ~7x schneller als stack
            np.multiply(a[k], 255, out=tmp)
            tmp += 0.5
            out[..., i] = tmp
        return out


def flip_nachmalen(lw, bild, fp):
    """Nur die Klappziffern neu malen, der Rest des letzten Bildes bleibt: ~2 ms statt ~30 ms."""
    for x0, y0, x1, y1, grund, args in lw.flips:
        lw.a[:, y0:y1, x0:x1] = grund
        lw.flip(*args, fp, merken=False)
        bild[y0:y1, x0:x1] = lw.bgra(slice(y0, y1), slice(x0, x1))
    return bild


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


def kurve(t, x1, y1, x2, y2):
    """CSS-cubic-bezier(x1, y1, x2, y2) an der Stelle t (0..1), per Halbierung."""
    t = max(0.0, min(1.0, t))
    lo, hi = 0.0, 1.0
    for _ in range(18):
        u = (lo + hi) / 2
        x = 3 * (1 - u) ** 2 * u * x1 + 3 * (1 - u) * u * u * x2 + u ** 3
        lo, hi = (u, hi) if x < t else (lo, u)
    u = (lo + hi) / 2
    return 3 * (1 - u) ** 2 * u * y1 + 3 * (1 - u) * u * u * y2 + u ** 3


def anzeige(st):
    """Grosse Zeit und Untertitel der Pille."""
    if st["status"] == "aus":
        return mmss(st["heute_s"]), "heute · Klick startet"
    if st.get("offen"):
        return mmss(st["lauf_s"]), {"arbeit": st["titel"] or "Stoppuhr", "pause": "Pause",
                                    "angehalten": "angehalten"}[st["status"]]
    if st["faellig"]:
        return "+" + mmss(-st["rest_s"]), ("Pause vorbei" if st["art"] == "pause" else "Zeit für eine Pause")
    return mmss(st["rest_s"]), {"arbeit": st["titel"] or "bis zur Pause", "angehalten": "angehalten",
                                "pause": "Pause"}[st["status"]]


def hoehe(ui):
    """Logische Hoehe, die die Leinwand braucht (Pille/Panel + Songtext-Karte + Schatten)."""
    m, ly = ui["m"], ui.get("ly", 0.0)
    h = CH + (EH - CH) * m
    return M + h + (8 + (LH - 16) * ly if ly > 0.02 else 0) + M


def ton(st):
    if st["faellig"]:
        return TOENE["pause" if st["art"] == "pause" else "faellig"]
    return TOENE[st["status"]]


def kuerzen(s, font, breite):
    while s and font.getlength(s) > breite:
        s = s[:-2] + "…"
    return s


_GRUND = {}   # Schatten + Glas je Ruhelage
LETZTE = None  # Leinwand des letzten vollen Bildes


def zeichnen(st, ui, z, cover=None, cover_alt=None):
    """st = Timer.stand(); ui = {m, anker, hover, tab, puls, jetzt, musik, ...}; cover = PIL-Bild.
    Gibt (BGRA, Trefferflaechen) zurueck; Flaechen in Fensterpixeln."""
    S = lambda v: v * z
    lw = Leinwand(round(S(WW)), min(round(S(WH)), round(S(hoehe(ui)))))   # nur so hoch wie noetig
    m = ui["m"]
    ly = ui.get("ly", 0.0)                     # Songtext-Karte 0..1 (Feder, darf ueberschwingen)
    w, h = CW + (EW - CW) * m, CH + (EH - CH) * m
    x0 = M if ui["anker"] == "l" else M + EW - w
    y0 = M
    r = min(h / 2, CH / 2 + (26 - CH / 2) * min(1.0, max(0.0, m)) * 1.0)
    X0, Y0, X1, Y1, R = S(x0), S(y0), S(x0 + w), S(y0 + h), S(r)
    ty0, kh = y0 + h + 8, (LH - 16) * max(0.0, ly)      # Songtext-Karte haengt unter der Pille
    kx0, kx1 = M, M + EW                                 # immer volle Panelbreite, sonst sind Zeilen zu kurz
    zonen = []
    hov = ui["hover"]

    def zone(name, *rect):
        zonen.append((name,) + tuple(S(v) for v in rect))
        return name == hov

    def gr(name):
        """Druck: Knopf taucht in 0.2 s kurz auf 0.93 ab und kommt zurueck (ohne Nachfedern)."""
        return 1 - 0.07 * math.sin(math.pi * ui.get("dp", 1.0)) if ui.get("druck") == name else 1.0

    c1, c2 = ton(st)
    puls = ui["puls"]

    # Grund: Schatten, Glas, Lichtkante (+ Songtext-Karte als eigene Insel)
    ringx, ringy = (x0 + CH / 2, y0 + CH / 2) if m < 0.5 else (x0 + 20 + 40, y0 + 20 + 40)
    schein = st["status"] != "aus"
    grund_key = (round(m, 4), round(ly, 4), ui["anker"], z, lw.h, c1 if schein and not puls else None)
    if grund_key in _GRUND:
        lw.a[:] = _GRUND[grund_key]
    else:
        flaechen = [(X0, Y0, X1, Y1, R)]
        if ly > 0.02:
            flaechen.append((S(kx0), S(ty0), S(kx1), S(ty0 + kh), S(min(22, kh / 2))))
        for f in flaechen:
            lw.schatten(f[0], f[1] + S(5), f[2], f[3] + S(5), f[4], S(10), (0, 0, 0, 120))
            lw.schatten(f[0], f[1] + S(1), f[2], f[3] + S(1), f[4], S(3), (0, 0, 0, 90))
        for f in flaechen:
            lw.box(*f, (16, 16, 19, 238))
            lw.rand(*f, 0.24, 0.05, dicke=max(1.0, S(0.8)))
        if schein and not puls:                   # Farbschein gehoert zum Grund (spart ~8 ms je Bild)
            lw.schein(S(ringx), S(ringy), S(70 + 90 * m), a(c1, 46), clip=(X0, Y0, X1, Y1, R))
        if m in (0.0, 1.0) and ly in (0.0, 1.0):  # nur Ruhelagen merken
            _GRUND[grund_key] = lw.a.copy()
    if schein and puls:
        lw.schein(S(ringx), S(ringy), S(70 + 90 * m), a(c1, 46 + 40 * puls), clip=(X0, Y0, X1, Y1, R))

    anteil = 1.0 if st["faellig"] else st["lauf_s"] / max(1, (st["pause_min"] if st["art"] == "pause"
                                                            else st["ziel_min"]) * 60)
    if st["status"] == "aus":
        anteil = 0.0
    elif st.get("offen"):
        anteil = st["lauf_s"] % 3600 / 3600           # Stoppuhr: Ring fuellt sich einmal je Stunde
    gross, klein = anzeige(st)
    fp, gross_alt = ui.get("fp", 1.0), ui.get("gross_alt")

    def cover_bild(kx, ky, kg, rad, alpha):
        """Cover-Wechsel wie bjork-now-playing-card: altes gleitet raus, neues 24 px herein, 0.92 -> 1."""
        cp, d = ui.get("cp", 1.0), ui.get("cdir", 1)
        teile = [(cover, cp, 24 * d * (1 - cp))]
        if cp < 1 and cover_alt is not None:
            teile.insert(0, (cover_alt, 1 - cp, -24 * d * cp))
        for bild_, al, dx in teile:
            sk = 0.92 + 0.08 * al
            g_ = kg * sk
            bx_, by_ = kx + (kg - g_) / 2 + dx * kg / 72, ky + (kg - g_) / 2
            if bild_ is not None:
                lw.bild(S(bx_), S(by_), S(g_), S(rad * sk), bild_, alpha * al)
            else:
                lw.box(S(bx_), S(by_), S(bx_ + g_), S(by_ + g_), S(rad * sk), a(TOENE["arbeit"][0], 60 * alpha * al))

    def herz(cx, cy, px, alpha, an):
        """Favorit-Herz: gefuellt, wenn der Song schon in den Lieblingssongs ist."""
        voll = ui["musik"].get("favorit")
        lw.text(S(cx), S(cy), HERZ_VOLL if voll else HERZ, symbolschrift(max(1, round(S(px * gr("favorit"))))),
                a((255, 92, 120) if voll else WEISS, 255 * alpha * (1.0 if an or voll else 0.85)), "mm")

    # --- zu ---
    ca = max(0.0, 1 - m * 2.6)
    if ca > 0:
        cx, cy = x0 + CH / 2, y0 + CH / 2
        lw.ring(S(cx), S(cy), S(13), S(3.6), anteil, c1, c2, a(WEISS, 26), ca)
        if st["faellig"] and puls:
            lw.ring(S(cx), S(cy), S(18), S(1.6), 1.0, c1, c2, (0, 0, 0, 0), ca * puls * 0.7)
        tx = x0 + CH + 2
        lw.flip(S(tx), S(y0 + 26), gross_alt, gross, schrift(round(S(18)), 640), a(WEISS, 255 * ca), fp)
        fk = schrift(round(S(11)), 460)
        mu = ui["musik"]
        hv = ui.get("hv", 0.0) if mu.get("verbunden") else 0.0     # Maus ueber der Pille
        # Kuerzen nach dem Hover-Ziel, nicht dem laufenden Wert: sonst springt der Text jedes Bild
        frei = w - CH - 46 - (44 + 70 * ui.get("hz", hv) if mu.get("verbunden") else 0)
        lw.text(S(tx), S(y0 + 40), kuerzen(klein, fk, S(frei)), fk, a(GRAU, 255 * ca))
        if mu.get("verbunden"):
            # beim Hover rueckt das Cover nach links, zurueck/weiter blenden daneben ein
            kx, ky, kg = x0 + w - 86 - 34 * hv, y0 + 9, 30
            # Herz-Knopf liegt auf der Cover-Ecke: zaehlt als Cover-Hover, sonst flackert das Cover beim Ueberfahren
            an = zone("cover", kx - 4, ky - 4, kx + kg + 4, ky + kg + 4) or hov == "favorit"
            spielt = mu.get("spielt")
            g_ = gr("cover")
            cover_bild(kx + kg * (1 - g_) / 2, ky + kg * (1 - g_) / 2, kg * g_, 7,
                       ca * (1.0 if spielt and not an else 0.45))
            if an or not spielt or cover is None:
                musik_symbol(lw, S, kx + kg / 2, ky + kg / 2, 5.5, spielt, a(WEISS, 235 * ca))
            if hv > 0.5 and mu.get("favorit") is not None:     # kleines Herz unten rechts am Cover
                hx, hy = kx + kg - 1, ky + kg - 1
                an = zone("favorit", hx - 9, hy - 9, hx + 9, hy + 9)
                lw.kreis(S(hx), S(hy), S(8 * gr("favorit")), a((16, 16, 19), 240 * ca * hv))
                herz(hx, hy + 0.5, 8.5, ca * hv, an)
            if hv > 0.02:
                for name, sym_, sx in (("zurueck", ZURUECK, kx - 17), ("weiter", WEITER, kx + kg + 17)):
                    an = zone(name, sx - 13, ky, sx + 13, ky + kg) if hv > 0.5 else False
                    lw.kreis(S(sx), S(ky + kg / 2), S(13 * gr(name)), a(WEISS, (36 if an else 0) * ca * hv))
                    lw.text(S(sx), S(ky + kg / 2), sym_, symbolschrift(round(S(10 * gr(name)))),
                            a(WEISS if an else (200, 200, 206), 255 * ca * hv), "mm")
        bx, by = x0 + w - 24, y0 + CH / 2
        an = zone("aufklappen", bx - 18, by - 18, bx + 18, by + 18)
        lw.kreis(S(bx), S(by), S(13 * gr("aufklappen")), a(WEISS, (40 if an else 18) * ca))
        lw.text(S(bx), S(by), ZU, symbolschrift(round(S(10))), a(WEISS if an else GRAU, 255 * ca), "mm")

    # --- Songtext-Karte (Apple-Music-Stil): eigene Insel unter der Pille, immer ausgeklappt ---
    mu = ui["musik"]
    if ly > 0.02 and mu.get("verbunden"):
        ty1 = ty0 + kh
        sa = max(0.0, min(1.0, (ly - 0.3) / 0.5))
        L_, R_ = kx0 + 18, kx1 - 18
        clip = (S(kx0), S(ty0 + 4), S(kx1), S(ty1 - 4))
        zeilen, idx = ui.get("zeilen") or [], ui.get("idx", -1)
        mitte = ty0 + 44
        gs = 14 + 2 * min(1.0, max(0.0, m))               # breitere Karte = groessere Schrift
        if ui.get("text_status") != "da" or not zeilen:
            hinweis = {"laedt": "• • •"}.get(ui.get("text_status"), "Kein Songtext gefunden")
            lw.text(S(L_), S(mitte), hinweis, schrift(round(S(gs)), 640), a(WEISS, 110 * sa), clip=clip)
        else:
            zp, sprung = ui.get("zp", 1.0), ui.get("sprung", 0)
            lz = 27 + 2 * min(1.0, max(0.0, m))           # Zeilenabstand
            for i in range(max(0, idx - 2), min(len(zeilen), idx + 4)):
                d = (i - idx) + (1 - zp) * sprung         # Lage relativ zur laufenden Zeile, gefedert
                nah = max(0.0, 1 - abs(d))
                al = max(0.0, 1 + d * 0.75) * 0.32 if d < 0 else max(0.0, 1 - d * 0.3) * 0.66
                al = al + (1 - al) * nah
                un = abs(d) if d < 0 else max(0.0, d - 1)      # naechste Zeile bleibt scharf und lesbar
                txt = zeilen[i] or "• • •"
                f_ = schrift(round(S(gs + 2 * nah)), 700)
                lw.text(S(L_), S(mitte + d * lz), kuerzen(txt, f_, S(R_ - L_)), f_,
                        a(WEISS, 255 * al * sa), blur=S(min(3.0, un * 1.4)), clip=clip)

    # --- auf ---
    ea0 = max(0.0, min(1.0, (m - 0.3) / 0.4))
    ea = ea0

    def reihe(unten):
        """Zeile blendet erst ein, wenn die wachsende Pille sie schon umschliesst."""
        return ea0 * max(0.0, min(1.0, (y0 + h - unten - 6) / 18))
    if ea > 0:
        y0 += 10 * (1 - ea0)                # Inhalt poppt beim Aufklappen 10 px nach oben ein
        P = 20
        L, Rr = x0 + P, x0 + w - P
        ein = lambda v: v * ea
        ea = reihe(y0 + P + 80)
        cx, cy = x0 + P + 40, y0 + P + 40
        g_ = gr("ring")
        lw.ring(S(cx), S(cy), S(36 * g_), S(7 * g_), anteil, c1, c2, a(WEISS, 22), ea)
        if st["faellig"] and puls:
            lw.ring(S(cx), S(cy), S(45), S(2), 1.0, c1, c2, (0, 0, 0, 0), ea * puls * 0.7)
        if st["status"] in ("arbeit", "pause"):
            for dx in (-5, 5):
                lw.box(S(cx + dx - 2.5), S(cy - 9), S(cx + dx + 2.5), S(cy + 9), S(1.5), a(WEISS, ein(220)))
        else:
            lw.text(S(cx + 2), S(cy), "▶", schrift_symbol(round(S(19))), a(WEISS, ein(220)), "mm")
        zone("ring", cx - 36, cy - 36, cx + 36, cy + 36)
        tx = x0 + P + 96
        lw.flip(S(tx), S(y0 + P + 46), gross_alt, gross, schrift(round(S(40)), 620), a(WEISS, ein(255)), fp)
        fk = schrift(round(S(12)), 460)
        lw.text(S(tx + 2), S(y0 + P + 68), kuerzen(klein, fk, S(Rr - tx - 40)), fk, a(GRAU, ein(255)))
        bx, by = Rr - 14, y0 + P + 14
        an = zone("zuklappen", bx - 16, by - 16, bx + 16, by + 16)
        lw.kreis(S(bx), S(by), S(14 * gr("zuklappen")), a(WEISS, ein(42 if an else 18)))
        lw.text(S(bx), S(by), AUF, symbolschrift(round(S(10))), a(WEISS if an else GRAU, ein(255)), "mm")

        # Fokus | Pause: Auswahl gleitet gefedert hinueber
        ea = reihe(y0 + 144)
        sy0, sy1 = y0 + 112, y0 + 144
        lw.box(S(L), S(sy0), S(Rr), S(sy1), S(11), a(WEISS, ein(14)))
        mitte = (L + Rr) / 2
        ix = L + (mitte - L) * ui.get("tp", 1.0 if ui["tab"] == "pause" else 0.0)
        lw.box(S(ix + 3), S(sy0 + 3), S(ix + mitte - L - 3), S(sy1 - 3), S(8), a((58, 58, 64), ein(255)))
        lw.rand(S(ix + 3), S(sy0 + 3), S(ix + mitte - L - 3), S(sy1 - 3), S(8), 0.16 * ea, 0.03 * ea)
        for name, text, lx0, lx1 in (("tab_fokus", "Fokus", L, mitte), ("tab_pause", "Pause", mitte, Rr)):
            an = zone(name, lx0, sy0, lx1, sy1)
            aktiv = ui["tab"] == name[4:]
            lw.text(S((lx0 + lx1) / 2), S((sy0 + sy1) / 2 + 0.5), text, schrift(round(S(12.5)), 600),
                    a(WEISS if aktiv or an else GRAU, ein(255)), "mm")

        # Zeit einstellen: rollende Ziffern, Ziehen ueber der Zahl scrubbt, Chip-Auswahl gleitet
        ea = reihe(y0 + 200)
        pause_tab = ui["tab"] == "pause"
        wert = st["pause_min"] if pause_tab else st["ziel_min"]
        zy = y0 + 182
        for name, sym_, bx in (("minus", MINUS, L + 18), ("plus", PLUS, L + 150)):
            an = zone(name, bx - 18, zy - 18, bx + 18, zy + 18)
            lw.kreis(S(bx), S(zy), S(17 * gr(name)), a(WEISS, ein(44 if an else 20)))
            lw.text(S(bx), S(zy), sym_, symbolschrift(round(S(11 * gr(name)))), a(WEISS, ein(230)), "mm")
        an = zone("zeit", L + 38, zy - 20, L + 130, zy + 20)
        if an or ui.get("scrub"):
            lw.box(S(L + 40), S(zy - 18), S(L + 128), S(zy + 18), S(9), a(WEISS, ein(16 if ui.get("scrub") else 8)))
        zf, ef = schrift(round(S(26)), 620), schrift(round(S(12)), 500)
        zs, es = str(wert), " min"
        gesamt = zf.getlength(zs) + ef.getlength(es)
        zx = S(L + 84) - gesamt / 2
        lw.rolle(zx, S(zy + 9), ui.get("wert_alt"), zs, zf, a(WEISS, ein(255)), ui.get("rp", 1.0),
                 ui.get("rdir", 1), S(22))
        lw.text(zx + zf.getlength(zs), S(zy + 9), es, ef, a(GRAU, ein(255)))
        chips = (30, 60, 90)
        cw_, gap = 40, 6
        cx0 = Rr - 3 * cw_ - 2 * gap
        tc1 = TOENE["pause" if pause_tab else "arbeit"][0]
        cal = ui.get("cal", 1.0 if wert in chips else 0.0)
        if cal > 0.01:
            hx = cx0 + ui.get("cpos", chips.index(wert) if wert in chips else 1) * (cw_ + gap)
            lw.box(S(hx), S(zy - 15), S(hx + cw_), S(zy + 15), S(9), a(tc1, ein(64 * min(1.0, cal))))
        for i, v in enumerate(chips):
            cx_ = cx0 + i * (cw_ + gap)
            an = zone(f"chip:{v}", cx_, zy - 15, cx_ + cw_, zy + 15)
            gewaehlt = v == wert
            if not gewaehlt:
                lw.box(S(cx_), S(zy - 15), S(cx_ + cw_), S(zy + 15), S(9), a(WEISS, ein(30 if an else 14)))
            g_ = gr(f"chip:{v}")
            lw.text(S(cx_ + cw_ / 2), S(zy + 0.5), str(v), schrift(round(S(12.5 * g_)), 600),
                    a(WEISS if gewaehlt or an else GRAU, ein(255)), "mm")

        # Hauptknopf + Stopp
        ea = reihe(y0 + 258)
        by0, by1 = y0 + 216, y0 + 258
        text, art = primaer(st, ui["tab"])
        pr = Rr - 100                       # rechts davon: Stoppuhr, Stopp
        an = zone("primaer", L, by0, pr, by1)
        if art == "neutral":
            fuell, schrift_farbe = a(WEISS, ein(36 if an else 22)), a(WEISS, ein(255))
        else:
            tc = TOENE[art][0]
            hell = 1.12 if an else 1.0
            fuell = a(tuple(min(255, int(c * hell)) for c in tc), ein(255))
            schrift_farbe = a((8, 8, 10) if art == "arbeit" else WEISS, ein(255))
        g_ = gr("primaer")
        bw, bh = (pr - L) * (1 - g_) / 2, (by1 - by0) * (1 - g_) / 2
        lw.box(S(L + bw), S(by0 + bh), S(pr - bw), S(by1 - bh), S(14), fuell)
        lw.rand(S(L + bw), S(by0 + bh), S(pr - bw), S(by1 - bh), S(14), 0.22 * ea, 0.0)
        lw.text(S((L + pr) / 2), S((by0 + by1) / 2 + 0.5), text, schrift(round(S(14 * g_)), 640),
                schrift_farbe, "mm")
        sx, sy = Rr - 67, (by0 + by1) / 2
        laeuft = st.get("offen") and st["status"] != "aus" and st["art"] == ("pause" if ui["tab"] == "pause" else "arbeit")
        an = zone("stoppuhr", sx - 21, sy - 21, sx + 21, sy + 21)
        g_ = gr("stoppuhr")
        lw.kreis(S(sx), S(sy), S(21 * g_), a(TOENE[st["art"]][0], ein(255)) if laeuft else a(WEISS, ein(40 if an else 18)))
        lw.text(S(sx), S(sy), STOPPUHR, symbolschrift(round(S(15 * g_))),
                a((8, 8, 10), ein(255)) if laeuft else a(WEISS if an else (200, 200, 206), ein(255)), "mm")
        sx = Rr - 21
        aktiv = st["status"] != "aus"
        an = zone("stopp", sx - 21, sy - 21, sx + 21, sy + 21) and aktiv
        g_ = gr("stopp")
        lw.kreis(S(sx), S(sy), S(21 * g_), a(WEISS, ein(40 if an else 18)))
        lw.box(S(sx - 6 * g_), S(sy - 6 * g_), S(sx + 6 * g_), S(sy + 6 * g_), S(2.5),
               a((255, 105, 97) if an else (WEISS if aktiv else GRAU), ein(235 if aktiv else 90)))

        # Heute: gestufte Leiste (staged-progress-bar) — Pillen mit 2 px Luecke, fuellt sich beim Oeffnen
        ea = reihe(y0 + 298)
        hy = y0 + 282
        fk = schrift(round(S(11)), 500)
        n = st["fokus_bloecke"]
        info = f"Heute {stunden(st['heute_s'])} · {n} {'Block' if n == 1 else 'Blöcke'}"
        lw.text(S(L), S(hy), info, fk, a(GRAU, ein(255)))
        jetzt = datetime.fromtimestamp(ui["jetzt"])
        lw.text(S(Rr), S(hy), jetzt.strftime("%H:%M"), fk, a(GRAU, ein(170)), "rs")
        # Eine Pille je Stunde (Stufen); Bloecke fuellen sie an ihrer Uhrzeit, die laufende Stunde ist umrandet
        tl0, tl1 = hy + 9, hy + 19
        h0 = heute_start(ui["jetzt"])
        segs = st["segmente"]
        jetzt_h = (ui["jetzt"] - h0) / 3600
        von = int(min([6.0, jetzt_h] + [(s - h0) / 3600 for s, _, _ in segs]))
        bis = int(math.ceil(max([22.0, jetzt_h + 0.01] + [(e - h0) / 3600 for _, e, _ in segs])))
        n_h, lueck = bis - von, 3
        pw = (Rr - L - lueck * (n_h - 1)) / n_h
        lp = ui.get("lp", 1.0)
        for i in range(n_h):
            stunde = von + i
            # gestaffelt einblenden: jede Pille 0.25 s, links zuerst (ease aus staged-progress-bar)
            pi = kurve(max(0.0, min(1.0, lp * 1.6 - i / n_h * 0.6)), 0.22, 1, 0.36, 1)
            if pi <= 0:
                continue
            px0 = L + i * (pw + lueck)
            px1 = px0 + pw
            yy0 = tl0 + (tl1 - tl0) * (1 - pi) / 2
            yy1 = tl1 - (tl1 - tl0) * (1 - pi) / 2
            jetzt_hier = stunde <= jetzt_h < stunde + 1
            grund_a = 26 if jetzt_hier else (16 if stunde < jetzt_h else 8)
            lw.box(S(px0), S(yy0), S(px1), S(yy1), S(3), a(WEISS, ein(grund_a * pi)))
            for s, e, art_ in segs:
                ta, tb = max((s - h0) / 3600, stunde), min((e - h0) / 3600, stunde + 1)
                if tb <= ta:
                    continue
                xa, xb = px0 + (ta - stunde) * pw, px0 + (tb - stunde) * pw
                xb = max(xb, xa + 2)
                lw.box(S(xa), S(yy0), S(min(px1, xb)), S(yy1), S(3),
                       a(TOENE["pause" if art_ == "pause" else "arbeit"][0], ein(255 * pi)))
            if jetzt_hier:
                lw.rand(S(px0), S(yy0), S(px1), S(yy1), S(3), 0.55 * ea * pi, 0.55 * ea * pi, dicke=S(1))

        # Musik (bjork-now-playing-card): Cover gleitet beim Songwechsel, Titel rollt, Knoepfe druecken ein
        ea = reihe(y0 + EH - 24)   # letzte Zeile: voll sichtbar, sobald ganz offen
        ky0, ky1 = y0 + 312, y0 + 388
        lw.box(S(L), S(ky0), S(Rr), S(ky1), S(16), a(WEISS, ein(12)))
        lw.rand(S(L), S(ky0), S(Rr), S(ky1), S(16), 0.10 * ea, 0.0)
        if not mu.get("verbunden"):
            lw.text(S(L + 16), S(ky0 + 32), "Spotify nicht verbunden", schrift(round(S(13)), 600),
                    a(WEISS, ein(220)))
            lw.text(S(L + 16), S(ky0 + 52), f"Erweiterung laden: {Spotify.ORDNER}",
                    schrift(round(S(11)), 460), a(GRAU, ein(255)))
        else:
            kx, kyy, kg = L + 12, ky0 + 12, 52
            cover_bild(kx, kyy, kg, 10, ea)
            if mu.get("favorit") is not None:                # Hover ueber dem Cover: abdunkeln + Herz
                an = zone("favorit", kx, kyy, kx + kg, kyy + kg)
                if an:
                    lw.box(S(kx), S(kyy), S(kx + kg), S(kyy + kg), S(10), (0, 0, 0, round(120 * ea)))
                    herz(kx + kg / 2, kyy + kg / 2 + 1, 20, ea, an)
                elif mu.get("favorit"):
                    herz(kx + kg - 9, kyy + kg - 9, 11, ea, False)
            tx = kx + kg + 12
            ft, fa = schrift(round(S(13)), 620), schrift(round(S(11.5)), 460)
            breite = S(Rr - 120 - tx)
            tp_ = kurve(ui.get("sp", 1.0), 0.23, 1, 0.32, 1)
            kclip = (S(tx - 2), S(ky0 + 14), S(Rr - 110), S(ky0 + 52))
            alt_ = ui.get("song_alt")
            for titel, kuenstler, dy, al in ((alt_ or [None, None])[0], (alt_ or [None, None])[1], -6 * tp_, 1 - tp_), \
                                            (mu.get("titel") or "–", mu.get("kuenstler") or "", 6 * (1 - tp_), tp_):
                if titel is None or al <= 0.01:
                    continue
                lw.text(S(tx), S(ky0 + 30 + dy), kuerzen(titel, ft, breite), ft, a(WEISS, ein(255 * al)), clip=kclip)
                lw.text(S(tx), S(ky0 + 46 + dy), kuerzen(kuenstler, fa, breite), fa, a(GRAU, ein(255 * al)),
                        clip=kclip)
            my = ky0 + 30
            for name, sx in (("zurueck", Rr - 98), ("playpause", Rr - 62), ("weiter", Rr - 26)):
                an = zone(name, sx - 17, my - 17, sx + 17, my + 17)
                g_ = gr(name)
                if name == "playpause":
                    lw.kreis(S(sx), S(my), S(16 * g_), a(WEISS, ein(255 if an else 232)))
                    musik_symbol(lw, S, sx, my, 5.5 * g_, mu.get("spielt"), a((12, 12, 14), ein(255)))
                else:
                    lw.kreis(S(sx), S(my), S(14 * g_), a(WEISS, ein(36 if an else 0)))
                    lw.text(S(sx), S(my), ZURUECK if name == "zurueck" else WEITER,
                            symbolschrift(round(S(11 * g_))), a(WEISS if an else (200, 200, 206), ein(255)), "mm")
            # Lautstaerke: Trefferflaeche = exakte Reglerbreite, damit Klick-x direkt der Wert ist
            vx0, vx1, vy = tx, Rr - 48, ky0 + 64
            mx, text_an, g_ = Rr - 26, ui.get("text_an"), gr("text")   # Songtext ein/aus
            an = zone("text", mx - 14, vy - 12, mx + 14, vy + 12)
            if text_an:
                lw.kreis(S(mx), S(vy), S(12 * g_), a(WEISS, ein(255 if an else 238)))
            else:
                lw.kreis(S(mx), S(vy), S(12 * g_), a(WEISS, ein(36 if an else 0)))
            lw.text(S(mx), S(vy), MIKRO, symbolschrift(max(1, round(S(11 * g_)))),
                    a((12, 12, 14) if text_an else (WEISS if an else (200, 200, 206)), ein(255)), "mm")
            an = zone("lautstaerke", vx0, vy - 10, vx1, vy + 10)
            v = mu.get("lautstaerke")
            lw.box(S(vx0), S(vy - 2), S(vx1), S(vy + 2), S(2), a(WEISS, ein(30)))
            if v is not None:
                vk = vx0 + (vx1 - vx0) * max(0.0, min(1.0, v))
                lw.box(S(vx0), S(vy - 2), S(vk), S(vy + 2), S(2), a(WEISS, ein(230 if an else 200)))
                lw.kreis(S(vk), S(vy), S(6 if an else 5), a(WEISS, ein(255)))

    global LETZTE
    LETZTE = lw            # ponytail: fuer flip_nachmalen; ein Fenster, ein Bild
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
        if s == "pause":
            return "Anhalten", "neutral"
        if s == "angehalten" and st["art"] == "pause":
            return "Weiter", "pause"
        return "Pause starten", "pause"
    if s == "arbeit":
        return "Anhalten", "neutral"
    if s == "angehalten" and st["art"] == "arbeit":
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


def zeilen_teilen(zeilen, passt):
    """Zu lange Zeilen in eigene, ausgewogene Zeilen teilen; die Zeit bis zur naechsten
    Zeile wird nach Textlaenge aufgeteilt. passt(text) -> bool sagt, ob eine Zeile in die Karte passt."""
    aus = []
    for k, (t0, text) in enumerate(zeilen):
        woerter = text.split()
        if passt(text) or len(woerter) < 2:
            aus.append((t0, text))
            continue
        t1 = zeilen[k + 1][0] if k + 1 < len(zeilen) else t0 + 4.0
        for n in range(2, len(woerter) + 1):            # so wenige Teile wie moeglich, gleich lang
            ziel, teile, akt = len(text) / n, [], []
            for w in woerter:
                if akt and len(" ".join(akt + [w])) > ziel * 1.15 and len(teile) < n - 1:
                    teile.append(" ".join(akt))
                    akt = []
                akt.append(w)
            teile.append(" ".join(akt))
            if all(passt(x) for x in teile):
                break
        vorher = 0
        for x in teile:
            aus.append((t0 + (t1 - t0) * vorher / len(text), x))
            vorher += len(x) + 1
    return aus


def index_bei(zeilen, t):
    """Index der Zeile, die zur Sekunde t laeuft; -1 vor der ersten."""
    i = -1
    for j, (z, _) in enumerate(zeilen):
        if z > t:
            break
        i = j
    return i


def zeile_bei(zeilen, t):
    """Zeile, die zur Sekunde t laeuft, sonst None (auch in Instrumental-Luecken)."""
    i = index_bei(zeilen, t)
    return (zeilen[i][1] or None) if i >= 0 else None


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
        treffer_liste = d if isinstance(d, list) else [d]
        if dauer:                                        # andere Fassung = verschobenes Timing
            treffer_liste.sort(key=lambda x: abs((x.get("duration") or 0) - dauer) if isinstance(x, dict) else 1e9)
        for treffer in treffer_liste:
            if isinstance(treffer, dict) and treffer.get("syncedLyrics"):
                f = schrift(18, 700)                     # groesste Schrift der Karte
                return zeilen_teilen(lrc_zeilen(treffer["syncedLyrics"]), lambda x: f.getlength(x) <= EW - 44)
    return []


class Spotify:
    """Stand des Spotify-Tabs + Befehle dorthin. Wird aus HTTP-Threads und dem
    Tk-Thread benutzt; nur einfache Zuweisungen, der GIL reicht."""
    BEFEHLE = ("playpause", "weiter", "zurueck", "lautstaerke", "favorit", "stand")
    ORDNER = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent / "chrome-ext"

    def __init__(self):
        self.ws, self.stand, self.cover, self._cover_url = None, {"verbunden": False}, None, None
        self._pos_t, self._pos_roh = time.time(), None
        self.text_an, self.text, self._text_fuer = False, [], None     # Songtext: an/aus, Zeilen, fuer welchen Song
        self.text_status = "keiner"                                     # laedt | da | keiner
        self._neu_geladen = False

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
        vorher, roh = self.position(), d.get("pos")
        self.stand = {k: d.get(k) for k in ("verbunden", "titel", "kuenstler", "spielt", "lautstaerke", "favorit", "bild",
                                            "pos", "dauer", "gefunden")}
        # pos kommt in ganzen Sekunden, ~0.3 s nach dem Sekundenwechsel (Observer-Verzoegerung im Tab).
        # Nur beim Wechsel neu ankern; sonst weiterzaehlen statt auf den abgerundeten Wert zurueckzufallen.
        if roh != self._pos_roh or vorher is None or not isinstance(roh, (int, float)) or abs(vorher - roh) > 1.5:
            self._pos_t = time.time() - 0.3
        else:
            self.stand["pos"], self._pos_t = vorher, time.time()
        self._pos_roh = roh
        # Nach einem Update liegt die neue Erweiterung im Ordner, Chrome laedt sie aber erst auf Zuruf neu.
        # Einmal pro Lauf: laedt Chrome sie aus einem anderen Ordner, bliebe es sonst eine Schleife.
        if d.get("version") != VERSION and not self._neu_geladen and self.ws:
            self._neu_geladen = True
            try:
                self.ws.senden({"cmd": "neuladen"})
            except OSError:
                pass
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
        self._text_fuer, self.text, self.text_status = song, [], "laedt"

        def holen():
            zeilen = songtext_laden(song[0], song[1], self.stand.get("dauer"))
            if self._text_fuer == song:
                self.text, self.text_status = zeilen, ("da" if zeilen else "keiner")
        threading.Thread(target=holen, daemon=True).start()

    def index(self):
        """Index der laufenden Songtextzeile (-1 davor oder ohne Text)."""
        t = self.position()
        return index_bei(self.text, t + 0.3) if self.text_an and self.text and t is not None else -1

    def zeile(self):
        """Aktuelle Songtextzeile oder None (aus, nicht gefunden, Instrumental)."""
        i = self.index()
        return (self.text[i][1] or None) if i >= 0 else None

    def befehl(self, cmd, wert=None):
        if cmd not in self.BEFEHLE or not self.ws:
            return False
        if cmd == "lautstaerke":
            wert = max(0.0, min(1.0, float(wert)))
            self.stand["lautstaerke"] = wert             # sofort zeigen, Tab zieht nach
        elif cmd == "favorit":
            if self.stand.get("favorit") is None:        # Tab kennt kein Herz
                return False
            self.stand["favorit"] = not self.stand["favorit"]
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
    AKTIONEN = ("start", "stoppuhr", "anhalten", "stop", "pause", "ziel")

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
        # feste Teilschritte: bei steifen Federn und langen Bildern (50 ms) schwingt Euler sonst auf
        n = max(1, math.ceil(dt / 0.004))
        for _ in range(n):
            self.v += (k * (self.ziel - self.x) - c * self.v) * dt / n
            self.x += self.v * dt / n
        if abs(self.ziel - self.x) < 0.002 and abs(self.v) < 0.02:
            self.x, self.v = self.ziel, 0.0

    @property
    def ruhig(self):
        return self.x == self.ziel and self.v == 0.0


class App:
    def __init__(self):
        try:
            # ohne SO_REUSEADDR: unter Windows bindet sonst ein zweites Fokus denselben Port, zwei Pillen liegen uebereinander
            ThreadingHTTPServer.allow_reuse_address = False
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

        # Aufklappen mit mehr Schwung (bounce ~0.4 wie die Dynamic Island von aghasisahakyan1)
        self.feder, self.anker, self.tab = Feder(k=600.0, daempfung=0.8), "r", "fokus"
        self.hfeder = Feder(k=500.0, daempfung=1.0)        # Hover-Einblendung der Musikknoepfe
        # Songtext: Karte (federt auf), Zeilenwechsel (Pop)
        # Pop = schnell und fast ohne Nachschwingen (Daempfung ~0.85-0.9), nicht wabbelig
        self.lfeder = Feder(k=600.0, daempfung=0.8)
        self.tfeder = Feder(k=700.0, daempfung=0.9)         # Fokus|Pause-Auswahl gleitet
        self.cfeder, self.cafeder = Feder(k=700.0, daempfung=0.9), Feder(k=700.0, daempfung=1.0)  # Chip: Lage 0..1, Sicht
        self.federn = (self.feder, self.hfeder, self.lfeder, self.tfeder, self.cfeder, self.cafeder)
        self._idx, self._sprung, self._druck_zone, self.anim = -1, 0, None, {}
        # zuletzt gezeigte Werte; aendern sie sich, startet die passende Animation
        self._gross = self._wert = self._song = self._cover = None
        self._gross_alt = self._wert_alt = self._song_alt = self._cover_alt = None
        self._rdir, self._cdir = 1, 1
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
            self.tab = "pause" if st["art"] == "pause" and st["status"] != "aus" or (st["faellig"] and st["status"] == "arbeit") \
                else "fokus"
            self._start("leiste", 0.5)                        # Tagesleiste fuellt sich auf
        self.feder.ziel = 1.0 if auf else 0.0
        self._zu_abbrechen()

    # --- Befehle (Tk-Thread) ---
    def ausfuehren(self, daten):
        a_ = daten.get("aktion")
        mn = daten.get("minuten")
        if a_ == "start":
            self.timer.start(mn, daten.get("titel"), daten.get("aufgabe_id"))
        elif a_ == "stoppuhr" and daten.get("art") == "pause":
            self.timer.pause(offen=True)
        elif a_ == "stoppuhr":
            self.timer.start(None, daten.get("titel"), daten.get("aufgabe_id"), offen=True)
        elif a_ == "pause":
            self.timer.pause(mn)
        elif a_ == "anhalten":
            self.timer.anhalten()
        elif a_ == "stop":
            self.timer.stop()
        elif a_ == "ziel" and mn:
            self._zeit_setzen(int(mn), daten.get("art", "fokus"))
        if a_ in ("start", "pause", "stoppuhr"):
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
            pausiert = s == "pause" or (s == "angehalten" and self.timer.art == "pause")
            self.ausfuehren({"aktion": "anhalten" if pausiert else "pause"})
        elif s in ("arbeit", "angehalten"):
            self.ausfuehren({"aktion": "anhalten"})
        else:
            self.ausfuehren({"aktion": "start"})

    def _start(self, name, dauer):
        self.anim[name] = (time.perf_counter(), dauer)

    def _p(self, name):
        """Fortschritt 0..1 einer Zeit-Animation (1 = fertig oder nie gestartet)."""
        t0, dauer = self.anim.get(name, (0.0, 1.0))
        return min(1.0, (time.perf_counter() - t0) / dauer)

    def _klick(self, zone):
        if zone:
            self._druck_zone = zone                      # Knopf drueckt kurz ein (wie CSS active:scale)
            self._start("druck", 0.2)
        if zone in ("weiter", "zurueck"):
            self._cdir = -1 if zone == "zurueck" else 1
        if zone == "text":
            self.spotify.text_umschalten()
            self.cfg["songtext"] = self.spotify.text_an
            speichere_config(self.cfg)
            return
        if zone in ("cover", "playpause", "weiter", "zurueck", "favorit"):
            self.spotify.befehl("playpause" if zone == "cover" else zone)
            return
        if self.feder.ziel == 0:
            if zone == "aufklappen":
                self.aufklappen()
            elif self.timer.status in ("arbeit", "pause", "angehalten"):
                self.ausfuehren({"aktion": "anhalten"})
            else:
                self.ausfuehren({"aktion": "start"})
            return
        schritt = 5
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
        elif zone == "stoppuhr" and not (self.timer.offen and self.timer.status != "aus"
                                          and self.timer.art == ("pause" if self.tab == "pause" else "arbeit")):
            self.ausfuehren({"aktion": "stoppuhr", "art": self.tab})

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
            self._wecken()

    def _betreten(self, e):
        self._zu_abbrechen()
        self.hfeder.ziel = 1.0
        self._wecken()

    def _verlassen(self, e):
        self.hover = None
        self.hfeder.ziel = 0.0
        self._wecken()
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
            schritt = 5 * (1 if e.delta > 0 else -1)
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
        zone = self._treffer(e.x, e.y)
        if zone == "lautstaerke":
            self._druck = "regler"
            self._laut_aus_x(e.x, sofort=True)
            return
        if zone == "zeit":                                  # Zahl ziehen = scrubben (duration-picker)
            wert = self.timer.pause_min if self.tab == "pause" else self.timer.ziel_min
            self._druck = ("zeit", e.x, wert)
            return
        self._druck = (e.x_root, e.y_root, self.px, self.py, False)

    def _ziehen(self, e):
        if self._druck == "regler":
            return self._laut_aus_x(e.x)
        if self._druck and self._druck[0] == "zeit":
            _, x0, wert = self._druck
            schritt = 5
            return self._zeit_setzen(wert + round((e.x - x0) / (6 * self.z)) * schritt, self.tab)
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
        elif d and d[0] == "zeit":
            pass
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
        sp = self.spotify
        self.lfeder.ziel = 1.0 if sp.text_an and sp.stand.get("verbunden") else 0.0
        idx = sp.index()
        if idx != self._idx:                               # neue Zeile: Karte rollt gefedert weiter
            self._sprung = max(-2, min(2, idx - self._idx)) if self._idx >= 0 else 1
            self._idx = idx
            self._start("zeile", 0.45)
        st = self.timer.stand()
        self._aenderungen(st)
        for f in self.federn:
            f.schritt(dt)
        self.letzter_stand = dict(st, spotify=self.spotify.fuer_claude())
        if st["faellig"] and not self._klang:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            self._klang = True
        self._malen()
        schnell = not all(f.ruhig for f in self.federn) or any(self._p(n) < 1 for n in self.anim)
        # Bildabstand ab Beginn des Takts, nicht ab Ende des Malens — sonst addiert sich die Malzeit
        rest = 16 - round((time.perf_counter() - jetzt) * 1000)
        self._takt_job = self.root.after(max(1, rest) if schnell else (50 if st["faellig"] else 200), self._takt)

    def _wecken(self):
        """Naechstes Bild sofort statt nach bis zu 200 ms (Hover, Klick)."""
        if getattr(self, "_takt_job", None):
            self.root.after_cancel(self._takt_job)
        self._takt_job = self.root.after(1, self._takt)

    def _aenderungen(self, st):
        """Vergleicht Gezeigtes mit dem Stand und startet Flip, Rollen, Gleiten, Songwechsel."""
        gross = anzeige(st)[0]
        if gross != self._gross:
            if self._gross is not None:
                self._gross_alt = self._gross
                self._start("flip", 0.6)
            self._gross = gross
        pause_tab = self.tab == "pause"
        self.tfeder.ziel = 1.0 if pause_tab else 0.0
        wert = self.timer.pause_min if pause_tab else self.timer.ziel_min
        if (self.tab, wert) != self._wert:
            gleich = bool(self._wert) and self._wert[0] == self.tab
            if gleich:
                self._wert_alt, self._rdir = str(self._wert[1]), (1 if wert > self._wert[1] else -1)
                self._start("rolle", 0.22)
            else:
                self._wert_alt = None
            chips = (30, 60, 90)
            if wert in chips:
                self.cfeder.ziel, self.cafeder.ziel = chips.index(wert) / 2, 1.0
                if not gleich or self.cafeder.x < 0.05:   # neu auftauchen: springen statt gleiten
                    self.cfeder.x, self.cfeder.v = self.cfeder.ziel, 0.0
            else:
                self.cafeder.ziel = 0.0
            self._wert = (self.tab, wert)
        sp = self.spotify
        song = (sp.stand.get("titel"), sp.stand.get("kuenstler"))
        if song != self._song:
            if self._song and self._song[0] and song[0]:
                self._song_alt = list(self._song)
                self._start("song", 0.22)
            self._song = song
        if sp.cover is not None and sp.cover is not self._cover:
            if self._cover is not None:
                self._cover_alt = self._cover
                self._start("cover", 0.32)
            self._cover = sp.cover

    def _malen(self):
        st = self.timer.stand()
        puls = (0.5 + 0.5 * math.sin(time.time() * 3.2)) if st["faellig"] else 0.0
        sp = self.spotify
        ui = {"m": self.feder.x, "anker": self.anker, "hover": self.hover, "tab": self.tab,
              "puls": round(puls, 2), "jetzt": time.time() // 60 * 60,
              "musik": sp.stand, "cover_da": sp.cover is not None, "hv": round(self.hfeder.x, 3),
              "hz": self.hfeder.ziel,
              "text_an": sp.text_an, "ly": round(self.lfeder.x, 3),
              "zp": round(kurve(self._p("zeile"), 0.22, 1, 0.36, 1), 3),
              "idx": self._idx, "sprung": self._sprung, "text_status": sp.text_status,
              "zeilen": [t for _, t in sp.text] if self.lfeder.x > 0.02 else None,
              "fp": round(self._p("flip"), 3), "gross_alt": self._gross_alt,
              "rp": round(kurve(self._p("rolle"), 0.4, 0, 0.2, 1), 3), "rdir": self._rdir, "wert_alt": self._wert_alt,
              "tp": round(self.tfeder.x, 3), "cpos": round(self.cfeder.x * 2, 3), "cal": round(self.cafeder.x, 3),
              "druck": self._druck_zone, "dp": round(self._p("druck"), 3), "lp": round(self._p("leiste"), 3),
              "sp": round(self._p("song"), 3), "song_alt": self._song_alt,
              "cp": round(kurve(self._p("cover"), 0.23, 1, 0.32, 1), 3), "cdir": self._cdir,
              "cover_id": id(self._cover), "scrub": bool(self._druck and self._druck[0] == "zeit")}
        # Vergleichsschluessel: laufender Block in 30-s-Schritten — er waechst alle 0.1 s, die Leiste
        # zeigt das nicht, sonst wird jedes Bild voll neu gemalt
        st_k = dict(st, segmente=[[s_, round(e_ / 30), a_] for s_, e_, a_ in st["segmente"]])
        schluessel = json.dumps([st_k, ui], sort_keys=True)
        if schluessel == self._schluessel:
            return
        self._schluessel = schluessel
        basis = json.dumps([st_k, dict(ui, fp=None)], sort_keys=True)
        if basis == getattr(self, "_basis", None) and getattr(self, "_bild", None) is not None and LETZTE is not None:
            bild = flip_nachmalen(LETZTE, self._bild, ui["fp"])        # nur die Klappziffern
        else:   # immer volle Aufloesung: halbe war beim Auf-/Zuklappen sichtbar unscharf
            bild, self.zonen = zeichnen(st, ui, self.z, self._cover, self._cover_alt)
        voll = self.feder.ruhig and self.lfeder.ruhig
        self._basis, self._bild = (basis, bild) if voll else (None, None)
        self.ebene.zeigen(np.ascontiguousarray(bild), round(WH * self.z))

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
