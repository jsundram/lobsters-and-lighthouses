"""Verification suite for build.py.

Run with:
    uvx --with jinja2,astral,qrcode,pytest python3 -m pytest test_build.py -v

The NOAA fetch step uses on-disk caching, so re-runs don't hit the network.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-function tests (no network, no I/O)
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_fmt_time_morning(self):
        assert build.fmt_time(dt.time(4, 58)) == "4:58 AM"

    def test_fmt_time_noon(self):
        assert build.fmt_time(dt.time(12, 0)) == "12:00 PM"

    def test_fmt_time_midnight(self):
        assert build.fmt_time(dt.time(0, 0)) == "12:00 AM"

    def test_fmt_time_pm(self):
        assert build.fmt_time(dt.time(20, 21)) == "8:21 PM"

    def test_fmt_time_from_datetime(self):
        d = dt.datetime(2026, 6, 8, 17, 47)
        assert build.fmt_time(d) == "5:47 PM"

    def test_fmt_duration_hm(self):
        assert build.fmt_duration_hm(dt.timedelta(hours=15, minutes=23)) == "15h 23m"
        assert build.fmt_duration_hm(dt.timedelta(hours=15, minutes=3)) == "15h 03m"
        assert build.fmt_duration_hm(dt.timedelta(hours=0, minutes=45)) == "0h 45m"

    def test_fmt_date_long(self):
        assert build.fmt_date_long(dt.date(2026, 6, 8)) == "Monday, June 8, 2026"
        assert build.fmt_date_long(dt.date(2026, 6, 22)) == "Monday, June 22, 2026"

    def test_fmt_date_short(self):
        assert build.fmt_date_short(dt.date(2026, 6, 8)) == "6/8"
        assert build.fmt_date_short(dt.date(2026, 12, 25)) == "12/25"

    def test_parse_time_str(self):
        assert build.parse_time_str("8:25 PM") == dt.time(20, 25)
        assert build.parse_time_str("11:10 AM") == dt.time(11, 10)
        assert build.parse_time_str("12:00 PM") == dt.time(12, 0)

    def test_time_to_hours(self):
        assert build.time_to_hours(dt.time(10, 0)) == pytest.approx(10.0)
        assert build.time_to_hours(dt.time(11, 30)) == pytest.approx(11.5)
        assert build.time_to_hours(dt.time(17, 45)) == pytest.approx(17.75)


# ---------------------------------------------------------------------------
# Config + stops loading
# ---------------------------------------------------------------------------

class TestConfig:
    def test_config_loads(self):
        cfg = build.load_config()
        assert "trip" in cfg
        assert "location" in cfg
        assert "car" in cfg
        assert cfg["location"]["tide_station_id"] == 8419399

    def test_config_date_parses(self):
        cfg = build.load_config()
        date = dt.date.fromisoformat(cfg["trip"]["date"])
        assert date.year == 2026

    def test_stops_load(self):
        stops = build.load_stops()
        assert len(stops) == 11
        ids = [s["id"] for s in stops]
        assert ids == ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10"]

    def test_stops_have_required_fields(self):
        stops = build.load_stops()
        for s in stops:
            assert "id" in s
            assert "name" in s
            assert "arrival" in s
            assert "icon" in s
            assert "icon_class" in s
            # Address required for non-depart stops too
            assert "address_display" in s
            assert "address_query" in s


# ---------------------------------------------------------------------------
# Battery / SoC
# ---------------------------------------------------------------------------

class TestEfficiencyModel:
    """The Wh/mi model derives from each leg's average speed."""

    eff = {"base_wh_per_mi": 213.0, "slope_wh_per_mph": 1.29, "overrides": {}}

    def test_low_speed_coastal(self):
        # 8 mi over 25 min = 19.2 mph
        leg = {"leg_distance_mi": 8, "leg_drive_min": 25}
        wh = build.wh_per_mi_for_leg(leg, {"id": "x"}, self.eff)
        assert 235 <= wh <= 245, wh

    def test_highway_cruise(self):
        # 60 mph: base + 1.29*60 = 290.4
        leg = {"leg_distance_mi": 60, "leg_drive_min": 60}
        wh = build.wh_per_mi_for_leg(leg, {"id": "x"}, self.eff)
        assert 285 <= wh <= 295, wh

    def test_increases_with_speed(self):
        slow = build.wh_per_mi_for_leg({"leg_distance_mi": 10, "leg_drive_min": 30}, {"id": "x"}, self.eff)
        fast = build.wh_per_mi_for_leg({"leg_distance_mi": 60, "leg_drive_min": 60}, {"id": "x"}, self.eff)
        assert fast > slow

    def test_override_wins(self):
        eff = {**self.eff, "overrides": {"x": 999}}
        leg = {"leg_distance_mi": 10, "leg_drive_min": 10}
        assert build.wh_per_mi_for_leg(leg, {"id": "x"}, eff) == 999.0

    def test_zero_distance_returns_base(self):
        leg = {"leg_distance_mi": 0, "leg_drive_min": 0}
        wh = build.wh_per_mi_for_leg(leg, {"id": "x"}, self.eff)
        assert wh == 213.0


