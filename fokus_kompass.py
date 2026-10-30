"""Fokus -> Kompass: offene Arbeitsbloecke fuer den Upload buendeln.

  python fokus_kompass.py offen          JSON: {"bis": ts, "posten": [...]}
  python fokus_kompass.py erledigt <bis> Merker setzen, nachdem alle Posten im Kompass sind

Ein Posten = alle Arbeitsbloecke eines Tages mit gleichem Titel + aufgabe_id.
Der Agent (Skill fokus-kompass) traegt die Posten per MCP in den Kompass ein.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from fokus import BLOECKE, DIR, lade_bloecke

MERKER = DIR / "kompass_sync.json"
MIN_MIN = 2  # ponytail: kuerzere Posten (Testklicks) fallen weg und werden nicht nachgereicht


def lies_bis(pfad=MERKER):
    try:
        return json.loads(Path(pfad).read_text("utf-8"))["bis"]
    except (FileNotFoundError, KeyError, ValueError):
        return 0.0


def offen(bloecke, bis):
    neu = [b for b in bloecke if b["ende"] > bis and b.get("art", "arbeit") == "arbeit"]
    gruppen = {}
    for b in neu:
        tag = datetime.fromtimestamp(b["start"]).strftime("%Y-%m-%d")
        g = gruppen.setdefault((tag, b.get("titel") or "", b.get("aufgabe_id")), [])
        g.append(b)
    posten = []
    for (tag, titel, aid), bs in sorted(gruppen.items()):
        sek = sum(b["ende"] - b["start"] for b in bs)
        if sek < MIN_MIN * 60:
            continue
        uhr = lambda t: datetime.fromtimestamp(t).strftime("%H:%M")
        posten.append({"tag": tag, "titel": titel, "aufgabe_id": aid, "minuten": round(sek / 60),
                       "bloecke": len(bs), "von": uhr(min(b["start"] for b in bs)),
                       "bis_uhr": uhr(max(b["ende"] for b in bs))})
    return {"bis": max((b["ende"] for b in neu), default=bis), "posten": posten}


def main(argv):
    if argv[:1] == ["offen"]:
        print(json.dumps(offen(lade_bloecke(BLOECKE), lies_bis()), ensure_ascii=False, indent=1))
    elif argv[:1] == ["erledigt"] and len(argv) == 2:
        bis = float(argv[1])
        if bis < lies_bis():
            sys.exit("Merker wuerde zurueckgehen – abgebrochen")
        MERKER.parent.mkdir(parents=True, exist_ok=True)
        MERKER.write_text(json.dumps({"bis": bis}), "utf-8")
        print("ok")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
