// Brücke Fokus-App <-> Spotify-Tab.
// WebSocket zur App (Befehle kommen sofort an, auch wenn der Tab im Hintergrund ist),
// Befehle gehen per tabs.sendMessage an content.js. WS-Verkehr alle 20 s hält den
// Service Worker wach (Chrome 116+); der Alarm verbindet neu, falls er doch schlief.
const ADRESSE = "ws://127.0.0.1:47650/ws";
let ws = null;
let tabId = null;

function verbinden() {
  if (ws && ws.readyState <= 1) return;
  try {
    ws = new WebSocket(ADRESSE);
  } catch {
    ws = null;
    return;
  }
  ws.onmessage = async (e) => {
    let befehl;
    try { befehl = JSON.parse(e.data); } catch { return; }
    senden(await anTab(befehl));
  };
  ws.onclose = () => { ws = null; };
  ws.onerror = () => {};
}

function senden(stand) {
  if (stand && ws && ws.readyState === 1) ws.send(JSON.stringify(stand));
}

async function spotifyTab() {
  if (tabId !== null) {
    try { await chrome.tabs.get(tabId); return tabId; } catch { tabId = null; }
  }
  const tabs = await chrome.tabs.query({ url: "https://open.spotify.com/*" });
  const t = tabs.find((x) => x.audible) || tabs[0];
  tabId = t ? t.id : null;
  return tabId;
}

async function anTab(befehl) {
  const id = await spotifyTab();
  if (id === null) return { verbunden: false };
  try {
    return await chrome.tabs.sendMessage(id, befehl);
  } catch {
    // Tab war schon offen, bevor die Erweiterung geladen wurde: Skript nachreichen
    try {
      await chrome.scripting.executeScript({ target: { tabId: id }, files: ["content.js"] });
      return await chrome.tabs.sendMessage(id, befehl);
    } catch {
      tabId = null;
      return { verbunden: false };
    }
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.typ === "stand") {
    if (sender.tab) tabId = sender.tab.id;
    verbinden();
    senden(msg.stand);
  }
});

setInterval(async () => { verbinden(); senden(await anTab({ cmd: "stand" })); }, 20000);
chrome.alarms.create("wach", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(verbinden);
chrome.runtime.onStartup.addListener(verbinden);
chrome.runtime.onInstalled.addListener(verbinden);
verbinden();