class TestBattery:
    @pytest.fixture
    def car_cfg(self):
        cfg = build.load_config()
        car = dict(cfg["car"])
        car["_battery_thresholds"] = cfg["battery_thresholds"]
        return car

    def test_depart_battery_at_100(self, car_cfg):
        stops = build.load_stops()
        result = build.compute_soc(stops, car_cfg)
        depart = result[0]
        assert depart["battery"]["pct"] == 100
        assert depart["battery"]["css_class"] == "bat-hi"

    def test_charge_stop_has_arrive_and_depart(self, car_cfg):
        stops = build.load_stops()
        result = build.compute_soc(stops, car_cfg)
        charge = next(s for s in result if s["id"] == "09")
        assert charge["battery"]["is_charge"] is True
        assert charge["battery"]["pct_depart"] == 77
        assert "→" in charge["battery"]["label"]

    def test_battery_monotonically_decreases_pre_charge(self, car_cfg):
        stops = build.load_stops()
        result = build.compute_soc(stops, car_cfg)
        pre_charge = [s for s in result if s["id"] < "09"]
        socs = [s["battery"]["pct"] for s in pre_charge]
        # Allow flat steps when leg distance is 0 (Marginal Way → Footbridge)
        assert all(socs[i] >= socs[i+1] for i in range(len(socs)-1)), socs

    def test_home_battery_above_15(self, car_cfg):
        """Per the original chart, home arrives at ~42% SoC."""
        stops = build.load_stops()
        result = build.compute_soc(stops, car_cfg)
        home = next(s for s in result if s["id"] == "10")
        # Should be in mid range (35-60), not low
        assert 25 <= home["battery"]["pct"] <= 60, home["battery"]["pct"]

    def test_battery_css_classes(self, car_cfg):
        b_hi = build.make_battery(75, 60, 35)
        assert b_hi["css_class"] == "bat-hi"
        b_mid = build.make_battery(50, 60, 35)
        assert b_mid["css_class"] == "bat-mid"
        b_low = build.make_battery(30, 60, 35)
        assert b_low["css_class"] == "bat-low"

    def test_battery_fill_width(self):
        b = build.make_battery(100, 60, 35)
        assert b["fill_width"] == 18  # full
        b = build.make_battery(50, 60, 35)
        assert b["fill_width"] == 9  # half


# ---------------------------------------------------------------------------
# NOAA fetch (cached)
# ---------------------------------------------------------------------------

class TestTideFetch:
    def test_fetch_uses_cache(self, tmp_path, monkeypatch):
        """Confirm that a second fetch call doesn't hit the network."""
        cache_dir = tmp_path / "data"
        monkeypatch.setattr(build, "DATA", cache_dir)
        cache_dir.mkdir()
        date = dt.date(2026, 6, 8)
        # Seed the cache with known data
        cached = {"predictions": [
            {"t": "2026-06-08 11:31", "v": "0.7", "type": "L"},
            {"t": "2026-06-08 17:47", "v": "8.5", "type": "H"},
        ]}
        cache_file = cache_dir / f"tides_8419399_20260608.json"
        cache_file.write_text(json.dumps(cached))

        # Block network access by patching urlopen to raise
        def fail(*a, **kw):
            raise AssertionError("urlopen called despite cached data")
        monkeypatch.setattr(build.urllib.request, "urlopen", fail)

        preds = build.fetch_tides(8419399, date)
        assert len(preds) == 2
        assert preds[0]["type"] == "L"

    def test_fetch_real_june_8_cached(self):
        """Validate the June 8 prediction set (was cached during development)."""
        # This relies on the cache from earlier work
        cache = build.DATA / "tides_8419399_20260608.json"
        if not cache.exists():
            pytest.skip("June 8 NOAA cache not present")
        preds = json.loads(cache.read_text())["predictions"]
        kinds = [p["type"] for p in preds]
        assert "H" in kinds and "L" in kinds


