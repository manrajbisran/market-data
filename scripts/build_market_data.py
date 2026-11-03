#!/usr/bin/env python3
"""Build the monthly market data file for one area (e.g. Hounslow).

Sources (both free, Open Government Licence v3.0):
  * HM Land Registry Price Paid Data (yearly CSVs, refreshed monthly)
  * UK House Price Index (HM Land Registry SPARQL endpoint)
  * ONS Price Index of Private Rents (monthly spreadsheet)
  * postcodes.io for postcode district centre points (map positions)

Needs openpyxl for the ONS spreadsheet (pip install -r requirements.txt);
everything else is standard library.

    python scripts/build_market_data.py --config config/hounslow.json \
        --out data/hounslow.json --summary data/hounslow-summary.txt
"""

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

PPD_BASE_URL = os.environ.get(
    "PPD_BASE_URL", "https://price-paid-data.publicdata.landregistry.gov.uk"
)
PPD_CACHE_DIR = os.environ.get("PPD_CACHE_DIR")  # optional, for local reruns
HPI_SPARQL_URL = os.environ.get(
    "HPI_SPARQL_URL", "https://landregistry.data.gov.uk/landregistry/query"
)
PIPR_PAGE_URL = os.environ.get(
    "PIPR_PAGE_URL",
    "https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/priceindexofprivaterentsukmonthlypricestatistics",
)
ONS_BASE_URL = "https://www.ons.gov.uk"
POSTCODES_URL = os.environ.get("POSTCODES_URL", "https://api.postcodes.io")
# ONS refuses requests whose user agent does not start with "Mozilla/5.0".
USER_AGENT = "Mozilla/5.0 (compatible; DiamondMove-market-data/1.0; +https://www.diamondmove.co.uk)"

PROPERTY_TYPES = {"D": "detached", "S": "semi_detached", "T": "terraced", "F": "flat"}
TYPE_LABELS = {
    "detached": "Detached",
    "semi_detached": "Semi-detached",
    "terraced": "Terraced",
    "flat": "Flat / maisonette",
}
TYPE_PHRASES = {
    "detached": "detached homes",
    "semi_detached": "semi-detached homes",
    "terraced": "terraced homes",
    "flat": "flats and maisonettes",
}
TENURE = {"F": "Freehold", "L": "Leasehold"}
# (key, months, heading label, phrase)
PERIODS = [("3m", 3, "3 Month", "3 months"), ("1y", 12, "1 Year", "year"),
           ("3y", 36, "3 Year", "3 years"), ("5y", 60, "5 Year", "5 years")]
MIN_DISTRICT_SALES = 20  # rolling 12-month medians from fewer sales are left out
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

ATTRIBUTION = (
    "Contains HM Land Registry data © Crown copyright and database right {year}. "
    "This data is licensed under the Open Government Licence v3.0."
)
ATTRIBUTION_ONS = (
    "Private rents: Office for National Statistics, Price Index of Private Rents, "
    "licensed under the Open Government Licence v3.0."
)
ATTRIBUTION_POSTCODES = (
    "Maps and areas: postcode locations and wards from the ONS Postcode Directory via postcodes.io. "
    "Contains OS data © Crown copyright and database right {year}. "
    "Contains Royal Mail data © Royal Mail copyright and database right {year}. "
    "Source: Office for National Statistics licensed under the Open Government Licence v3.0."
)
ATTRIBUTION_HPI = (
    "UK House Price Index: HM Land Registry, Office for National Statistics, "
    "Registers of Scotland and Land & Property Services Northern Ireland, "
    "licensed under the Open Government Licence v3.0."
)


# ---------------------------------------------------------------- helpers

