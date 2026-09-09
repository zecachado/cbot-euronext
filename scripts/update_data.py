#!/usr/bin/env python3
"""Refreshes data.json for the CBOT x Euronext grains board.

Sources:
  - Chicago (CBOT):  Yahoo Finance chart API, dated futures symbols (no scraping).
  - EUR/USD:         Yahoo Finance chart API.
  - Euronext/MATIF:  agritel.com/en/quotes (server-rendered HTML, ids like EMANOV26_VALUE).
  - Fisico (French cash refs): agritel.com generic price table + terre-net.fr (Ble tendre Rouen).
  - SIMA (Portugal):  regsima.gpp.pt weekly PDF bulletin, direct static path guess by ISO week.

Never wholesale-replaces an array: merges by key, only touching rows it
actually fetched fresh data for. If a source fails, that section of the
board simply keeps its previous value - no fabricated data is written.
"""
import io
import json
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA}
DATA_PATH = "data.json"

PT_MONTH = {1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
            7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"}
PT_MONTH_NUM = {v: k for k, v in PT_MONTH.items()}
EN_MONTH_NUM = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

CORN_WHEAT_CYCLE = [(3, "H"), (5, "K"), (7, "N"), (9, "U"), (12, "Z")]
SOYBEAN_CYCLE = [(1, "F"), (3, "H"), (5, "K"), (7, "N"), (8, "Q"), (9, "U"), (11, "X")]


def log(msg):
    print(msg, file=sys.stderr)


def contract_label(month_num, year):
    return f"{PT_MONTH[month_num]}-{year % 100:02d}"


def contract_sort_key(label):
    m, y = label.split("-")
    return (int(y), PT_MONTH_NUM.get(m, 0))


def next_contracts(cycle, today, count=5):
    candidates = []
    for dy in range(0, 3):
        year = today.year + dy
        for mm, letter in cycle:
            if dy == 0 and mm < today.month:
                continue
            candidates.append((year, mm, letter))
    candidates.sort()
    return candidates[:count]


# ---------------------------------------------------------------- Chicago --

def fetch_yahoo_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return meta["regularMarketPrice"]


def fetch_chicago(today):
    crops = [
        ("Milho", "corn", "ZC", CORN_WHEAT_CYCLE),
        ("Trigo", "wheat", "ZW", CORN_WHEAT_CYCLE),
        ("Soja", "soybean", "ZS", SOYBEAN_CYCLE),
    ]
    rows = []
    for name, commodity, root, cycle in crops:
        for year, mm, letter in next_contracts(cycle, today):
            symbol = f"{root}{letter}{year % 100:02d}.CBT"
            try:
                price_cents = fetch_yahoo_price(symbol)
                rows.append({
                    "name": name,
                    "commodity": commodity,
                    "contract": contract_label(mm, year),
                    "value": round(price_cents / 100.0, 4),
                })
                log(f"  chicago {symbol} -> {price_cents}")
            except Exception as exc:
                log(f"  chicago {symbol} FAILED: {exc}")
    return rows


def fetch_eurusd():
    return fetch_yahoo_price("EURUSD=X")


# --------------------------------------------------------------- Euronext --

AGRITEL_CODE_TO_CROP = {"EMA": "Milho", "EBM": "Trigo mole", "ECO": "Colza"}


def fetch_agritel_html():
    r = requests.get("https://www.agritel.com/en/quotes", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_euronext(html):
    rows = []
    pattern = re.compile(r"id='([A-Z]{3})([A-Z]{3})(\d{2})_VALUE'[^>]*>([\d.]+)")
    for code, mon, yy, value in pattern.findall(html):
        crop = AGRITEL_CODE_TO_CROP.get(code)
        month_num = EN_MONTH_NUM.get(mon)
        if not crop or not month_num:
            continue
        year = 2000 + int(yy)
        rows.append({
            "name": crop,
            "contract": contract_label(month_num, year),
            "value": float(value),
        })
        log(f"  euronext {code}{mon}{yy} -> {value}")
    return rows


# ----------------------------------------------------------------- Fisico --

AGRITEL_PHYSICAL_ROWS = [
    ("Trigo duro", "La Pallice, base jul.", r"Durum wheat delivered La Pallice[^<]*<[^>]*><[^>]*></td><td[^>]*>([\d.]+)"),
    ("Milho", "Bordeaux, base jul.", r"Corn delivered Bordeaux[^<]*<[^>]*><[^>]*></td><td[^>]*>([\d.]+)"),
    ("Colza", "FOB Moselle, colheita 26", r"Rapes+ed FOB Moselle[^<]*<[^>]*><[^>]*></td><td[^>]*>([\d.]+)"),
]


def fetch_fisico_agritel(html):
    rows = []
    for name, local, pattern in AGRITEL_PHYSICAL_ROWS:
        m = re.search(pattern, html)
        if m:
            rows.append({"name": name, "local": local, "value": float(m.group(1))})
            log(f"  fisico {name} -> {m.group(1)}")
        else:
            log(f"  fisico {name} NOT FOUND on agritel")
    return rows


def fetch_fisico_terrenet():
    try:
        r = requests.get("https://www.terre-net.fr/marche-agricole", headers=HEADERS, timeout=20)
        r.raise_for_status()
        html = r.text
        idx = html.find("tendre Rouen")
        if idx < 0:
            return []
        window = html[idx:idx + 600]
        m = re.search(r'([\d]+(?:[.,]\d+)?)\s*(?:&#x20AC;|€)\s*/t', window)
        if m:
            value = float(m.group(1).replace(",", "."))
            log(f"  fisico Trigo mole (Rouen/terre-net) -> {value}")
            return [{"name": "Trigo mole", "local": "Rouen (Terre-net)", "value": value}]
    except Exception as exc:
        log(f"  terre-net FAILED: {exc}")
    return []


# ------------------------------------------------------------------- SIMA --

SIMA_CROPS = ["Milho Forrageiro", "Cevada Forrageira", "Trigo Mole Forrageiro", "Trigo Mole Panificável"]
PRICE_RE = re.compile(r"^\d{2,3}[.,]\d$")


def fetch_sima(existing_sima_history):
    try:
        import pdfplumber
    except ImportError:
        log("  sima SKIPPED: pdfplumber not installed")
        return None

    known_weeks = {h["week"] for h in (existing_sima_history or [])}
    today = datetime.now(timezone.utc)
    iso_year, iso_week, _ = today.isocalendar()

    for week in (iso_week, iso_week - 1, iso_week - 2):
        if week in known_weeks or week < 1:
            continue
        url = f"https://regsima.gpp.pt/regsima/static/pdf/{iso_year}/arvs/arvs_News{week}.pdf"
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                log(f"  sima week {week}: HTTP {r.status_code} (not published yet)")
                continue
        except Exception as exc:
            log(f"  sima week {week} FAILED: {exc}")
            continue

        log(f"  sima week {week}: PDF found, parsing...")
        try:
            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                page1_text = pdf.pages[0].extract_text() or ""
                date_range = parse_week_dates(page1_text)

                values = {}
                page4 = pdf.pages[3] if len(pdf.pages) > 3 else None
                if page4:
                    for table in (page4.extract_tables() or []):
                        current_crop = None
                        for row in table:
                            cells = [(c or "").strip() for c in row]
                            for c in cells:
                                if c in SIMA_CROPS:
                                    current_crop = c
                                    break
                            if current_crop and "Lisboa" in cells:
                                for c in cells:
                                    if PRICE_RE.match(c):
                                        values[current_crop] = float(c.replace(",", "."))
                                        break
        except Exception as exc:
            log(f"  sima week {week} PARSE FAILED: {exc}")
            continue

        if len(values) == 4 and date_range:
            log(f"  sima week {week}: {values}, {date_range}")
            return {"week": week, "date_range": date_range, "values": values}
        else:
            log(f"  sima week {week}: incomplete extraction ({values}), skipping")

    return None


def parse_week_dates(text):
    m = re.search(r"(\d{2})/(\d{2})\s*a\s*(\d{2})/(\d{2})/\d{4}", text)
    if not m:
        return None
    d1, m1, d2, m2 = m.groups()
    if m1 == m2:
        return f"{d1}-{d2}/{m2}"
    return f"{d1}/{m1}-{d2}/{m2}"


# ------------------------------------------------------------------- Merge --

def merge_rows(existing, fresh, key_fields):
    by_key = {tuple(r[k] for k in key_fields): dict(r) for r in existing}
    for r in fresh:
        by_key[tuple(r[k] for k in key_fields)] = {**by_key.get(tuple(r[k] for k in key_fields), {}), **r}
    return list(by_key.values())


def sort_chicago(rows):
    order = {"Milho": 0, "Trigo": 1, "Soja": 2}
    return sorted(rows, key=lambda r: (order.get(r["name"], 9), contract_sort_key(r["contract"])))


def sort_euronext(rows):
    order = {"Milho": 0, "Trigo mole": 1, "Colza": 2}
    return sorted(rows, key=lambda r: (order.get(r["name"], 9), contract_sort_key(r["contract"])))


def main():
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"chicago": [], "euronext": [], "fisico": [], "sima": [], "simaHistory": []}

    today = datetime.now(timezone.utc)

    log("Fetching Chicago (Yahoo Finance)...")
    fresh_chicago = fetch_chicago(today)
    if fresh_chicago:
        data["chicago"] = sort_chicago(merge_rows(data.get("chicago", []), fresh_chicago, ("name", "contract")))

    log("Fetching EUR/USD (Yahoo Finance)...")
    try:
        data["eurusd"] = round(fetch_eurusd(), 4)
        log(f"  eurusd -> {data['eurusd']}")
    except Exception as exc:
        log(f"  eurusd FAILED: {exc}")

    log("Fetching Euronext + Fisico (agritel.com)...")
    try:
        html = fetch_agritel_html()
        fresh_euronext = fetch_euronext(html)
        if fresh_euronext:
            data["euronext"] = sort_euronext(merge_rows(data.get("euronext", []), fresh_euronext, ("name", "contract")))
        fresh_fisico_a = fetch_fisico_agritel(html)
        if fresh_fisico_a:
            data["fisico"] = merge_rows(data.get("fisico", []), fresh_fisico_a, ("name", "local"))
    except Exception as exc:
        log(f"  agritel FAILED: {exc}")

    log("Fetching Fisico Ble tendre (terre-net.fr)...")
    fresh_fisico_t = fetch_fisico_terrenet()
    if fresh_fisico_t:
        data["fisico"] = merge_rows(data.get("fisico", []), fresh_fisico_t, ("name", "local"))

    log("Fetching SIMA (regsima.gpp.pt)...")
    sima_result = fetch_sima(data.get("simaHistory", []))
    if sima_result:
        week = sima_result["week"]
        date_range = sima_result["date_range"]
        values = sima_result["values"]
        local_label = f"Lisboa (sem. {week}, {date_range})"
        data["sima"] = [{"name": crop, "local": local_label, "value": values[crop]} for crop in SIMA_CROPS]

        history_key_map = {"Milho Forrageiro": "milho", "Cevada Forrageira": "cevada",
                            "Trigo Mole Forrageiro": "trigoForr", "Trigo Mole Panificável": "trigoPanif"}
        entry = {"week": week}
        for crop, key in history_key_map.items():
            entry[key] = values[crop]
        history = data.get("simaHistory", [])
        history = [h for h in history if h["week"] != week] + [entry]
        history.sort(key=lambda h: h["week"])
        data["simaHistory"] = history[-14:]

    lisbon_now = today.astimezone(ZoneInfo("Europe/Lisbon"))
    data["updatedAt"] = lisbon_now.strftime("%d/%m %H:%M")

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    log(f"Wrote {DATA_PATH}, updatedAt={data['updatedAt']}")


if __name__ == "__main__":
    main()