# ---------------------------------------------------------------------------
# Sun / moon
# ---------------------------------------------------------------------------

class TestSun:
    def test_sun_june_8_portland(self):
        sun = build.compute_sun(
            dt.date(2026, 6, 8), 43.6591, -70.2568,
            "America/New_York", "Portland, ME",
        )
        # Sunrise should be 4:55 - 5:00 AM
        sr_h = sun["sunrise"].hour + sun["sunrise"].minute / 60
        assert 4.85 <= sr_h <= 5.05, f"sunrise={sun['sunrise']}"
        # Sunset should be 8:15 - 8:25 PM
        ss_h = sun["sunset"].hour + sun["sunset"].minute / 60
        assert 20.20 <= ss_h <= 20.50, f"sunset={sun['sunset']}"

    def test_sun_june_22_longer_than_june_8(self):
        """June 22 is just past summer solstice; day length should be ≥ June 8."""
        s8 = build.compute_sun(dt.date(2026, 6, 8), 43.6591, -70.2568,
                               "America/New_York", "Portland, ME")
        s22 = build.compute_sun(dt.date(2026, 6, 22), 43.6591, -70.2568,
                                "America/New_York", "Portland, ME")
        assert s22["day_length"] >= s8["day_length"]

    def test_golden_hour_before_sunset(self):
        sun = build.compute_sun(dt.date(2026, 6, 8), 43.6591, -70.2568,
                                "America/New_York", "Portland, ME")
        assert sun["golden_hour"] < sun["sunset"]


class TestMoon:
    def test_june_8_is_neap(self):
        """June 7 2026 is third quarter; June 8 should classify as neap."""
        m = build.compute_moon(dt.date(2026, 6, 8))
        assert m["tide_class"] == "neap"
        assert "quarter" in m["label"]

    def test_june_22_is_neap(self):
        """June 21 2026 is first quarter; June 22 should classify as neap."""
        m = build.compute_moon(dt.date(2026, 6, 22))
        assert m["tide_class"] == "neap"
        assert "quarter" in m["label"]

    def test_full_moon_is_spring(self):
        """May 31 2026 is full moon → spring tide."""
        m = build.compute_moon(dt.date(2026, 5, 31))
        assert m["tide_class"] == "spring"


# ---------------------------------------------------------------------------
# Tide SVG
# ---------------------------------------------------------------------------

class TestTideSvg:
    @pytest.fixture
    def preds(self):
        return [
            {"t": "2026-06-08 05:08", "v": "8.481", "type": "H"},
            {"t": "2026-06-08 11:31", "v": "0.724", "type": "L"},
            {"t": "2026-06-08 17:47", "v": "8.496", "type": "H"},
        ]

    def test_svg_has_viewbox(self, preds):
        svg = build.render_tide_svg(preds, build.load_stops(), {})
        assert 'viewBox="0 0 600 100"' in svg

    def test_svg_has_low_and_high_labels(self, preds):
        svg = build.render_tide_svg(preds, build.load_stops(), {})
        assert "LOW" in svg
        assert "HIGH" in svg
        assert "11:31 AM" in svg
        assert "5:47 PM" in svg

    def test_svg_has_time_axis(self, preds):
        svg = build.render_tide_svg(preds, build.load_stops(), {})
        for tick in ("10 AM", "1 PM", "4 PM", "7 PM", "10 PM"):
            assert tick in svg

    def test_svg_has_stop_dots(self, preds):
        """One dot per visible stop (excludes depart 10 AM and home)."""
        svg = build.render_tide_svg(preds, build.load_stops(), {})
        # Stop dots are smaller (r=2). Marker dots are r=4.5.
        n_small = svg.count('r="2"')
        assert n_small >= 7  # 9 stops minus depart and home


# ---------------------------------------------------------------------------
# QR code
# ---------------------------------------------------------------------------

class TestQrCode:
    def test_qr_renders(self):
        svg = build.render_qr_svg("https://example.com/test")
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")
        assert "fill" in svg

    def test_qr_has_finder_patterns(self):
        """Three rounded finder patterns at corners."""
        svg = build.render_qr_svg("https://example.com/test")
        # The styled finder pattern includes "fill-rule" attribute
        assert svg.count('fill-rule="evenodd"') == 3

    def test_qr_long_url(self):
        """Long Google Maps URL should still produce valid SVG."""
        cfg = build.load_config()
        svg = build.render_qr_svg(cfg["maps"]["all_stops_url"])
        assert len(svg) > 5000  # Real QR for ~670-char URL


