"""Build build/index.html from config.toml + stops.toml + live NOAA data.

Run with:
    uvx --with jinja2,astral,qrcode python3 build.py

Outputs build/index.html (and build/trip-handout.pdf). NOAA tide responses
are cached in cache/ so subsequent builds for the same date don't re-fetch.
The HTML is named index.html so Netlify serves it from the site root.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
CACHE = ROOT / "cache"          # regenerable: NOAA tide fetches
FONTS = ROOT / "fonts"          # pinned vendored assets: JetBrains Mono TTFs
TEMPLATE = ROOT / "template.html.j2"
OUTPUT = BUILD / "index.html"


# ---------------------------------------------------------------------------
# Config + stops loading
# ---------------------------------------------------------------------------

def load_config(path: Path = ROOT / "config.toml") -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_stops(path: Path = ROOT / "stops.toml") -> list[dict[str, Any]]:
    with open(path, "rb") as f:
        return tomllib.load(f)["stops"]


# ---------------------------------------------------------------------------
# NOAA tide predictions (cached on disk)
# ---------------------------------------------------------------------------

NOAA_URL = (
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    "?begin_date={d}&end_date={d}&station={s}&product=predictions"
    "&datum=MLLW&interval=hilo&units=english&time_zone=lst_ldt&format=json"
)


def fetch_tides(station_id: int, date: dt.date, *, force: bool = False) -> list[dict]:
    """Return list of {t: 'YYYY-MM-DD HH:MM', v: '8.5', type: 'H'|'L'}.

    Cached in cache/tides_<station>_<YYYYMMDD>.json. Pass force=True to refetch.
    """
    CACHE.mkdir(exist_ok=True)
    cache_file = CACHE / f"tides_{station_id}_{date:%Y%m%d}.json"
    if cache_file.exists() and not force:
        return json.loads(cache_file.read_text())["predictions"]
    url = NOAA_URL.format(s=station_id, d=date.strftime("%Y%m%d"))
    with urllib.request.urlopen(url, timeout=15) as resp:
        body = resp.read().decode()
    payload = json.loads(body)
    if "predictions" not in payload:
        raise RuntimeError(f"NOAA response missing predictions: {payload}")
    cache_file.write_text(json.dumps(payload, indent=2))
    return payload["predictions"]


# ---------------------------------------------------------------------------
# Webfont embedding (so Chromium's PDF backend doesn't fall back on SVG text)
# ---------------------------------------------------------------------------

# JetBrains Mono TTFs are checked into fonts/ so the build has no runtime
# webfont dependency. We embed them as base64 @font-face because Chromium's
# PDF generator inconsistently resolves loaded webfonts for inline-SVG <text>
# elements — even when CSS computed-style says the right thing, the PDF
# renderer can fall back to a system monospace. Embedding the font bytes
# sidesteps that entirely.
JETBRAINS_MONO_TTFS = {
    500: "JetBrainsMono-Medium.ttf",
    700: "JetBrainsMono-Bold.ttf",
}


def get_jetbrains_mono_css() -> str:
    """Return @font-face CSS with JetBrains Mono 500 and 700 base64-embedded
    from fonts/*.ttf."""
    import base64
    chunks = []
    for weight, fname in JETBRAINS_MONO_TTFS.items():
        ttf_bytes = (FONTS / fname).read_bytes()
        b64 = base64.b64encode(ttf_bytes).decode("ascii")
        chunks.append(
            "@font-face {\n"
            "  font-family: 'JetBrains Mono';\n"
            "  font-style: normal;\n"
            f"  font-weight: {weight};\n"
            f"  src: url(data:font/ttf;base64,{b64}) format('truetype');\n"
            "}"
        )
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Sun
# ---------------------------------------------------------------------------

def compute_sun(date: dt.date, lat: float, lng: float, tz: str, city: str = "") -> dict:
    """Return sunrise, solar_noon, golden_hour_start, sunset, civil_dusk, day_length.

    All times are timezone-aware datetimes in the requested tz. day_length is a timedelta.
    """
    from astral import LocationInfo
    from astral.sun import sun, golden_hour, SunDirection

    loc = LocationInfo(city or "City", "USA", tz, lat, lng)
    s = sun(loc.observer, date=date, tzinfo=loc.timezone)
    gh = golden_hour(loc.observer, date=date, direction=SunDirection.SETTING, tzinfo=loc.timezone)
    return {
        "sunrise": s["sunrise"],
        "solar_noon": s["noon"],
        "golden_hour": gh[0],  # start of evening golden hour
        "sunset": s["sunset"],
        "civil_dusk": s["dusk"],
        "day_length": s["sunset"] - s["sunrise"],
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def fmt_time(t: dt.datetime | dt.time) -> str:
    """Format like '8:21 PM' with no leading zero on hour."""
    if isinstance(t, dt.datetime):
        t = t.time()
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def fmt_duration_hm(td: dt.timedelta) -> str:
    """Format a timedelta like '15h 23m'."""
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


def fmt_date_long(date: dt.date) -> str:
    """E.g. 'Monday, June 8, 2026'."""
    return date.strftime("%A, %B ") + str(date.day) + date.strftime(", %Y")


def fmt_date_short(date: dt.date) -> str:
    """E.g. '6/8'."""
    return f"{date.month}/{date.day}"


def parse_time_str(s: str) -> dt.time:
    """Parse '8:25 PM' or '11:10 AM' to datetime.time."""
    return dt.datetime.strptime(s.strip(), "%I:%M %p").time()


def time_to_hours(t: dt.time) -> float:
    """Convert datetime.time to float hours since midnight."""
    return t.hour + t.minute / 60 + t.second / 3600


# ---------------------------------------------------------------------------
# Per-stop SoC computation
# ---------------------------------------------------------------------------

def wh_per_mi_for_leg(stop: dict, next_stop: dict, efficiency: dict) -> float:
    """Wh/mi for the leg leaving `stop` toward `next_stop`.

    Looks up an override (keyed by next_stop id) if present; otherwise computes
    Wh/mi from the leg's average speed using the linear model in config.

    A zero-distance "leg" (e.g. Marginal Way → Footbridge, same lot) returns
    the model's intercept — but no energy is used either way since miles=0.
    """
    overrides = efficiency.get("overrides", {}) or {}
    if next_stop["id"] in overrides:
        return float(overrides[next_stop["id"]])
    miles = float(stop.get("leg_distance_mi", 0) or 0)
    minutes = float(stop.get("leg_drive_min", 0) or 0)
    if miles <= 0 or minutes <= 0:
        return float(efficiency["base_wh_per_mi"])
    avg_mph = miles / (minutes / 60)
    return float(efficiency["base_wh_per_mi"]) + float(efficiency["slope_wh_per_mph"]) * avg_mph


def compute_soc(stops: list[dict], car_cfg: dict) -> list[dict]:
    """Return list of stops with `battery` field added.

    Each stop's `leg_distance_mi` is the leg *out* (to the next stop). The
    walking algorithm: arrive at stop -> compute battery -> drive the leg out.
    The Tesla supercharge swaps soc from arrival% to target% before the next leg.
    """
    battery_kwh = car_cfg["battery_kwh"]
    start_pct = car_cfg["start_soc_pct"]
    charge_stop_id = car_cfg["charge_stop_id"]
    charge_target = car_cfg["charge_target_pct"]
    efficiency = car_cfg["efficiency"]
    hi_min = car_cfg["_battery_thresholds"]["hi_min"]
    mid_min = car_cfg["_battery_thresholds"]["mid_min"]

    soc = start_pct
    enriched: list[dict] = []
    for i, stop in enumerate(stops):
        # Battery state at THIS stop on arrival
        if stop["id"] == charge_stop_id:
            pct_arrive = round(soc)
            soc = charge_target  # plug in, top up
            battery = make_battery(pct_arrive, hi_min, mid_min, pct_depart=charge_target)
        else:
            battery = make_battery(soc, hi_min, mid_min)
        enriched.append({**stop, "battery": battery, "_soc_arrive": soc})

        # Drive the leg out of this stop to the next, if any
        leg_mi = stop.get("leg_distance_mi")
        if leg_mi is None or i + 1 >= len(stops):
            continue
        next_stop = stops[i + 1]
        wh_per_mi = wh_per_mi_for_leg(stop, next_stop, efficiency)
        kwh_used = leg_mi * wh_per_mi / 1000
        pct_used = (kwh_used / battery_kwh) * 100
        soc = max(0.0, soc - pct_used)

    return enriched


def make_battery(pct: float, hi_min: int, mid_min: int, *, pct_depart: float | None = None) -> dict:
    """Build the battery dict with CSS class and SVG fill width."""
    p = round(pct)
    css_class = "bat-hi" if p >= hi_min else "bat-mid" if p >= mid_min else "bat-low"
    # The battery SVG body width is 20.8, fill from x=2 maxing at width 18
    fill_width = round(min(p, 100) / 100 * 18, 2)
    if pct_depart is not None:
        return {
            "pct": p,
            "pct_arrive": p,
            "pct_depart": round(pct_depart),
            "label": f"{p}→{round(pct_depart)}%",
            "css_class": css_class,
            "fill_width": fill_width,
            "is_charge": True,
        }
    label = f"{p}%" if pct < 100 else "100%"
    return {
        "pct": p,
        "label": label,
        "css_class": css_class,
        "fill_width": fill_width,
        "is_charge": False,
    }


# ---------------------------------------------------------------------------
# Tide SVG renderer
# ---------------------------------------------------------------------------

def render_tide_svg(predictions: list[dict], stops: list[dict], cfg: dict) -> str:
    """Render the tide curve as a self-contained SVG string.

    `predictions` is the NOAA list. `stops` provides the per-stop times for the
    little dots along the curve. `cfg` controls dimensions.
    """
    events = [_parse_event(p) for p in predictions]
    # Trip window: 10 AM (10.0) to 10 PM (22.0) in local hours.
    t_start, t_end = cfg.get("t_start", 10.0), cfg.get("t_end", 22.0)
    height_fn = _build_height_function(events)

    W = cfg.get("width", 600)
    H = cfg.get("height", 100)
    PAD_L = cfg.get("pad_l", 22)
    PAD_R = cfg.get("pad_r", 22)
    PAD_T = cfg.get("pad_t", 14)
    PAD_B = cfg.get("pad_b", 22)
    CW = W - PAD_L - PAD_R
    CH = H - PAD_T - PAD_B

    # Y axis: 0 ft at baseline, max at top
    h_axis_max = max(9.5, max(e["height"] for e in events) + 1)

    def xof(t: float) -> float:
        return PAD_L + (t - t_start) / (t_end - t_start) * CW

    def yof(h: float) -> float:
        return PAD_T + (1 - h / h_axis_max) * CH

    # Curve sample points
    points: list[tuple[float, float]] = []
    t = t_start
    while t <= t_end + 1e-6:
        points.append((xof(t), yof(height_fn(t))))
        t += 0.05

    baseline_y = PAD_T + CH

    def fmt(v: float) -> str:
        return f"{v:.1f}"

    curve_d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    fill_d = (
        curve_d
        + f" L{points[-1][0]:.1f},{baseline_y:.1f}"
        + f" L{points[0][0]:.1f},{baseline_y:.1f} Z"
    )

    # Mark each event that falls within the trip window
    visible_events = [
        e for e in events if t_start <= e["t_hours"] <= t_end
    ]

    # Stop dots
    stop_dots = ""
    for s in stops:
        try:
            t_h = time_to_hours(parse_time_str(s["arrival"]))
        except Exception:
            continue
        if not (t_start <= t_h <= t_end):
            continue
        if s["id"] in {"00", "10"}:  # skip depart/home — they're at boundary, distracting
            continue
        stop_dots += (
            f'<circle cx="{fmt(xof(t_h))}" cy="{fmt(yof(height_fn(t_h)))}" '
            f'r="2" fill="#1c3a5e" opacity="0.7"/>'
        )

    # Text labels are native SVG <text>. JetBrains Mono is base64-embedded
    # via @font-face in the page <style> so Chromium's PDF backend picks
    # it up reliably here. Font-sizes are in viewBox user units; the SVG
    # then scales with its container — so we pick values that *after* scaling
    # land near daylight's 13px values / 8.5px labels.
    # Empirically: SVG renders at ~0.9× scale in print, so 11 user units →
    # ~10 print px; 7 user units → ~6.3 print px. That visually matches the
    # daylight 13px / 8.5px CSS sizes after accounting for the page-level
    # PDF scale (~0.7).

    # Time axis ticks
    ticks = [(10, "10 AM"), (13, "1 PM"), (16, "4 PM"), (19, "7 PM"), (22, "10 PM")]
    axis = "".join(
        f'<line x1="{fmt(xof(ht))}" y1="{fmt(baseline_y)}" '
        f'x2="{fmt(xof(ht))}" y2="{fmt(baseline_y+3)}" '
        f'stroke="#4a5566" stroke-width="0.7"/>'
        f'<text x="{fmt(xof(ht))}" y="{fmt(baseline_y+10)}" '
        f'font-family="JetBrains Mono, monospace" font-size="7" font-weight="500" '
        f'fill="#4a5566" text-anchor="middle" letter-spacing="0.05em">{label}</text>'
        for ht, label in ticks
    )

    # Markers for visible high/low events
    markers = ""
    for ev in visible_events:
        x, y = xof(ev["t_hours"]), yof(ev["height"])
        kind = "HIGH" if ev["kind"] == "H" else "LOW"
        text = f'{kind}  {fmt_time(ev["time"])}  ·  {ev["height"]:.1f} ft'
        markers += (
            f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="4.5" '
            f'fill="#fbf6ea" stroke="#1c3a5e" stroke-width="1.8"/>'
            f'<text x="{fmt(x)}" y="{fmt(y - 8)}" '
            f'font-family="JetBrains Mono, monospace" font-size="10" font-weight="700" '
            f'fill="#1c3a5e" text-anchor="middle" letter-spacing="-0.01em">{text}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
        '<defs><linearGradient id="water" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#a8c7d8" stop-opacity="0.65"/>'
        '<stop offset="100%" stop-color="#7daabf" stop-opacity="0.85"/>'
        '</linearGradient></defs>'
        f'<line x1="{fmt(PAD_L)}" y1="{fmt(baseline_y)}" '
        f'x2="{fmt(W-PAD_R)}" y2="{fmt(baseline_y)}" stroke="#4a5566" '
        f'stroke-width="0.7" opacity="0.55"/>'
        f'<path d="{fill_d}" fill="url(#water)"/>'
        f'<path d="{curve_d}" fill="none" stroke="#1c3a5e" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'{stop_dots}{markers}{axis}'
        '</svg>'
    )


def _parse_event(p: dict) -> dict:
    """NOAA event {t, v, type} → {time, t_hours, height, kind}."""
    when = dt.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
    return {
        "time": when,
        "t_hours": when.hour + when.minute / 60,
        "height": float(p["v"]),
        "kind": p["type"],
    }


def _build_height_function(events: list[dict]):
    """Build a piecewise-cosine height(t) function from sorted high/low events."""
    events = sorted(events, key=lambda e: e["t_hours"])

    def height(t: float) -> float:
        # Find the segment containing t
        for i in range(len(events) - 1):
            a, b = events[i], events[i + 1]
            if a["t_hours"] <= t <= b["t_hours"]:
                mean = (a["height"] + b["height"]) / 2
                amp = (a["height"] - b["height"]) / 2  # positive if a is H, neg if L
                half = b["t_hours"] - a["t_hours"]
                return mean + amp * math.cos((t - a["t_hours"]) * math.pi / half)
        # Outside the event range — extrapolate using the closest segment
        if t < events[0]["t_hours"]:
            a, b = events[0], events[1]
        else:
            a, b = events[-2], events[-1]
        mean = (a["height"] + b["height"]) / 2
        amp = (a["height"] - b["height"]) / 2
        half = b["t_hours"] - a["t_hours"]
        return mean + amp * math.cos((t - a["t_hours"]) * math.pi / half)

    return height


# ---------------------------------------------------------------------------
# QR code SVG renderer
# ---------------------------------------------------------------------------

def render_qr_svg(url: str, color: str = "#1c3a5e", quiet: int = 2) -> str:
    """Render `url` as a styled QR code SVG (ECL L, run-length compressed,
    rounded finder patterns)."""
    import qrcode
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=0,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    total = n + 2 * quiet

    finder_positions = [(0, 0), (n - 7, 0), (0, n - 7)]

    def in_finder(r: int, c: int) -> bool:
        return any(fr <= r < fr + 7 and fc <= c < fc + 7 for fr, fc in finder_positions)

    # Run-length encode horizontal runs of dark modules
    parts: list[str] = []
    for r in range(n):
        c = 0
        while c < n:
            if matrix[r][c] and not in_finder(r, c):
                start = c
                while c < n and matrix[r][c] and not in_finder(r, c):
                    c += 1
                width = c - start
                parts.append(f"M{start + quiet},{r + quiet}h{width}v1h-{width}z")
            else:
                c += 1
    data_d = "".join(parts)

    # Stylized finder patterns
    finder_svg = []
    for fr, fc in finder_positions:
        x, y = fc + quiet, fr + quiet
        outer = (
            f"M{x+1.5},{y}"
            f"h4a1.5,1.5 0 0 1 1.5,1.5"
            f"v4a1.5,1.5 0 0 1 -1.5,1.5"
            f"h-4a1.5,1.5 0 0 1 -1.5,-1.5"
            f"v-4a1.5,1.5 0 0 1 1.5,-1.5z"
        )
        inner_hole = (
            f"M{x+1.5},{y+1}"
            f"a0.5,0.5 0 0 0 -0.5,0.5"
            f"v4a0.5,0.5 0 0 0 0.5,0.5"
            f"h4a0.5,0.5 0 0 0 0.5,-0.5"
            f"v-4a0.5,0.5 0 0 0 -0.5,-0.5z"
        )
        finder_svg.append(
            f'<path d="{outer}{inner_hole}" fill="{color}" fill-rule="evenodd"/>'
        )
        finder_svg.append(
            f'<rect x="{x+2}" y="{y+2}" width="3" height="3" '
            f'rx="0.8" ry="0.8" fill="{color}"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'shape-rendering="geometricPrecision">'
        f'<path d="{data_d}" fill="{color}" shape-rendering="crispEdges"/>'
        + "".join(finder_svg)
        + "</svg>"
    )


# ---------------------------------------------------------------------------
# Stop enrichment (add computed maps_url, duration_total, etc.)
# ---------------------------------------------------------------------------

def enrich_stops(stops_raw: list[dict], sun: dict, car_cfg: dict) -> list[dict]:
    """Add per-stop derived fields the template needs."""
    stops_with_battery = compute_soc(stops_raw, car_cfg)
    golden = fmt_time(sun["golden_hour"])
    result = []
    for stop in stops_with_battery:
        s = dict(stop)
        # Maps URL for the address link
        s["maps_url"] = (
            "https://www.google.com/maps/search/?api=1&query="
            + urllib.parse.quote_plus(s["address_query"])
        )
        # Leg distance label fallback
        if "leg_distance_label" not in s and "leg_distance_mi" in s:
            mi = s["leg_distance_mi"]
            mn = s.get("leg_drive_min", 0)
            if mi == 0 and mn == 0:
                s["leg_distance_label"] = None
            else:
                s["leg_distance_label"] = f"{mi} mi · ~{mn} min"
        # Meta with template substitution for golden hour
        if "meta_extra_template" in s:
            s["meta"] = [
                {
                    **m,
                    "value": m["value"].format(golden_hour=golden),
                }
                for m in s["meta_extra_template"]
            ]
        result.append(s)
    return result


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def build(date: dt.date | None = None, *, force_fetch: bool = False) -> Path:
    cfg = load_config()
    if date is None:
        date = dt.date.fromisoformat(cfg["trip"]["date"])

    stops_raw = load_stops()
    sun = compute_sun(
        date,
        cfg["location"]["sun_lat"],
        cfg["location"]["sun_lng"],
        cfg["location"]["sun_tz"],
        cfg["location"]["sun_city_name"],
    )
    tides = fetch_tides(cfg["location"]["tide_station_id"], date, force=force_fetch)

    # Merge battery thresholds into car_cfg for compute_soc
    car_cfg = dict(cfg["car"])
    car_cfg["_battery_thresholds"] = cfg["battery_thresholds"]

    stops = enrich_stops(stops_raw, sun, car_cfg)
    tide_svg = render_tide_svg(tides, stops, {})
    qr_svg = render_qr_svg(cfg["maps"]["all_stops_url"])

    # Render template
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
    from markupsafe import Markup
    env = Environment(
        loader=FileSystemLoader(ROOT),
        undefined=StrictUndefined,
        autoescape=True,  # escape user data in attributes/text…
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE.name)
    # … but the rendered SVG strings are pre-formed markup
    html = template.render(
        cfg=cfg,
        date=date,
        date_long=fmt_date_long(date),
        date_short=fmt_date_short(date),
        title_meta=f"{cfg['trip']['title']} · {fmt_date_long(date)}",
        # Embed JetBrains Mono so PDF SVG text renders consistently.
        jbm_css=Markup(get_jetbrains_mono_css()),
        # style.css is inlined here so the output is a single self-contained file.
        style_css=Markup((ROOT / "style.css").read_text()),
        sun={
            **{k: fmt_time(v) for k, v in sun.items() if k != "day_length"},
            "day_length": fmt_duration_hm(sun["day_length"]),
        },
        stops=stops,
        tide_svg=Markup(tide_svg),
        qr_svg=Markup(qr_svg),
        # PDF page split: stops[:page_break_at] on page 1, rest on page 2.
        # Tuned so page 1 ends after Footbridge Lobster (stop 04) — the lunch break.
        page_break_at=cfg.get("pdf", {}).get("break_before_stop_index", 5),
    )

    BUILD.mkdir(exist_ok=True)
    OUTPUT.write_text(html)
    return OUTPUT


# ---------------------------------------------------------------------------
# PDF rendering (Playwright + page-count check via pypdf)
# ---------------------------------------------------------------------------

PDF_OUTPUT = BUILD / "trip-handout.pdf"
HANDOUT_PNG_OUTPUT = BUILD / "trip-handout.png"
PREVIEW_OUTPUT = BUILD / "preview.png"

# 1.91:1 — the de facto aspect for Open Graph / Twitter Card hero images.
# iMessage, Google Messages (RCS), WhatsApp, Signal, Slack, Twitter, Facebook
# all crop link-preview images to roughly this size.
PREVIEW_W, PREVIEW_H = 1200, 630

# 8.5 × 11 inches at Chromium's 96 dpi.
PRINT_PAGE_PX = 1056

# Chromium needs ~1% slack below the theoretical scale before rounding pushes
# content onto a third page. 0.985 is plenty empirically — leaves only ~15px
# of whitespace per page at the bottom.
SCALE_SAFETY = 0.97


def _launch_chromium(p):
    """Launch Chromium, installing the binary on first run if missing."""
    try:
        return p.chromium.launch()
    except Exception:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        return p.chromium.launch()


def render_pdf(html_path: Path, pdf_path: Path, *, expected_pages: int = 2) -> tuple[int, float]:
    """Render `html_path` to `pdf_path` via headless Chromium (Playwright).

    Measures the laid-out heights of page 1 (content before .running-header)
    and page 2 (.running-header onwards), then picks the largest PDF scale
    that fits both inside one letter page each. Returns (page_count, scale).
    Raises RuntimeError if the rendered count doesn't match `expected_pages`.
    """
    from playwright.sync_api import sync_playwright

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        page = browser.new_page(viewport={"width": 816, "height": PRINT_PAGE_PX})
        page.goto(f"file://{html_path.resolve()}")
        page.wait_for_load_state("networkidle")
        # Switch to print media FIRST, then block on webfonts being ready.
        # The order matters: emulating print can trigger re-layout, and SVG
        # <text> in particular falls back to a system mono if JetBrains Mono
        # hasn't fully loaded *for the current media* before page.pdf() fires.
        page.emulate_media(media="print")
        page.evaluate("async () => { await document.fonts.ready; }")

        m = page.evaluate(
            "() => ({"
            "  break_y: document.querySelector('.page-break').getBoundingClientRect().top,"
            "  total:   document.body.scrollHeight,"
            "})"
        )
        page1_native = float(m["break_y"])
        page2_native = float(m["total"]) - page1_native
        max_native = max(page1_native, page2_native)
        scale = max(0.1, min(2.0, PRINT_PAGE_PX * SCALE_SAFETY / max_native))

        page.pdf(
            path=str(pdf_path),
            format="Letter",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            prefer_css_page_size=False,
            scale=scale,
        )
        browser.close()

    from pypdf import PdfReader
    pages = len(PdfReader(str(pdf_path)).pages)
    if expected_pages is not None and pages != expected_pages:
        raise RuntimeError(
            f"PDF page count regression: expected {expected_pages}, got {pages} "
            f"(scale={scale:.3f}, page1_native={page1_native:.0f}px, "
            f"page2_native={page2_native:.0f}px). Tighten the design or move the "
            f"page-break point in stops.toml/template."
        )
    return pages, scale


# ---------------------------------------------------------------------------
# Social-share preview PNG (Playwright screenshot of the page hero)
# ---------------------------------------------------------------------------

def render_preview_png(html_path: Path, png_path: Path) -> None:
    """Screenshot the masthead → tides region of index.html as preview.png.

    Loaded by link-preview scrapers (iMessage, Google Messages, Slack,
    WhatsApp…) via og:image when the URL is texted. The .page CSS is overridden
    so the centered card expands to fill the 1200px viewport edge-to-edge;
    everything below the tide chart is hidden so the 1200×630 clip lands on a
    clean composition.
    """
    from playwright.sync_api import sync_playwright

    png_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        # device_scale_factor=2 → physically 2400×1260 pixels behind the
        # 1200×630 CSS viewport. iMessage downsamples to a thumbnail anyway,
        # but desktop Slack / Twitter benefit from the extra crispness.
        page = browser.new_page(
            viewport={"width": PREVIEW_W, "height": PREVIEW_H},
            device_scale_factor=2,
        )
        page.goto(f"file://{html_path.resolve()}")
        page.wait_for_load_state("networkidle")
        page.evaluate("async () => { await document.fonts.ready; }")
        page.add_style_tag(content="""
            html, body { margin: 0 !important; padding: 0 !important; }
            .page {
                width: 100% !important; max-width: 100% !important;
                min-height: 0 !important; margin: 0 !important;
                padding: 26px 56px !important; box-shadow: none !important;
            }
            /* Everything past the tide chart is irrelevant to the hero crop,
               but the route-block and colophon would otherwise stretch the
               document tall and slow the screenshot. Hide them. The section
               heading + first itinerary card stay visible so the bottom
               ~150px of the 1200×630 frame teases the content. */
            .route-block, .colophon { display: none !important; }
            /* Trim the first stop's padding so its card peeks into frame
               without dominating it. */
            .timeline .stop:not(:first-child) { display: none !important; }
        """)
        page.screenshot(
            path=str(png_path),
            clip={"x": 0, "y": 0, "width": PREVIEW_W, "height": PREVIEW_H},
        )
        browser.close()


# ---------------------------------------------------------------------------
# Handout PNG (rasterized page 1 of trip-handout.pdf)
# ---------------------------------------------------------------------------

# 144 dpi → 1224×1584 PNG. Reproducing Chromium's print auto-fit in a screenshot
# is fiddly (CSS transform shrinks .page horizontally, leaving paper-color
# whitespace to the right), so rasterizing the already-correct PDF is both
# simpler and pixel-perfect against the printed sheet.
HANDOUT_PNG_DPI = 144


def render_handout_png(pdf_path: Path, png_path: Path, *, dpi: int = HANDOUT_PNG_DPI) -> None:
    """Rasterize page 1 of trip-handout.pdf to PNG using pypdfium2.

    Output matches the printed front of the sheet exactly — same letter
    proportions, same auto-fit scale, no surrounding whitespace.
    """
    import pypdfium2 as pdfium

    png_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    bitmap = pdf[0].render(scale=dpi / 72)
    bitmap.to_pil().save(str(png_path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build build/index.html (and optionally a PDF)")
    parser.add_argument("--date", help="Override trip date (YYYY-MM-DD)")
    parser.add_argument("--force-fetch", action="store_true",
                        help="Re-fetch NOAA tide data even if cached")
    parser.add_argument("--no-pdf", action="store_true",
                        help="Skip the PDF + preview render steps (fast iteration)")
    args = parser.parse_args()
    date = dt.date.fromisoformat(args.date) if args.date else None
    out = build(date=date, force_fetch=args.force_fetch)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    if not args.no_pdf:
        pages, scale = render_pdf(out, PDF_OUTPUT)
        print(f"Wrote {PDF_OUTPUT} ({PDF_OUTPUT.stat().st_size:,} bytes, "
              f"{pages} pages, scale={scale:.3f})")
        render_handout_png(PDF_OUTPUT, HANDOUT_PNG_OUTPUT)
        print(f"Wrote {HANDOUT_PNG_OUTPUT} ({HANDOUT_PNG_OUTPUT.stat().st_size:,} bytes)")
        render_preview_png(out, PREVIEW_OUTPUT)
        print(f"Wrote {PREVIEW_OUTPUT} ({PREVIEW_OUTPUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
