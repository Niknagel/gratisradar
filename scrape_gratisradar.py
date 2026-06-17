#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GratisRadar Scraper
===================
Liest die aktiven Geld-zurück-Aktionen von geldzurueck.deals, öffnet jede
Detailseite und zieht die FAKTEN + den echten "Zur Aktion"-Hersteller-Link.
Schreibt offers.json im Format, das gratisradar.html erwartet.

Wichtige Designentscheidungen (siehe Projekt-Diskussion):
- Es werden nur FAKTEN extrahiert (Zeitraum, Betrag, Limits, Händler, Hersteller-Link).
  Die Anzeige-Beschreibung wird aus diesen Fakten NEU zusammengebaut (Template),
  nicht der Originaltext kopiert.
- Keine Produktbilder: die App nutzt Emojis (Kategorie-Mapping unten).
- Rate-Limit + User-Agent + robots.txt-Hinweis beachten.

Setup:
    pip install requests beautifulsoup4
Run:
    python scrape_gratisradar.py
Ausgabe:
    offers.json  (neben gratisradar.html legen oder zusammen hosten)
"""

import json
import re
import time
import sys
import urllib.robotparser
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://geldzurueck.deals"
LIST_URL = BASE + "/?view=active&sort=newest&page={page}#aktionen-top"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}
DELAY = 1.5          # Sekunden Pause zwischen Requests (fair bleiben)
MAX_PAGES = 12       # Sicherheitslimit
TODAY = date.today()

# ---------------------------------------------------------------------------
# Kategorie/Emoji-Mapping (Stichwort im Titel -> emoji + Kategorie-Farbe-Key)
# Reihenfolge = Priorität.
# ---------------------------------------------------------------------------
CATS = [
    (("bier", "beer", "sixpack", "damm", "jever", "schöfferhofer", "desperados", "bud"), "🍺", "beer"),
    (("wasser", "mineralwasser", "volvic", "liebenwerda"), "💧", "water"),
    (("cola", "limo", "mocktail", "rockstar", "granini", "vita"), "🥤", "soft"),
    (("kaffee", "nescafé", "nescafe", "frappé", "melitta", "pads"), "☕", "coffee"),
    (("eis", "schoko", "milka", "oreo", "ferrero", "monte", "praline"), "🍦", "sweet"),
    (("käse", "kaese", "bergader"), "🧀", "cheese"),
    (("tampon", "o.b.", "always", "discreet", "nivea", "deo", "toilettenpapier",
      "cottonelle", "somat", "cillit", "melatonin", "tetesept", "spray", "reiniger"), "🧴", "care"),
    (("purina", "whiskas", "gourmet", "katze", "hund", "tiernahrung", "tier"), "🐾", "pet"),
    (("milch", "joghurt", "zott", "pure joy"), "🥛", "dairy"),
]
DEFAULT_EMOJI, DEFAULT_CAT = "🛒", "food"

RET_KNOWN = {"dm", "rossmann", "mueller", "edeka", "rewe", "lidl",
             "aldi", "kaufland", "penny", "netto"}


def categorize(title):
    t = title.lower()
    for keys, emoji, cat in CATS:
        if any(k in t for k in keys):
            return emoji, cat
    return DEFAULT_EMOJI, DEFAULT_CAT


def check_robots():
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(BASE + "/robots.txt")
    try:
        rp.read()
    except Exception:
        print("robots.txt nicht lesbar – fahre vorsichtig fort.")
        return
    if not rp.can_fetch(HEADERS["User-Agent"], BASE + "/"):
        print("robots.txt verbietet das Crawlen. Abbruch.")
        sys.exit(1)


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    time.sleep(DELAY)
    return r.text


def collect_slugs():
    """Sammelt die Slugs aller aktiven Aktionen über die Übersichtsseiten."""
    slugs, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        html = get(LIST_URL.format(page=page))
        # tolerant: <slug>_xs.<ext> ODER ohne _xs, beliebige Bildendung
        found = re.findall(r"/images/\d+/([A-Za-z0-9._-]+?)(?:_xs)?\.(?:png|jpe?g|webp)", html)
        # Helfer-/Layoutbilder rausfiltern (haben keine reine Slug-Form)
        found = [s for s in found if "-" in s and "geld-zur" not in s.lower()]
        new = [s for s in found if s not in seen]
        if page == 1 and not new:
            # Diagnose: echte Bild-Pfade und Detail-Link-Kandidaten ausgeben
            print(f"  DIAGNOSE Seite 1: HTML-Laenge={len(html)} | "
                  f"'/images/' vorhanden={'/images/' in html} | "
                  f"'aktionen-top'={'aktionen-top' in html}")
            imgs = list(dict.fromkeys(re.findall(r"/images/[^\"'\s)>]+", html)))
            print(f"  --- {len(imgs)} unterschiedliche /images/-Pfade, erste 25:")
            for u in imgs[:25]:
                print("     IMG:", u)
            hrefs = list(dict.fromkeys(re.findall(r'href="(/[a-z0-9][a-z0-9-]{6,})"', html)))
            print(f"  --- {len(hrefs)} Link-Kandidaten (/slug), erste 20:")
            for u in hrefs[:20]:
                print("     HREF:", u)
            with open("debug_page1.html", "w", encoding="utf-8") as d:
                d.write(html)
            print("  -> debug_page1.html geschrieben.")
        if not new:
            break
        for s in new:
            seen.add(s)
            slugs.append(s)
        print(f"  Seite {page}: +{len(new)} Aktionen (gesamt {len(slugs)})")
    return slugs


def text_after(soup_text, label):
    """Holt den Wert nach einem Label wie 'Teilnahmezeitraum ...'."""
    m = re.search(re.escape(label) + r"\s*[:\n]?\s*(.+)", soup_text)
    return m.group(1).strip() if m else None


def parse_detail(slug):
    html = get(f"{BASE}/{slug}")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    title = soup.find("h1")
    title = title.get_text(strip=True) if title else slug.replace("-", " ").title()

    # Typ (Badge)
    low = text.lower()
    if "gewinnspiel" in title.lower():
        typ = "gewinnspiel"
    elif "gratis testen" in low:
        typ = "gratis"
    else:
        typ = "cashback"

    # Score
    m = re.search(r"Score:\s*(\d+)\s*von\s*100", html)
    score = int(m.group(1)) if m else 80

    # Zeitraum -> Enddatum -> Resttage
    period = None
    days_left = None
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*[-–]\s*(\d{2}\.\d{2}\.\d{4})", text)
    if m:
        period = f"{m.group(1)} – {m.group(2)}"
        try:
            end = datetime.strptime(m.group(2), "%d.%m.%Y").date()
            days_left = max((end - TODAY).days, 0)
        except ValueError:
            pass

    # Felder
    proof = text_after(text, "Nachweis") or "Nur Kassenbon"
    qty = text_after(text, "Kaufmenge") or text_after(text, "Teilnahmelimit") or "1 Produkt"
    maxref = text_after(text, "Max. Erstattung")
    gesamt = text_after(text, "Einlöselimit gesamt") or "–"
    dauer = text_after(text, "Auszahlungsdauer") or "ca. 6 Wochen"
    proof = proof.split("\n")[0][:60]
    qty = qty.split("\n")[0][:60]

    # Produktfoto nötig?
    photo = 1 if ("produktfoto" in low or "foto des produkts" in low
                  or "produkt und kassenbon" in low) else 0

    # Teilnehmende Produkte
    prods = []
    h = soup.find(lambda t: t.name in ("h2", "h3") and "Teilnehmende Produkte" in t.get_text())
    if h:
        ul = h.find_next("ul")
        if ul:
            prods = [li.get_text(strip=True) for li in ul.find_all("li")][:8]

    # Händler ("Häufig gekauft bei") -> retailer-slugs aus Logo-URLs
    ret = re.findall(r"/images/retailers/([a-z0-9]+)\.svg", html)
    ret = [r for r in dict.fromkeys(ret) if r in RET_KNOWN]

    # >>> DER WICHTIGE TEIL: echter Hersteller-Link <<<
    extern = None
    for a in soup.find_all("a"):
        if a.get_text(strip=True).lower().startswith("zur aktion"):
            href = a.get("href", "")
            if href.startswith("http") and "geldzurueck.deals" not in href:
                extern = href
                break

    emoji, cat = categorize(title)

    # Betrag (amt) aus Fakten ableiten
    amt, free = derive_amount(title, typ, maxref)

    return {
        "b": brand_from_title(title),
        "t": title,
        "type": typ,
        "amt": amt,
        **({"free": 1} if free else {}),
        **({"d": days_left} if days_left is not None else {}),
        "score": score,
        "e": emoji,
        "cat": cat,
        "ret": ret or ["edeka", "rewe"],
        **({"photo": 1} if photo else {}),
        "period": period or "siehe Aktionsseite",
        "proof": proof,
        "qty": qty,
        "max": maxref or ("Kaufpreis (100%)" if free else "siehe Bedingungen"),
        "gesamt": gesamt,
        "dauer": dauer,
        "prods": prods or [brand_from_title(title) + " Aktionsprodukt"],
        "extern": extern,                      # None -> App nutzt Hersteller-Suche
        "real": 1,
        "desc": build_desc(title, amt, qty, photo),  # EIGENER Text, kein Original
    }


def brand_from_title(title):
    # erste 1-2 Wörter als Marke, grob
    words = re.split(r"[ –-]", title)
    return " ".join(words[:1]).strip() or title


def derive_amount(title, typ, maxref):
    t = title.lower()
    if "2-für-1" in t or "2 für 1" in t or "2-fuer-1" in t:
        return "2 für 1", False
    if "3-für-2" in t or "3 für 2" in t:
        return "3 für 2", False
    if "50" in t and "%" in t:
        return "50 %", False
    if typ == "gewinnspiel":
        return "Gewinnspiel", False
    if typ == "gratis" and not maxref:
        return "100% zurück", True
    if maxref:
        v = maxref.replace("EUR", "€").strip()
        return ("bis " + v) if "€" in v or "," in v else maxref, False
    return ("100% zurück", True) if typ == "gratis" else ("Cashback", False)


def build_desc(title, amt, qty, photo):
    """EIGENE Kurzbeschreibung aus Fakten – kein Originaltext."""
    foto = " + Produktfoto" if photo else ""
    return (f"{amt} sichern: {qty} kaufen, Kassenbon{foto} hochladen, "
            f"Formular ausfüllen – Geld kommt aufs Konto.")


def main():
    print("robots.txt prüfen …")
    check_robots()
    print("Aktive Aktionen sammeln …")
    slugs = collect_slugs()
    print(f"{len(slugs)} Aktionen gefunden. Detailseiten laden …")

    offers = []
    for i, slug in enumerate(slugs, 1):
        try:
            o = parse_detail(slug)
            offers.append(o)
            link = "✓ Link" if o["extern"] else "· kein Link"
            print(f"  [{i}/{len(slugs)}] {o['t'][:48]:48} {link}")
        except Exception as e:
            print(f"  [{i}/{len(slugs)}] FEHLER bei {slug}: {e}")

    with open("offers.json", "w", encoding="utf-8") as f:
        json.dump(offers, f, ensure_ascii=False, indent=1)
    have = sum(1 for o in offers if o["extern"])
    print(f"\nFertig: {len(offers)} Aktionen -> offers.json "
          f"({have} mit echtem Hersteller-Link).")


if __name__ == "__main__":
    main()