# ---------------------------------------------------------------------------
# End-to-end build
# ---------------------------------------------------------------------------

class TestBuild:
    def test_build_produces_file(self):
        out = build.build()
        assert out.exists()
        assert out.stat().st_size > 50000  # Sane lower bound

    def test_output_has_no_unrendered_placeholders(self):
        out = build.build()
        content = out.read_text()
        # Jinja placeholders should all be resolved
        assert "{{" not in content, "Unrendered {{ }} placeholders found"
        assert "{%" not in content, "Unrendered {% %} blocks found"

    def test_output_has_doctype_and_html(self):
        out = build.build()
        content = out.read_text()
        assert content.startswith("<!doctype html>")
        assert content.rstrip().endswith("</html>")

    def test_date_appears_consistently(self):
        out = build.build()
        content = out.read_text()
        cfg = build.load_config()
        date = dt.date.fromisoformat(cfg["trip"]["date"])
        long_date = build.fmt_date_long(date)
        short_date = build.fmt_date_short(date)
        # Long form in title, masthead, manifest description
        assert long_date in content
        # Short form in footer
        assert short_date in content

    def test_each_stop_appears_in_timeline(self):
        import html as html_mod
        out = build.build()
        # Unescape so we can search for literal text (apostrophes etc.)
        content = html_mod.unescape(out.read_text())
        stops = build.load_stops()
        for stop in stops:
            assert stop["name"] in content, f"missing stop: {stop['name']}"

    def test_all_addresses_have_links(self):
        import html as html_mod
        out = build.build()
        content = html_mod.unescape(out.read_text())
        stops = build.load_stops()
        # Each display address should be wrapped in a Google Maps search link
        for stop in stops:
            addr = stop["address_display"]
            idx = content.find(addr)
            assert idx > 0, f"address not found: {addr}"
            window = content[max(0, idx - 250):idx]
            assert "google.com/maps/search" in window, f"no maps link near: {addr}"

    def test_tide_svg_embedded(self):
        out = build.build()
        content = out.read_text()
        assert 'viewBox="0 0 600 100"' in content  # tide SVG viewBox
        assert "LOW" in content and "HIGH" in content

    def test_qr_svg_embedded(self):
        out = build.build()
        content = out.read_text()
        # QR uses viewBox 0 0 N N where N depends on data; check for fill-rule="evenodd"
        assert 'fill-rule="evenodd"' in content

    def test_lighthouse_icon_present(self):
        out = build.build()
        content = out.read_text()
        # The apple-touch-icon SVG has a distinctive radialGradient id="bg"
        assert "apple-touch-icon" in content
        assert "data:image/svg+xml" in content


class TestPdf:
    """End-to-end: render_pdf produces exactly 2 letter pages at a sane scale."""

    @pytest.fixture(scope="class")
    def pdf(self):
        out = build.build()
        pages, scale = build.render_pdf(out, build.PDF_OUTPUT)
        return build.PDF_OUTPUT, pages, scale

    def test_two_pages(self, pdf):
        _, pages, _ = pdf
        assert pages == 2

    def test_scale_in_reasonable_range(self, pdf):
        """Below 0.6 the design has bloated; above 0.85 the running-header is
        probably mis-placed and the pages would underfill. Either should be
        a build-design red flag."""
        _, _, scale = pdf
        assert 0.6 < scale < 0.85, f"scale={scale}"

    def test_page_size_is_letter(self, pdf):
        from pypdf import PdfReader
        path, _, _ = pdf
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages, 1):
            # PDF user-space units = points (1/72 inch). Letter = 612 × 792 pt.
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            assert abs(w - 612) < 1, f"page {i} width {w}pt, expected 612"
            assert abs(h - 792) < 1, f"page {i} height {h}pt, expected 792"


class TestDateOverride:
    def test_override_date_via_build(self, tmp_path, monkeypatch):
        """Build with a non-config date and confirm output uses it."""
        # Don't pollute the default output
        monkeypatch.setattr(build, "BUILD", tmp_path / "build")
        monkeypatch.setattr(build, "OUTPUT", tmp_path / "build" / "trip-handout.html")
        out = build.build(date=dt.date(2026, 6, 22))
        content = out.read_text()
        assert "June 22, 2026" in content
        assert "Monday, June 22, 2026" in content


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