def http_get(url, data=None, headers=None, retries=4):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        wait = 2 ** (attempt + 1)
        print(f"  retry {attempt + 1} for {url} in {wait}s ({err})", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {err}")


def month_key(date_str):
    return date_str[:7]  # "2026-07-14 00:00" -> "2026-07"


def add_months(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_label(ym):
    return f"{MONTHS[int(ym[5:7]) - 1]} {ym[:4]}"


def month_name(ym):
    return dt.date(int(ym[:4]), int(ym[5:7]), 1).strftime("%B %Y")


def pct_change(new, old):
    if not new or not old:
        return None
    return round((new - old) / old * 100, 1)


def money(n):
    return f"£{n:,.0f}"


def describe_change(pct):
    if pct is None:
        return None
    if abs(pct) < 0.05:
        return "unchanged"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%"


# ---------------------------------------------------------------- price paid

def outcode(postcode):
    return postcode.split(" ")[0].upper() if postcode else ""


def area_matches(area, row):
    match = area["match"]
    if "district" in match and row["district"] != match["district"].upper():
        return False
    if "outcode" in match and row["outcode"] != match["outcode"].upper():
        return False
    return True


def parse_ppd(lines, areas):
    """Yield the rows from a Price Paid CSV that fall in any configured area."""
    for rec in csv.reader(lines):
        if len(rec) < 16:
            continue
        row = {
            "id": rec[0].strip("{}"),
            "price": int(rec[1]),
            "date": rec[2][:10],
            "postcode": rec[3],
            "outcode": outcode(rec[3]),
            "type": rec[4],
            "new_build": rec[5] == "Y",
            "tenure": rec[6],
            "paon": rec[7],
            "saon": rec[8],
            "street": rec[9],
            "town": rec[11],
            "district": rec[12].upper(),
            "category": rec[14],
            "status": rec[15],
        }
        if row["status"] == "D":
            continue
        if any(area_matches(a, row) for a in areas):
            yield row


def load_ppd(years, areas):
    rows = {}
    for year in years:
        url = f"{PPD_BASE_URL}/pp-{year}.csv"
        cached = os.path.join(PPD_CACHE_DIR, f"pp-{year}.csv") if PPD_CACHE_DIR else None
        if cached and os.path.exists(cached):
            resp = open(cached, "rb")
        else:
            print(f"Downloading {url}", file=sys.stderr)
            try:
                resp = http_get(url)
            except urllib.error.HTTPError as e:
                if e.code == 404 and year == max(years):
                    print(f"  {year} file not published yet, skipping", file=sys.stderr)
                    continue
                raise
            if cached:  # local development only: keep a copy so reruns skip the download
                os.makedirs(PPD_CACHE_DIR, exist_ok=True)
                with resp, open(cached, "wb") as f:
                    f.write(resp.read())
                resp = open(cached, "rb")
        with resp:
            text = io.TextIOWrapper(resp, encoding="utf-8", newline="")
            for row in parse_ppd(text, areas):
                rows[row["id"]] = row  # later files win on duplicate ids
    return list(rows.values())


def summarise(prices):
    if not prices:
        return {"sales": 0, "mean": None, "median": None}
    return {
        "sales": len(prices),
        "mean": round(statistics.fmean(prices)),
        "median": round(statistics.median(prices)),
    }


def address(row):
    # PAON is a house number or name ("12", "ROSE COTTAGE"); SAON is a flat ("FLAT 3").
    line = " ".join(p for p in (row["paon"], row["street"]) if p)
    return ", ".join(p.title() for p in (row["saon"], line) if p)


def area_stats(area, rows, end, recent_n):
    mine = [r for r in rows if area_matches(area, r)]
    # Standard residential sales only: category A, and type O ("other") excluded.
    std = [r for r in mine if r["category"] == "A" and r["type"] in PROPERTY_TYPES]

    def window(first, last):
        return [r for r in std if first <= month_key(r["date"]) <= last]

    last12 = window(add_months(end, -11), end)
    prior12 = window(add_months(end, -23), add_months(end, -12))
    cur, prev = summarise([r["price"] for r in last12]), summarise([r["price"] for r in prior12])

    by_type = {}
    for code, key in PROPERTY_TYPES.items():
        c = summarise([r["price"] for r in last12 if r["type"] == code])
        p = summarise([r["price"] for r in prior12 if r["type"] == code])
        c["median_change_pct"] = pct_change(c["median"], p["median"]) if c["sales"] >= 10 and p["sales"] >= 10 else None
        by_type[key] = c

    # Rolling 3-month blocks ending at `end`, oldest first, for the trend chart.
    quarterly = []
    for i in range(11, -1, -1):
        last = add_months(end, -3 * i)
        q = window(add_months(last, -2), last)
        s = summarise([r["price"] for r in q])
        quarterly.append({
            "period_end": last,
            "label": f"3 months to {month_label(last)}",
            "sales": s["sales"],
            "median": s["median"] if s["sales"] >= 5 else None,
        })

    recent = sorted(std, key=lambda r: (r["date"], r["id"]), reverse=True)[:recent_n]
    return {
        "id": area["id"],
        "label": area["label"],
        "place": area.get("place"),
        "last_12_months": cur,
        "previous_12_months": prev,
        "median_change_pct": pct_change(cur["median"], prev["median"]),
        "mean_change_pct": pct_change(cur["mean"], prev["mean"]),
        "sales_change_pct": pct_change(cur["sales"], prev["sales"]),
        "by_type": by_type,
        "quarterly": quarterly,
        "recent_sales": [
            {
                "date": r["date"],
                "price": r["price"],
                "address": address(r),
                "postcode": r["postcode"],
                "type": TYPE_LABELS[PROPERTY_TYPES[r["type"]]],
                "new_build": r["new_build"],
                "tenure": TENURE.get(r["tenure"], ""),
            }
            for r in recent
        ],
    }


# ---------------------------------------------------------------- UK HPI

HPI_FIELDS = {
    "average_price": "averagePrice",
    "annual_change_pct": "percentageAnnualChange",
    "monthly_change_pct": "percentageChange",
    "detached": "averagePriceDetached",
    "detached_change_pct": "percentageAnnualChangeDetached",
    "semi_detached": "averagePriceSemiDetached",
    "semi_detached_change_pct": "percentageAnnualChangeSemiDetached",
    "terraced": "averagePriceTerraced",
    "terraced_change_pct": "percentageAnnualChangeTerraced",
    "flat": "averagePriceFlatMaisonette",
    "flat_change_pct": "percentageAnnualChangeFlatMaisonette",
    "first_time_buyer": "averagePriceFirstTimeBuyer",
    "sales_volume": "salesVolume",
}


def hpi_query(region):
    optional = "\n".join(
        f"  OPTIONAL {{ ?r ukhpi:{prop} ?{name} }}" for name, prop in HPI_FIELDS.items()
    )
    return f"""PREFIX ukhpi: <http://landregistry.data.gov.uk/def/ukhpi/>
SELECT ?month {' '.join('?' + n for n in HPI_FIELDS)} WHERE {{
  ?r ukhpi:refRegion <http://landregistry.data.gov.uk/id/region/{region}> ;
     ukhpi:refMonth ?month .
{optional}
}} ORDER BY ?month"""


def load_hpi(region):
    print(f"Querying UK HPI for {region}", file=sys.stderr)
    body = urllib.parse.urlencode({"query": hpi_query(region)}).encode()
    with http_get(HPI_SPARQL_URL, data=body, headers={
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    }) as resp:
        payload = json.load(resp)
    return parse_hpi(payload)


def parse_hpi(payload):
    months = []
    for b in payload["results"]["bindings"]:
        rec = {"month": b["month"]["value"][:7]}
        for name in HPI_FIELDS:
            if name in b:
                v = float(b[name]["value"])
                rec[name] = round(v, 1) if name.endswith("_pct") else round(v)
        if "average_price" in rec:
            months.append(rec)
    if not months:
        raise RuntimeError("UK HPI query returned no rows; check the region slug")
    return sorted(months, key=lambda m: m["month"])


def hpi_block(region_label, months, n):
    latest = months[-1]
    by_type = {
        key: {"average_price": latest.get(key), "annual_change_pct": latest.get(f"{key}_change_pct")}
        for key in PROPERTY_TYPES.values()
    }
    return {
        "region": region_label,
        "latest_month": latest["month"],
        "latest_month_label": month_label(latest["month"]),
        "average_price": latest["average_price"],
        "annual_change_pct": latest.get("annual_change_pct"),
        "monthly_change_pct": latest.get("monthly_change_pct"),
        "first_time_buyer_price": latest.get("first_time_buyer"),
        "by_type": by_type,
        "change": series_changes(months, "average_price"),
        "series": [{"month": m["month"], "average_price": m["average_price"]} for m in months[-n:]],
    }


# ---------------------------------------------------------------- period changes

def series_changes(points, key):
    """Change over each period from a monthly series [{month, key}], oldest first."""
    by_month = {p["month"]: p[key] for p in points if p.get(key)}
    latest = points[-1]["month"]
    out = {}
    for pk, n, _, _ in PERIODS:
        out[pk] = pct_change(by_month.get(latest), by_month.get(add_months(latest, -n)))
    return out


def series_trend(points, key, n):
    """Cumulative % change from the start of the last n months, for the period charts."""
    pts = points[-(n + 1):]
    if len(pts) < 2 or not pts[0].get(key):
        return []
    base = pts[0][key]
    return [{"month": p["month"], "pct": round((p[key] - base) / base * 100, 2)} for p in pts]


# ---------------------------------------------------------------- postcode districts

def district_stats(d, rows, end):
    """Median of each rolling 12-month window, compared across the standard periods."""
    by_month = defaultdict(list)
    for r in rows:
        if r["outcode"] == d["id"] and r["category"] == "A" and r["type"] in PROPERTY_TYPES:
            by_month[month_key(r["date"])].append(r["price"])

    def rolling(last):
        prices = [p for i in range(12) for p in by_month.get(add_months(last, -i), [])]
        return (round(statistics.median(prices)) if len(prices) >= MIN_DISTRICT_SALES else None), len(prices)

    median, sales = rolling(end)
    return {
        "id": d["id"],
        "place": d.get("place"),
        "sales_12m": sales,
        "median_12m": median,
        "change": {pk: pct_change(median, rolling(add_months(end, -n))[0]) for pk, n, _, _ in PERIODS},
    }


def load_centroids(ids):
    """Centre point of each postcode district from postcodes.io; the map is skipped if unavailable."""
    out = {}
    for oc in ids:
        try:
            with http_get(f"{POSTCODES_URL}/outcodes/{urllib.parse.quote(oc)}", retries=2) as resp:
                res = json.load(resp)["result"]
            out[oc] = {"lat": round(res["latitude"], 5), "lng": round(res["longitude"], 5)}
        except Exception as e:  # noqa: BLE001 - the map is optional
            print(f"  no centre point for {oc}: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- borough map

def load_postcode_points(postcodes):
    """Grid position (OS eastings/northings, metres), local authority and ward of each postcode, via postcodes.io."""
    out = {}
    pcs = sorted(postcodes)
    for i in range(0, len(pcs), 100):
        body = json.dumps({"postcodes": pcs[i:i + 100]}).encode()
        with http_get(f"{POSTCODES_URL}/postcodes", data=body, headers={"Content-Type": "application/json"}) as resp:
            for item in json.load(resp)["result"]:
                res = item.get("result")
                if res and res.get("eastings"):
                    out[item["query"]] = (res["eastings"], res["northings"], res.get("admin_district"), res.get("admin_ward"))
    return out


def _loops(cells):
    """Outline loops (in cell-corner coordinates) around a set of (col, row) grid cells."""
    nxt = defaultdict(list)
    for c, r in cells:
        # Clockwise edges on the sides that face a cell outside the set (row grows upwards, like northings).
        if (c, r + 1) not in cells: nxt[(c, r + 1)].append((c + 1, r + 1))
        if (c + 1, r) not in cells: nxt[(c + 1, r + 1)].append((c + 1, r))
        if (c, r - 1) not in cells: nxt[(c + 1, r)].append((c, r))
        if (c - 1, r) not in cells: nxt[(c, r)].append((c, r + 1))
    loops = []
    while nxt:
        start = next(iter(nxt))
        loop, p = [start], start
        while True:
            q = nxt[p].pop()
            if not nxt[p]:
                del nxt[p]
            if q == start:
                break
            loop.append(q)
            p = q
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def _smooth(loop, rounds=2):
    """Round off the grid steps: join edge midpoints (staircases become diagonals), then Chaikin corner cutting."""
    pts = [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in zip(loop, loop[1:] + loop[:1])]
    # Drop points in the middle of straight runs, to keep the paths small.
    pts = [p for i, p in enumerate(pts)
           if (p[0] - pts[i - 1][0]) * (pts[(i + 1) % len(pts)][1] - p[1]) != (p[1] - pts[i - 1][1]) * (pts[(i + 1) % len(pts)][0] - p[0])]
    for _ in range(rounds):
        out = []
        for i, p in enumerate(pts):
            q = pts[(i + 1) % len(pts)]
            out += [(p[0] * .75 + q[0] * .25, p[1] * .75 + q[1] * .25), (p[0] * .25 + q[0] * .75, p[1] * .25 + q[1] * .75)]
        pts = out
    return pts


def _area(loop):
    return abs(sum(p[0] * q[1] - q[0] * p[1] for p, q in zip(loop, loop[1:] + loop[:1]))) / 2


def _label_point(cs, scale, height):
    """The cell deepest inside the district (furthest from its edge or any hole), nearest the middle on ties."""
    depth, frontier = {}, [p for p in cs if any(n not in cs for n in ((p[0] + 1, p[1]), (p[0] - 1, p[1]), (p[0], p[1] + 1), (p[0], p[1] - 1)))]
    for p in frontier:
        depth[p] = 0
    while frontier:
        nxt = []
        for c, r in frontier:
            for n in ((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)):
                if n in cs and n not in depth:
                    depth[n] = depth[(c, r)] + 1
                    nxt.append(n)
        frontier = nxt
    mc, mr = sum(c for c, _ in cs) / len(cs), sum(r for _, r in cs) / len(cs)
    c, r = max(cs, key=lambda p: (depth[p], -((p[0] - mc) ** 2 + (p[1] - mr) ** 2)))
    return [round((c + .5) * scale), round(height - (r + .5) * scale)]


def build_borough_map(points, borough, district_ids, cell=100, reach=300, width=1000):
    """Approximate postcode district shapes inside the borough, drawn from where its postcodes are."""
    return build_area_map([(p[0], p[1], pc.split(" ")[0]) for pc, p in points.items()
                           if p[2] == borough and pc.split(" ")[0] in district_ids], cell, reach, width)


def build_area_map(pts, cell=100, reach=300, width=1000):
    """SVG shapes for groups of postcodes, from a list of (eastings, northings, group).

    Each grid cell takes the group of the nearest postcode, and stays empty when no postcode is
    within `reach` metres (parks, reservoirs). Returns SVG path data per group plus the outline.
    """
    if len(pts) < 50:
        return None
    buckets = defaultdict(list)
    for p in pts:
        buckets[(int(p[0] // reach), int(p[1] // reach))].append(p)
    e0, e1 = min(p[0] for p in pts) - reach, max(p[0] for p in pts) + reach
    n0, n1 = min(p[1] for p in pts) - reach, max(p[1] for p in pts) + reach
    cells = defaultdict(set)
    for c in range(int((e1 - e0) // cell) + 1):
        for r in range(int((n1 - n0) // cell) + 1):
            x, y = e0 + (c + .5) * cell, n0 + (r + .5) * cell
            bx, by = int(x // reach), int(y // reach)
            best, bd = None, reach * reach
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for p in buckets.get((bx + dx, by + dy), ()):
                        d = (p[0] - x) ** 2 + (p[1] - y) ** 2
                        if d < bd:
                            best, bd = p[2], d
            if best:
                cells[best].add((c, r))
    cols, rows = int((e1 - e0) // cell) + 1, int((n1 - n0) // cell) + 1
    scale = width / cols
    height = round(rows * scale)

    def path(loops):
        d = ""
        for loop in loops:
            if _area(loop) < 6:  # specks and pinholes a few cells across
                continue
            pts2 = _smooth(loop)
            d += "M" + "L".join(f"{x * scale:.0f} {height - y * scale:.0f}" for x, y in pts2) + "Z"
        return d

    every = set().union(*cells.values())
    return {
        "width": width,
        "height": height,
        "outline": path(_loops(every)),
        "districts": {key: path(_loops(cs)) for key, cs in sorted(cells.items())},
        "labels": {oc: _label_point(cs, scale, height) for oc, cs in cells.items()},
    }


# ---------------------------------------------------------------- ONS private rents

PIPR_COLUMNS = {
    "index": "Index",
    "rent": "Rental price",
    "annual_change_pct": "Annual change",
    "one_bed": "Rental price one bed",
    "two_bed": "Rental price two bed",
    "three_bed": "Rental price three bed",
    "four_plus_bed": "Rental price four or more bed",
    "detached": "Rental price detached",
    "semi_detached": "Rental price semidetached",
    "terraced": "Rental price terraced",
    "flat": "Rental price flat maisonette",
}


def latest_pipr_url():
    with http_get(PIPR_PAGE_URL) as resp:
        page = resp.read().decode("utf-8", "replace")
    m = re.search(r'href="(/file\?uri=[^"]+\.xlsx)"', page)
    if not m:
        raise RuntimeError("No spreadsheet link found on the ONS private rents page")
    return ONS_BASE_URL + m.group(1)


def parse_pipr_rows(rows, names):
    """rows: iterable of tuples from the 'Table 1' sheet. Returns {area name: [month records]}."""
    out = {n: [] for n in names}
    header = None
    for r in rows:
        if header is None:
            if r and r[0] == "Time period":
                header = {v: i for i, v in enumerate(r) if v}
            continue
        if not r or r[2] not in out:
            continue
        when = r[0]
        month = when.strftime("%Y-%m") if hasattr(when, "strftime") else str(when)[:7]
        rec = {"month": month}
        for key, col in PIPR_COLUMNS.items():
            v = r[header[col]] if col in header else None
            if isinstance(v, (int, float)):
                rec[key] = round(v, 1) if key in ("index", "annual_change_pct") else round(v)
        if "rent" in rec:
            out[r[2]].append(rec)
    for n in out:
        out[n].sort(key=lambda x: x["month"])
    return out


def load_rents(names):
    import openpyxl  # only needed here, so the tests run without it

    url = latest_pipr_url()
    print(f"Downloading {url}", file=sys.stderr)
    with http_get(url) as resp:
        data = resp.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = next(ws for ws in wb.worksheets if ws.title.lower().startswith("table"))
    return parse_pipr_rows(sheet.iter_rows(values_only=True), names)


def rent_block(name, months, n):
    latest = months[-1]
    return {
        "area": name,
        "latest_month": latest["month"],
        "rent": latest["rent"],
        "annual_change_pct": latest.get("annual_change_pct"),
        "change": series_changes(months, "index"),
        "by_bedrooms": {k: latest.get(k) for k in ("one_bed", "two_bed", "three_bed", "four_plus_bed")},
        "by_type": {k: latest.get(k) for k in PROPERTY_TYPES.values()},
        "series": [{"month": m["month"], "rent": m["rent"], "index": m.get("index")} for m in months[-n:]],
    }


# ---------------------------------------------------------------- towns

def rolling_medians(prices_by_month, end, months, min_sales=MIN_DISTRICT_SALES):
    """{month: median of the 12 months to that month} for the last `months` months to `end`."""
    out = {}
    for k in range(months + 1):
        last = add_months(end, -k)
        prices = [p for i in range(12) for p in prices_by_month.get(add_months(last, -i), [])]
        out[last] = (round(statistics.median(prices)) if len(prices) >= min_sales else None, len(prices))
    return out


def town_stats(town, rows, wards, end, recent_n):
    """Figures for one town, defined as a set of council wards. `wards` maps postcode -> (borough, ward)."""
    keys = {(town.get("borough", "Hounslow"), w) for w in town["wards"]}
    mine = [r for r in rows if wards.get(r["postcode"]) in keys]
    std = [r for r in mine if r["category"] == "A" and r["type"] in PROPERTY_TYPES]
    by_month, by_type = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for r in std:
        by_month[month_key(r["date"])].append(r["price"])
        by_type[PROPERTY_TYPES[r["type"]]][month_key(r["date"])].append(r["price"])
    roll = rolling_medians(by_month, end, 72)
    median, sales = roll[end]
    series = [{"month": m, "median": roll[m][0]} for m in sorted(roll) if roll[m][0]]
    types = {}
    for key in PROPERTY_TYPES.values():
        r = rolling_medians(by_type[key], end, 12, min_sales=5)
        types[key] = {"median": r[end][0], "sales": r[end][1], "change_1y": pct_change(r[end][0], r[add_months(end, -12)][0])}
    recent = sorted(std, key=lambda r: (r["date"], r["id"]), reverse=True)[:recent_n]
    return {
        "id": town["id"],
        "name": town["name"],
        "borough": town.get("borough", "Hounslow"),
        "wards": [k[1] for k in sorted(keys)],
        "url": town.get("url"),
        "sales_12m": sales,
        "median_12m": median,
        "previous_sales_12m": roll[add_months(end, -12)][1],
        "change": {pk: pct_change(median, roll[add_months(end, -n)][0]) for pk, n, _, _ in PERIODS},
        "trends": {pk: series_trend([p for p in series if p["month"] >= add_months(end, -n)], "median", n) for pk, n, _, _ in PERIODS},
        "by_type": types,
        "recent_sales": [
            {"date": r["date"], "price": r["price"], "address": address(r), "postcode": r["postcode"],
             "type": TYPE_LABELS[PROPERTY_TYPES[r["type"]]], "new_build": r["new_build"], "tenure": TENURE.get(r["tenure"], "")}
            for r in recent
        ],
    }


def town_text(t, towns, boroughs, end):
    name = t["name"]
    out = {}
    if not t["median_12m"]:
        return out
    ranked = sorted((x for x in towns if x["median_12m"]), key=lambda x: -x["median_12m"])
    rank = ranked.index(t) + 1
    line = (f"The median price paid for a home in {name} was {money(t['median_12m'])} in the 12 months to "
            f"{month_name(end)}, based on {t['sales_12m']:,} sales registered with HM Land Registry")
    ch = describe_change(t["change"]["1y"])
    line += f", {ch} on the previous 12 months." if ch else "."
    out["key_facts"] = line
    others = [x for x in ranked if x is not t]
    if others:
        out["compare"] = (f"That makes {name} the {ordinal(rank)} most expensive of the {len(ranked)} areas we cover, "
                          f"where medians range from {money(ranked[-1]['median_12m'])} in {ranked[-1]['name']} "
                          f"to {money(ranked[0]['median_12m'])} in {ranked[0]['name']}."
                          if 1 < rank < len(ranked) else
                          f"That makes {name} the {'most' if rank == 1 else 'least'} expensive of the {len(ranked)} areas we cover; "
                          f"the next is {others[0 if rank == 1 else -1]['name']} at {money(others[0 if rank == 1 else -1]['median_12m'])}.")
    if t["sales_12m"] < 60:
        out["key_facts"] += (f" {name} is a small area with relatively few sales, so its figures can move "
                             f"sharply from one period to the next.")
    parts = [f"{money(v['median'])} for {TYPE_PHRASES[k]} ({v['sales']} sales)" for k, v in t["by_type"].items() if v["median"]]
    if parts:
        out["by_type"] = (f"By property type, median prices in {name} were " + ", ".join(parts[:-1])
                          + (f" and {parts[-1]}." if len(parts) > 1 else f"{parts[0]}."))
    for pk, n, label, phrase in PERIODS:
        c = t["change"][pk]
        if c is None:
            continue
        line = f"Over the past {phrase}, the median price paid in {name} {moved(c)}."
        peers = [{**x, "c": x["change"][pk]} for x in towns]
        best, worst = best_worst(peers, "c")
        if best:
            line += (f" Across our {len([p for p in peers if p['c'] is not None])} areas, {best['name']} saw the strongest change "
                     f"({signed(best['c'])}) and {worst['name']} the weakest ({signed(worst['c'])}).")
        out[f"sales_{pk}"] = line
    b = next((x for x in boroughs if x["name"] == t["borough"]), None)
    if b and b.get("rent"):
        out["lettings"] = (f"Rents are published for the whole borough rather than for {name}: private rents in "
                           f"{b['name']} averaged {money(b['rent']['rent'])} a month in {month_name(b['rent']['latest_month'])}"
                           + (f", {describe_change(b['rent']['change']['1y'])} on a year earlier." if b["rent"]["change"].get("1y") is not None else "."))
        if b.get("hpi"):
            out["lettings"] += (f" Set against the {b['name']} average price of {money(b['hpi']['average_price'])}, "
                                f"that is a gross yield of about {b['gross_yield_pct']:.1f}% before costs.") if b.get("gross_yield_pct") else ""
    return out


def ordinal(n):
    return {1: "most", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
            9: "ninth", 10: "tenth"}.get(n, f"{n}th")


TOWN_HEADINGS = [("key_facts", "{name} property prices"), ("compare", "How {name} compares"),
                 ("by_type", "{name} prices by property type")] \
    + [(f"sales_{pk}", f"{label} house price change in {{name}}") for pk, _, label, _ in PERIODS] \
    + [("lettings", "Renting in {name}")]


def build_towns(config, rows, points, end, boroughs):
    if not config.get("towns") or not points:
        return [], None, {}
    wards = {pc: (p[2], p[3]) for pc, p in points.items() if len(p) > 3}
    towns = [town_stats(t, rows, wards, end, config.get("recent_sales", 10)) for t in config["towns"]]
    for t in towns:
        t["text"] = town_text(t, towns, boroughs, end)
    compare = compare_text(towns, boroughs, end)
    # Map: the borough plus any town outside it, each postcode coloured by its town (others grey).
    ward_town = {}
    for t, cfg in zip(towns, config["towns"]):
        for w in cfg["wards"]:
            ward_town[(cfg.get("borough", "Hounslow"), w)] = t["id"]
    borough = config.get("borough_name", config["name"])
    pts = [(p[0], p[1], ward_town.get((p[2], p[3]), "_other")) for p in points.values()
           if len(p) > 3 and (p[2] == borough or (p[2], p[3]) in ward_town)]
    return towns, build_area_map(pts), compare


def compare_text(towns, boroughs, end):
    """Written summary for the "Compare all towns" view."""
    priced = sorted((t for t in towns if t["median_12m"]), key=lambda t: -t["median_12m"])
    if len(priced) < 2:
        return {}
    out = {}
    total = sum(t["sales_12m"] for t in towns)
    out["overview"] = (f"Across the {len(towns)} areas we cover, HM Land Registry has registered {total:,} home sales in the "
                       f"12 months to {month_name(end)}. Median prices range from {money(priced[-1]['median_12m'])} in "
                       f"{priced[-1]['name']} to {money(priced[0]['median_12m'])} in {priced[0]['name']}.")
    for pk, n, label, phrase in PERIODS:
        best, worst = best_worst([{**t, "c": t["change"][pk]} for t in towns], "c")
        if best:
            rising = sum(1 for t in towns if (t["change"][pk] or 0) > 0)
            out[f"sales_{pk}"] = (f"Over the past {phrase}, median prices rose in {rising} of the {len(towns)} areas. "
                                  f"{best['name']} saw the strongest change ({signed(best['c'])}) and "
                                  f"{worst['name']} the weakest ({signed(worst['c'])}).")
    parts = []
    for key in PROPERTY_TYPES.values():
        ranked = sorted((t for t in towns if t["by_type"][key]["median"]), key=lambda t: -t["by_type"][key]["median"])
        if len(ranked) >= 2:
            parts.append(f"{TYPE_PHRASES[key]} cost most in {ranked[0]['name']} ({money(ranked[0]['by_type'][key]['median'])}) "
                         f"and least in {ranked[-1]['name']} ({money(ranked[-1]['by_type'][key]['median'])})")
    if parts:
        out["by_type"] = "By property type, " + "; ".join(parts) + "."
    rents = [b for b in boroughs if b.get("rent") and any(t["borough"] == b["name"] for t in towns)]
    if rents:
        out["lettings"] = "Rents are published by borough: " + " and ".join(
            f"{b['name']} averaged {money(b['rent']['rent'])} a month in {month_name(b['rent']['latest_month'])}"
            + (f" (a gross yield of about {b['gross_yield_pct']:.1f}%)" if b.get("gross_yield_pct") else "") for b in rents) + "."
    return out


COMPARE_HEADINGS = [("overview", "Property prices across our areas"), ("by_type", "Prices by property type")] \
    + [(f"sales_{pk}", f"{label} house price change across our areas") for pk, _, label, _ in PERIODS] \
    + [("lettings", "Rents across our areas")]


def town_seo_html(t, attribution, headings=None):
    out = [f"<h3>{html.escape(h.format(name=t['name']))}</h3>\n<p>{html.escape(t['text'][k])}</p>"
           for k, h in (headings or TOWN_HEADINGS) if t["text"].get(k)]
    out.append("<p><small>" + " ".join(html.escape(a) for a in attribution) + "</small></p>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- comparison and text

def best_worst(items, key):
    ranked = sorted((i for i in items if i[key] is not None), key=lambda i: i[key])
    return (ranked[-1], ranked[0]) if len(ranked) >= 2 else (None, None)


def signed(p):
    return f"{'+' if p > 0 else ''}{p:.1f}%"


def moved(p, up="rose", down="fell"):
    if p is None:
        return None
    if abs(p) < 0.05:
        return "was unchanged"
    return f"{up if p > 0 else down} {abs(p):.1f}%"


def district_name(d):
    return f"{d['id']} ({d['place']})" if d.get("place") else d["id"]


def build_text(name, hpi, rent, districts, boroughs, yield_pct, total_sales, end):
    t = {}
    if hpi:
        lead = (f"The average property price in {name} was {money(hpi['average_price'])} in "
                f"{month_name(hpi['latest_month'])}, according to the UK House Price Index")
        ch = describe_change(hpi["annual_change_pct"])
        lead += f", {ch} on a year earlier." if ch and ch != "unchanged" else "."
        if rent:
            lead += (f" Private rents averaged {money(rent['rent'])} a month in {month_name(rent['latest_month'])}"
                     + (f", {describe_change(rent['annual_change_pct'])} on a year earlier." if rent.get("annual_change_pct") is not None else "."))
        t["key_facts"] = lead
        t["current_prices"] = lead + (
            f" HM Land Registry has registered {total_sales:,} sales in {name} in the 12 months to {month_name(end)}."
            if total_sales else "")

    priced = [d for d in districts if d["median_12m"]]
    hi, lo = best_worst(priced, "median_12m")
    if hi:
        t["best_areas"] = (
            f"Over the 12 months to {month_name(end)}, {district_name(hi)} had the highest median sale price of "
            f"{name}'s postcode districts at {money(hi['median_12m'])}, and {district_name(lo)} the lowest at "
            f"{money(lo['median_12m'])}.")

    if rent:
        beds = rent["by_bedrooms"]
        parts = [f"{money(v)} for {label}" for label, v in (
            ("one bedroom", beds.get("one_bed")), ("two bedrooms", beds.get("two_bed")),
            ("three bedrooms", beds.get("three_bed")), ("four or more", beds.get("four_plus_bed"))) if v]
        line = f"The average monthly rent in {name} is {money(rent['rent'])}"
        if parts:
            line += ": " + ", ".join(parts[:-1]) + f" and {parts[-1]}"
        line += "."
        nb = [b for b in boroughs if b.get("rent")]
        if len(nb) > 1:
            dear = max(nb, key=lambda b: b["rent"]["rent"])
            cheap = min(nb, key=lambda b: b["rent"]["rent"])
            line += (f" Among {name} and its neighbouring boroughs, rents are highest in {dear['name']} "
                     f"({money(dear['rent']['rent'])}) and lowest in {cheap['name']} ({money(cheap['rent']['rent'])}).")
        t["lettings"] = line

    if yield_pct:
        t["investment"] = (
            f"Based on the average rent and the average property price, the gross rental yield in {name} is about "
            f"{yield_pct:.1f}% a year, before costs such as letting fees, maintenance and void periods.")
        others = [b for b in boroughs if not b.get("self") and b.get("gross_yield_pct")]
        if others:
            parts = [f"{b['gross_yield_pct']:.1f}% in {b['name']}" for b in others]
            t["investment"] += " For comparison, it is " + ", ".join(parts[:-1]) + f" and {parts[-1]}."

    for pk, n, label, phrase in PERIODS:
        lines = []
        if hpi and hpi["change"].get(pk) is not None:
            lines.append(f"Over the past {phrase}, the average property price in {name} {moved(hpi['change'][pk])}, "
                         f"according to the UK House Price Index.")
        best, worst = best_worst(districts and [{**d, "c": d["change"][pk]} for d in districts] or [], "c")
        if best:
            lines.append(f"Among its postcode districts, {district_name(best)} saw the strongest change in median "
                         f"sale price at {signed(best['c'])}, and {district_name(worst)} the weakest at {signed(worst['c'])}.")
        nb = [{**b, "c": b["hpi"]["change"].get(pk)} for b in boroughs if b.get("hpi")]
        b_best, b_worst = best_worst(nb, "c")
        if b_best:
            lines.append(f"Among {name} and its neighbouring boroughs, prices did best in {b_best['name']} ({signed(b_best['c'])}) "
                         f"and worst in {b_worst['name']} ({signed(b_worst['c'])}).")
        if lines:
            t[f"sales_{pk}"] = " ".join(lines)

        lines = []
        if rent and rent["change"].get(pk) is not None:
            lines.append(f"Over the past {phrase}, average private rents in {name} {moved(rent['change'][pk])}, "
                         f"according to the ONS Price Index of Private Rents.")
        nb = [{**b, "c": b["rent"]["change"].get(pk)} for b in boroughs if b.get("rent")]
        b_best, b_worst = best_worst(nb, "c")
        if b_best:
            lines.append(f"Among {name} and its neighbouring boroughs, rent growth was strongest in {b_best['name']} "
                         f"({signed(b_best['c'])}) and weakest in {b_worst['name']} ({signed(b_worst['c'])}).")
        if lines:
            t[f"lettings_{pk}"] = " ".join(lines)
    return t


TEXT_HEADINGS = [
    ("current_prices", "Current property prices in {name}"),
    ("best_areas", "Best areas to buy property in {name}"),
    ("lettings", "{name} lettings market"),
    ("investment", "{name} property investment"),
] + [(f"sales_{pk}", f"{label} house price change in {{name}}") for pk, _, label, _ in PERIODS] \
  + [(f"lettings_{pk}", f"{label} rental price change in {{name}}") for pk, _, label, _ in PERIODS]


def seo_html(name, text, attribution):
    out = []
    for key, heading in TEXT_HEADINGS:
        if text.get(key):
            out.append(f"<h3>{html.escape(heading.format(name=name))}</h3>\n<p>{html.escape(text[key])}</p>")
    out.append('<p><small>' + " ".join(html.escape(a) for a in attribution) + "</small></p>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- summary

def build_summary(name, hpi, ppd):
    s = []
    if hpi:
        change = describe_change(hpi["annual_change_pct"])
        line = (f"The average property price in {name} was {money(hpi['average_price'])} "
                f"in {month_name(hpi['latest_month'])}, according to the UK House Price Index")
        line += f", {change} on a year earlier." if change and change != "unchanged" else (
            ", unchanged on a year earlier." if change else ".")
        s.append(line)
        parts = [f"{money(v['average_price'])} for {TYPE_PHRASES[k]}"
                 for k, v in hpi["by_type"].items() if v["average_price"]]
        if len(parts) > 1:
            s.append("By property type, the average was " + ", ".join(parts[:-1]) + f" and {parts[-1]}.")

    areas = {a["id"]: a for a in ppd["areas"]}
    b = areas.get("borough")
    period = f"the 12 months to {month_name(ppd['window']['to'])}"
    if b and b["last_12_months"]["sales"]:
        line = (f"HM Land Registry has registered {b['last_12_months']['sales']:,} sales in the borough in {period}, "
                f"with a median price of {money(b['last_12_months']['median'])}")
        ch = describe_change(b["median_change_pct"])
        line += f" ({ch} on the previous 12 months)." if ch else "."
        s.append(line)

    pcs = [a for a in ppd["areas"] if a["id"] != "borough" and a["last_12_months"]["median"]]
    if pcs:
        parts = [f"{money(a['last_12_months']['median'])} in "
                 + (f"{a['id']} ({a['place']})" if a.get("place") else a["label"])
                 + f" from {a['last_12_months']['sales']:,} sales" for a in pcs]
        joined = ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0]
        s.append(f"Median sale prices over the same period were {joined}.")

    s.append("Figures are updated monthly from HM Land Registry Price Paid Data and the UK House Price Index. "
             "Recent months may be revised as late sales are registered.")
    return " ".join(s)


# ---------------------------------------------------------------- main

def build(config, ppd_rows, hpi_months, today, neighbour_hpi=None, rents=None, centroids=None, borough_map=None,
          points=None):
    """neighbour_hpi: {hpi_region: months}; rents: {ONS area name: months}; centroids: {outcode: {lat, lng}};
    borough_map: output of build_borough_map; points: output of load_postcode_points (for towns)."""
    neighbour_hpi, rents, centroids = neighbour_hpi or {}, rents or {}, centroids or {}
    name = config["name"]
    areas = config["areas"]
    std_months = [month_key(r["date"]) for r in ppd_rows if r["category"] == "A"]
    if not std_months:
        raise RuntimeError("No Price Paid rows matched the configured areas")
    latest = max(std_months)
    end = add_months(latest, -config.get("drop_latest_months", 1))

    ppd = {
        "latest_sale_month": latest,
        "window": {"from": add_months(end, -11), "to": end},
        "areas": [area_stats(a, ppd_rows, end, config.get("recent_sales", 10)) for a in areas],
    }
    n_series = config.get("hpi_months", 72)
    hpi = hpi_block(name, hpi_months, n_series) if hpi_months else None
    rent_area = config.get("rent_area", name)
    rent = rent_block(rent_area, rents[rent_area], n_series) if rents.get(rent_area) else None

    districts = []
    for d in config.get("districts", []):
        st = district_stats(d, ppd_rows, end)
        if d["id"] in centroids:
            st.update(centroids[d["id"]])
        districts.append(st)

    # The area itself plus its neighbours, for the borough comparison cards.
    boroughs = []
    for b in [{"name": name, "hpi_region": config["hpi_region"], "rent_area": rent_area}] + config.get("neighbours", []):
        months = hpi_months if b["hpi_region"] == config["hpi_region"] else neighbour_hpi.get(b["hpi_region"])
        entry = {"name": b["name"], "self": b["hpi_region"] == config["hpi_region"]}
        if months:
            entry["hpi"] = {"average_price": months[-1]["average_price"], "latest_month": months[-1]["month"],
                            "change": series_changes(months, "average_price")}
        if rents.get(b["rent_area"]):
            rm = rents[b["rent_area"]]
            entry["rent"] = {"rent": rm[-1]["rent"], "latest_month": rm[-1]["month"], "change": series_changes(rm, "index")}
        if months and entry.get("rent"):
            entry["gross_yield_pct"] = round(entry["rent"]["rent"] * 12 / entry["hpi"]["average_price"] * 100, 1)
        boroughs.append(entry)

    yield_pct = round(rent["rent"] * 12 / hpi["average_price"] * 100, 1) if hpi and rent else None
    borough_sales = next((a["last_12_months"]["sales"] for a in ppd["areas"] if a["id"] == "borough"), 0)
    text = build_text(name, hpi, rent, districts, boroughs, yield_pct, borough_sales, end)

    towns, town_map, compare = build_towns(config, ppd_rows, points or {}, end, boroughs)

    attribution = [ATTRIBUTION.format(year=today.year), ATTRIBUTION_HPI] + ([ATTRIBUTION_ONS] if rent else []) \
        + ([ATTRIBUTION_POSTCODES.format(year=today.year)] if points or centroids else [])
    return {
        "area": name,
        "generated_at": today.isoformat(timespec="seconds"),
        "valuation_url": config.get("valuation_url"),
        "hpi": hpi,
        "rent": rent,
        "gross_yield_pct": yield_pct,
        "trends": {
            pk: {"hpi": series_trend(hpi_months, "average_price", n) if hpi_months else [],
                 "rent": series_trend(rents.get(rent_area, []), "index", n)}
            for pk, n, _, _ in PERIODS
        },
        "districts": districts,
        "map": borough_map,
        "towns": towns,
        "town_map": town_map,
        "compare_text": compare,
        "boroughs": boroughs,
        "ppd": ppd,
        "text": text,
        "summary": build_summary(name, hpi, ppd),
        "attribution": attribution,
        "sources": {
            "price_paid": "https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads",
            "uk_hpi": "https://landregistry.data.gov.uk/app/ukhpi",
            "private_rents": PIPR_PAGE_URL,
            "licence": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", help="also write the summary paragraph to this text file")
    ap.add_argument("--seo-html", help="also write the section paragraphs as plain HTML to paste into the page")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    today = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    years = range(today.year - config.get("ppd_years_back", 3), today.year + 1)

    match_areas = config["areas"] + [{"match": {"outcode": d["id"]}} for d in config.get("districts", [])] \
        + [{"match": {"outcode": oc}} for oc in config.get("extra_outcodes", [])]
    rows = load_ppd(years, match_areas)
    print(f"Matched {len(rows):,} Price Paid rows", file=sys.stderr)
    hpi = load_hpi(config["hpi_region"])
    neighbours = {n["hpi_region"]: load_hpi(n["hpi_region"]) for n in config.get("neighbours", [])}
    rent_names = [config.get("rent_area", config["name"])] + [n["rent_area"] for n in config.get("neighbours", [])]
    rents = load_rents(rent_names)
    district_ids = [d["id"] for d in config.get("districts", [])]
    centroids = load_centroids(district_ids)
    points = load_postcode_points({r["postcode"] for r in rows if r["postcode"]})
    borough_map = build_borough_map(points, config.get("borough_name", config["name"]), set(district_ids))
    data = build(config, rows, hpi, today, neighbours, rents, centroids, borough_map, points)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(data["summary"] + "\n\n" + "\n".join(data["attribution"]) + "\n")
    if args.seo_html:
        with open(args.seo_html, "w", encoding="utf-8") as f:
            f.write(seo_html(config["name"], data["text"], data["attribution"]))
        # One text file per town beside it, e.g. data/hounslow-seo-isleworth.html
        for t in data["towns"]:
            with open(args.seo_html.replace(".html", f"-{t['id']}.html"), "w", encoding="utf-8") as f:
                f.write(town_seo_html(t, data["attribution"]))
        if data["compare_text"]:
            with open(args.seo_html.replace(".html", "-compare.html"), "w", encoding="utf-8") as f:
                f.write(town_seo_html({"name": "", "text": data["compare_text"]}, data["attribution"], COMPARE_HEADINGS))
    print(data["summary"], file=sys.stderr)


if __name__ == "__main__":
    main()
