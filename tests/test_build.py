"""Offline tests for build_market_data.py using made-up rows (never published)."""

import csv
import datetime as dt
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import build_market_data as b  # noqa: E402

CONFIG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config", "hounslow.json")))


def ppd_line(i, price, date, postcode, ptype="T", district="HOUNSLOW", category="A", status="A"):
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL).writerow([
        f"{{TEST-{i:04d}}}", price, f"{date} 00:00", postcode, ptype, "N", "F",
        str(i), "", "TEST STREET", "", "HOUNSLOW", district, "GREATER LONDON", category, status,
    ])
    return buf.getvalue()


def fixture_lines():
    lines, i = [], 0
    # 24 months of sales, Aug 2024 .. Jul 2026, prices rising 1,000 per month
    for n in range(24):
        month = b.add_months("2024-08", n)
        for pc, base in (("TW3 1AA", 400000), ("TW4 5BB", 450000), ("TW7 6CC", 600000)):
            i += 1
            lines.append(ppd_line(i, base + n * 1000, f"{month}-15", pc))
    # one flat in Aug 2026 (the partial latest month), and noise that must be excluded
    lines.append(ppd_line(900, 999999, "2026-08-02", "TW3 2ZZ", ptype="F"))
    lines.append(ppd_line(901, 5000000, "2026-01-10", "TW3 3ZZ", category="B"))
    lines.append(ppd_line(902, 7000000, "2026-01-10", "TW3 3ZZ", ptype="O"))
    lines.append(ppd_line(903, 123, "2026-01-10", "TW3 3ZZ", status="D"))
    lines.append(ppd_line(904, 300000, "2026-01-10", "UB1 1AA", district="EALING"))
    return lines


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.rows = list(b.parse_ppd(fixture_lines(), CONFIG["areas"]))
        hpi_payload = {"results": {"bindings": [
            {"month": {"value": f"2026-{m:02d}"}, "average_price": {"value": str(500000 + m)},
             "annual_change_pct": {"value": "2.34"}, "flat": {"value": "350000"},
             "terraced": {"value": "550000"}}
            for m in range(1, 9)
        ]}}
        self.hpi = b.parse_hpi(hpi_payload)
        self.data = b.build(CONFIG, self.rows, self.hpi, dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc))

    def area(self, id_):
        return next(a for a in self.data["ppd"]["areas"] if a["id"] == id_)

    def test_filters(self):
        ids = {r["id"] for r in self.rows}
        self.assertNotIn("TEST-0903", ids)  # deleted record
        self.assertNotIn("TEST-0904", ids)  # outside Hounslow

    def test_window_drops_partial_latest_month(self):
        self.assertEqual(self.data["ppd"]["latest_sale_month"], "2026-08")
        self.assertEqual(self.data["ppd"]["window"], {"from": "2025-08", "to": "2026-07"})

    def test_borough_counts_exclude_category_b_and_other(self):
        cur = self.area("borough")["last_12_months"]
        self.assertEqual(cur["sales"], 36)
        self.assertLess(cur["mean"], 1000000)
        self.assertEqual(self.area("borough")["sales_change_pct"], 0.0)

    def test_postcode_change(self):
        tw3 = self.area("TW3")
        self.assertEqual(tw3["last_12_months"]["median"], 417500)
        self.assertEqual(tw3["previous_12_months"]["median"], 405500)
        self.assertEqual(tw3["median_change_pct"], 3.0)
        self.assertEqual(self.area("TW5")["last_12_months"]["sales"], 0)

    def test_recent_sales_include_latest_month(self):
        self.assertEqual(self.area("TW3")["recent_sales"][0]["price"], 999999)
        self.assertEqual(self.area("TW3")["recent_sales"][0]["type"], "Flat / maisonette")

    def test_hpi_and_summary(self):
        hpi = self.data["hpi"]
        self.assertEqual(hpi["latest_month"], "2026-08")
        self.assertEqual(hpi["annual_change_pct"], 2.3)
        s = self.data["summary"]
        self.assertIn("£500,008 in August 2026", s)
        self.assertIn("up 2.3% on a year earlier", s)
        self.assertIn("£350,000 for flats and maisonettes", s)
        self.assertIn("36 sales in the borough", s)
        self.assertNotIn("TW5", s)
        self.assertIn("Open Government Licence v3.0", self.data["attribution"][0])

    def test_district_period_changes(self):
        tw3 = next(d for d in b.build(CONFIG, self.rows, self.hpi, dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc))["districts"]
                   if d["id"] == "TW3")
        # 12 sales a year is below the 20-sale minimum, so no median is published
        self.assertEqual(tw3["sales_12m"], 12)
        self.assertIsNone(tw3["median_12m"])

    def test_series_changes_and_trend(self):
        pts = [{"month": b.add_months("2021-01", i), "v": 100 + i} for i in range(61)]
        ch = b.series_changes(pts, "v")
        self.assertEqual(ch["3m"], round((160 - 157) / 157 * 100, 1))
        self.assertEqual(ch["5y"], 60.0)
        trend = b.series_trend(pts, "v", 12)
        self.assertEqual(len(trend), 13)
        self.assertEqual(trend[0]["pct"], 0.0)

    def test_parse_pipr(self):
        header = ("Time period", "Area code", "Area name", "Region or country name", "Index", "Monthly change",
                  "Annual change", "Rental price", "Rental price one bed")
        rows = [("Price Index of Private Rents",), header,
                (dt.datetime(2026, 8, 1), "E09000018", "Hounslow", "London", 133.2, 0.3, 3.2, 1952, 1583),
                (dt.datetime(2026, 9, 1), "E09000018", "Hounslow", "London", 133.9, 0.5, 3.6, 1962, "[x]"),
                (dt.datetime(2026, 9, 1), "E09000009", "Ealing", "London", 130.0, 0.5, 3.8, 2126, 1700)]
        out = b.parse_pipr_rows(rows, ["Hounslow"])
        self.assertEqual([m["rent"] for m in out["Hounslow"]], [1952, 1962])
        self.assertNotIn("one_bed", out["Hounslow"][1])  # "[x]" means not available

    def test_text_with_rents_and_neighbours(self):
        rents = {"Hounslow": [{"month": b.add_months("2021-09", i), "index": 100 + i, "rent": 1500 + i * 5,
                               "annual_change_pct": 3.6, "one_bed": 1500} for i in range(61)]}
        cfg = dict(CONFIG, neighbours=[{"name": "Ealing", "hpi_region": "ealing", "rent_area": "Ealing"}])
        data = b.build(cfg, self.rows, self.hpi, dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc),
                       {"ealing": self.hpi}, rents, {})
        self.assertEqual(data["rent"]["rent"], 1800)
        self.assertEqual(data["gross_yield_pct"], round(1800 * 12 / 500008 * 100, 1))
        self.assertIn("Private rents averaged £1,800 a month in September 2026", data["text"]["key_facts"])
        self.assertIn("gross rental yield", data["text"]["investment"])
        self.assertIn("ONS Price Index of Private Rents", data["text"]["lettings_1y"])
        self.assertIn("Price Index of Private Rents", data["attribution"][-1])
        html = b.seo_html("Hounslow", data["text"], data["attribution"])
        self.assertIn("<h3>Hounslow property investment</h3>", html)

    def test_borough_map(self):
        # Two districts side by side on a 2 km grid of postcodes, plus one postcode in another borough.
        pts = {}
        for i in range(10):
            for j in range(10):
                oc = "TW3" if i < 5 else "TW4"
                pts[f"{oc} {i}{j}A"] = (510000 + i * 200, 175000 + j * 200, "Hounslow")
        pts["TW3 9ZZ"] = (509000, 175000, "Ealing")
        m = b.build_borough_map(pts, "Hounslow", {"TW3", "TW4"})
        self.assertEqual(sorted(m["districts"]), ["TW3", "TW4"])
        self.assertTrue(m["outline"].startswith("M"))
        self.assertLess(m["labels"]["TW3"][0], m["labels"]["TW4"][0])  # TW3 is to the west
        self.assertIsNone(b.build_borough_map({}, "Hounslow", {"TW3"}))

    def test_towns_from_wards(self):
        cfg = dict(CONFIG, towns=[{"id": "east", "name": "East", "wards": ["East Ward"]},
                                  {"id": "west", "name": "West", "wards": ["West Ward"]}])
        points = {}
        for r in self.rows:
            ward = "East Ward" if r["outcode"] == "TW7" else "West Ward"
            points[r["postcode"]] = (510000 + len(points) * 50, 175000, "Hounslow", ward)
        data = b.build(cfg, self.rows, self.hpi, dt.datetime(2026, 10, 22, tzinfo=dt.timezone.utc), points=points)
        east, west = data["towns"]
        self.assertEqual(east["sales_12m"], 12)   # TW7 only: one sale a month
        self.assertEqual(west["sales_12m"], 24)   # TW3 and TW4
        self.assertEqual(west["median_12m"], 442500)
        self.assertIsNone(east["median_12m"])     # below the 20-sale minimum, so no figures or text
        self.assertEqual(east["text"], {})
        self.assertIn("West is a small area", west["text"]["key_facts"])
        html = b.town_seo_html(west, data["attribution"])
        self.assertIn("<h3>West property prices</h3>", html)

    def test_hpi_query_names_region(self):
        self.assertIn("/id/region/hounslow>", b.hpi_query("hounslow"))


if __name__ == "__main__":
    unittest.main()
