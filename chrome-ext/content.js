// Läuft im Spotify-Tab: liest den Player, führt Befehle der Fokus-App aus.
// Kann zweimal landen (Manifest + Nachreichen aus background.js) — nur einmal einrichten.
if (!globalThis.__fokusBruecke) {
globalThis.__fokusBruecke = true;
const $ = (s) => document.querySelector(s);
const KNOEPFE = {
  playpause: "control-button-playpause",
  weiter: "control-button-skip-forward",
  zurueck: "control-button-skip-back",
};

function regler() {
  return $('[data-testid="volume-bar"] input[type="range"]');
}

// Die sichtbare Leiste: traegt den echten Wert als CSS-Variable (--progress-bar-transform: 62%)
function leiste() {
  const vb = $('[data-testid="volume-bar"]');
  if (!vb) return null;
  return [vb, ...vb.querySelectorAll("*")].find((e) => (e.getAttribute("style") || "").includes("--progress-bar-transform"))
    || vb.querySelector('[data-testid="progress-bar"]');
}

function lautAnzeige() {
  const l = leiste();
  const m = l && /--progress-bar-transform:\s*([\d.]+)%/.exec(l.getAttribute("style") || "");
  if (m) return Number(m[1]) / 100;
  const r = regler();
  return r ? Number(r.value) / Number(r.max || 1) : null;
}

let methode = null;

function stand() {
  const ms = navigator.mediaSession;
  const md = ms && ms.metadata;
  const pp = $(`[data-testid="${KNOEPFE.playpause}"]`);
  const r = regler();
  let spielt = null;
  if (ms && ms.playbackState && ms.playbackState !== "none") spielt = ms.playbackState === "playing";
  else if (pp) spielt = /paus/i.test(pp.getAttribute("aria-label") || "");
  const titelEl = $('[data-testid="context-item-info-title"]');
  const kuenstler = [...document.querySelectorAll('[data-testid="context-item-info-artist"]')]
    .map((e) => e.textContent).join(", ");
  const art = md && md.artwork && md.artwork.length ? md.artwork[md.artwork.length - 1].src : null;
  const img = $('[data-testid="now-playing-widget"] img');
  return {
    verbunden: true,
    titel: (md && md.title) || (titelEl && titelEl.textContent) || "",
    kuenstler: (md && md.artist) || kuenstler,
    bild: art || (img && img.src) || null,
    spielt,
    lautstaerke: lautAnzeige(),
    gefunden: {
      playpause: !!pp,
      weiter: !!$(`[data-testid="${KNOEPFE.weiter}"]`),
      zurueck: !!$(`[data-testid="${KNOEPFE.zurueck}"]`),
      regler: !!r,
      leiste: !!leiste(),
      methode,
      mediasession: !!md,
    },
  };
}

function klickAuf(el, x, y) {
  const o = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, buttons: 1,
              pointerId: 1, pointerType: "mouse", isPrimary: true };
  el.dispatchEvent(new PointerEvent("pointerdown", o));
  el.dispatchEvent(new MouseEvent("mousedown", o));
  el.dispatchEvent(new PointerEvent("pointermove", o));
  el.dispatchEvent(new MouseEvent("mousemove", o));
  o.buttons = 0;
  el.dispatchEvent(new PointerEvent("pointerup", o));
  el.dispatchEvent(new MouseEvent("mouseup", o));
  el.dispatchEvent(new MouseEvent("click", o));
}

function setzeLautstaerke(w) {
  w = Math.max(0, Math.min(1, Number(w)));
  // 1) wie ein Mensch: in die Leiste klicken
  const l = leiste();
  if (l) {
    const r = l.getBoundingClientRect();
    if (r.width > 0) klickAuf(l, r.left + r.width * w, r.top + r.height / 2);
  }
  // 2) Pruefen; greift der Klick nicht, den versteckten Regler setzen
  setTimeout(() => {
    const ist = lautAnzeige();
    if (ist !== null && Math.abs(ist - w) < 0.04) { methode = "klick"; return; }
    const rg = regler();
    if (!rg) { methode = "keine"; return; }
    const max = Number(rg.max || 1);
    // Setter des Prototyps umgeht Reacts Wert-Merker, sonst ignoriert React das Event
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(rg, String(w * max));
    rg.dispatchEvent(new Event("input", { bubbles: true }));
    rg.dispatchEvent(new Event("change", { bubbles: true }));
    methode = "regler";
  }, 120);
}

chrome.runtime.onMessage.addListener((b, _absender, antwort) => {
  if (KNOEPFE[b.cmd]) {
    const k = $(`[data-testid="${KNOEPFE[b.cmd]}"]`);
    if (k) k.click();
  } else if (b.cmd === "lautstaerke") {
    setzeLautstaerke(b.wert);
  }
  // Spotify braucht einen Moment, bis der Player nachzieht
  setTimeout(() => antwort(stand()), b.cmd === "stand" ? 0 : b.cmd === "lautstaerke" ? 450 : 350);
  return true;
});

// Änderungen melden (Titel, Play/Pause, Lautstärke) — nur wenn sich etwas geändert hat
let letzter = "";
let geplant = false;
function melden() {
  geplant = false;
  const s = stand();
  const j = JSON.stringify(s);
  if (j === letzter) return;
  letzter = j;
  chrome.runtime.sendMessage({ typ: "stand", stand: s }).catch(() => {});
}
new MutationObserver(() => {
  if (!geplant) { geplant = true; setTimeout(melden, 300); }
}).observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
melden();
}
