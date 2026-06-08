# Lobsters & Lighthouses — Maine day-trip handout

A static-HTML in-car handout for a 10-stop day trip up the southern Maine coast,
generated from data files by a small Python build script.

## File map

```
maine/
├── run                  # ./run [all|build|test|refresh] — wrapper for uvx + flow
├── config.toml          # date · NOAA station · sun location · car SoC params
├── stops.toml           # the 11 stops — addresses, hours, notes, leg distances
├── template.html.j2     # Jinja2 template — the design layer
├── build.py             # fetch + compute + render → build/trip-handout.html
├── test_build.py        # pytest suite (50 tests, run before every change)
├── build/
│   └── trip-handout.html  # generated output (what you publish or print)
├── data/
│   └── tides_<station>_<YYYYMMDD>.json  # NOAA fetch cache, keyed by date
└── .archive/            # superseded files (original CSV, pre-refactor HTML, etc.)
```

## Run + test

The `./run` wrapper handles uvx + the dep list (`jinja2`, `astral`, `qrcode`,
+ `pytest` for tests):

```sh
./run                            # tests, then build (the usual cycle)
./run build                      # just build (skip tests)
./run build --date 2026-07-13    # build for a date other than config.toml
./run test                       # just tests, verbose
./run test -k battery            # filter tests
./run refresh                    # re-pull NOAA tides (bypass cache) then build
```

Output lands in `build/trip-handout.html`. Open it in a browser to view, or
publish it (Netlify Drop is the quickest path to phone — see "Publishing to
phone" below).

If you'd rather invoke things directly without the wrapper:

```sh
uvx --with jinja2 --with astral --with qrcode python3 build.py
uvx --with jinja2 --with astral --with qrcode --with pytest \
    python3 -m pytest test_build.py -v
```

## Data flow

```
config.toml ─┐
stops.toml ──┤
             │      ┌── NOAA API ──┐
             ▼      ▼              │
            build.py               │ (cached in data/)
             │                     │
             │   ┌── astral ────┐  │
             │   ▼              │  │
             │   sun + moon     │  │
             │                  │  │
             ├──► render tide SVG (from cached NOAA predictions)
             ├──► render QR SVG (from maps_url)
             ├──► compute per-stop SoC (from car_cfg + leg distances)
             │
             ▼
        template.html.j2 ──► build/trip-handout.html
```

What each input controls:

| File | Controls |
|---|---|
| `config.toml` `[trip].date` | The date everything is computed for. Single biggest knob. |
| `config.toml` `[location].tide_station_id` | Which NOAA station's tide predictions to fetch. |
| `config.toml` `[location].sun_*` | Where to compute sunrise/sunset (lat/lng/tz). |
| `config.toml` `[car]` + `[car.efficiency]` | Per-stop SoC simulation. Wh/mi is derived from each leg's average speed (distance & drive-time live in `stops.toml`); tune `base_wh_per_mi` / `slope_wh_per_mph` if real trips drift, or add per-leg `[car.efficiency.overrides]` for specific legs. |
| `config.toml` `[maps].all_stops_url` | The Google Maps URL encoded into the QR code and the "Google Maps · all ten stops" link. |
| `stops.toml` | Per-stop data: name, address, hours, notes, leg distance & route. Icons map to `<symbol id="i-X">` in the template. |
| `template.html.j2` | Pure design + structure. Inlines the lighthouse touch icon, the icon symbol library, and the manifest JS. |

What gets fetched / computed at build time:

- **NOAA tide predictions** for the trip date. Cached in `data/` after first
  fetch — delete the cache file (or pass `--force-fetch`) to refresh.
- **Sun times** (sunrise, solar noon, golden hour, sunset, civil dusk, day
  length) via `astral` from the configured lat/lng/tz.
- **Moon phase** via `astral.moon.phase()` — currently computed but not
  displayed (was in the chart earlier; removed during a tightening pass).
- **Per-stop SoC** by walking through stops in order and subtracting
  `leg_distance_mi × wh_per_mi` from the running battery percentage, where
  `wh_per_mi` is derived from the leg's average speed
  (`leg_distance_mi / leg_drive_min × 60`) using the linear model in
  `[car.efficiency]`. The charge stop swaps SoC from arrival-% to
  `charge_target_pct` before computing the next leg.
- **Tide curve SVG** rendered from the NOAA predictions using a piecewise
  cosine between events. Dots along the curve mark each stop's arrival time.
- **QR code SVG** generated from `maps.all_stops_url` with run-length-compressed
  data path and styled rounded finder patterns.

## Common updates

**Move the trip to a different Monday.**
Edit `config.toml`:
```toml
date = "2026-07-13"
```
Then `./run`. Sun, tides, all date strings, manifest description, footer
date, and tide chart all regenerate.

**Tune battery accuracy after a previous trip.**
Two knobs in `config.toml`:
- Adjust `[car.efficiency].base_wh_per_mi` or `slope_wh_per_mph` to shift the
  global model (e.g. raise both if a hotter day or 21" wheels punish you).
- Or pin a single leg by adding to `[car.efficiency.overrides]` with the
  destination stop id as key: `"09" = 305`.

The SoC test in `test_build.py` covers correctness (home arrives in 25–60%,
monotonic decrease pre-charge). The efficiency tests check the speed model.

**Add, remove, or reorder a stop.**
Edit `stops.toml`. The `id` field is what threads through battery + chart +
template — keep the ids stable when possible. Each stop also needs a matching
icon class in `template.html.j2`'s CSS (search for `.stop-icon.<class>`) and a
corresponding `<symbol id="i-<icon>">` in the icon library if you introduce a
new icon.

**Change the route / Google Maps link.**
Drag the route in maps.google.com, copy the URL from the browser, paste into
`[maps].all_stops_url`. The QR regenerates automatically.

**Refresh NOAA predictions.**
```sh
./run refresh
```

**Change visual design (colors, layout, typography).**
Edit `template.html.j2` — the `<style>` block is at the top of the file. The
icon symbol library is also inline in the template. Run the tests after, then
eyeball the output. The tests check structure (placeholders resolved, all
stops present, addresses linked) but not visual correctness.

## Tests

`test_build.py` covers formatters, config/stops loading, battery math,
NOAA fetch caching, sun/moon computation, tide SVG, QR SVG, and end-to-end
build sanity. **Run before commits**:

```sh
./run test
```

Tests use `tmp_path` and `monkeypatch` to keep filesystem effects isolated.
The NOAA fetch test is split: one test confirms the cache shortcut works
(blocks network and reads from a seeded cache file); the live-fetch path is
covered implicitly by every successful build.

## Publishing to phone

The build produces a single self-contained HTML file. To get it onto a phone
home screen with the lighthouse icon and 🍦🦞 label:

1. **Netlify Drop**: drag `build/trip-handout.html` onto
   <https://app.netlify.com/drop>. Get a public URL in ~5 seconds.
2. **Open on phone in Safari (iOS) or Chrome (Android)**.
3. **Add to Home Screen** from the share menu. The icon and label come from
   the embedded `<link rel="apple-touch-icon">` and the JS-generated web
   manifest.

The handout works offline after the first visit (HTML, QR, lighthouse icon,
tide chart are all inlined; only the Google Fonts CDN is external, and those
gracefully degrade to system serif/sans if unreachable).

## Why these design choices

- **TOML over JSON/YAML for config + stops**: comments + readable, and
  `tomllib` is stdlib in 3.11+.
- **Jinja2 with `StrictUndefined`**: a typo'd template variable raises rather
  than rendering empty. Optional stop fields use `{% if 'key' in stop %}`
  rather than `{% if stop.key %}` to avoid the strict-undefined trap.
- **NOAA caching keyed by station + date**: regenerating the same date doesn't
  hit the network; switching dates pulls fresh data once.
- **Speed-derived Wh/mi rather than a hand-coded per-leg table**: efficiency
  is dominated by speed (aero drag), so we compute it from each leg's average
  speed using `wh_per_mi = base + slope × mph`. Distance & drive-time live in
  `stops.toml` as facts about the route; the model parameters live in
  `[car.efficiency]` as facts about the car. Overrides are available per leg
  for known-difficult drives.
- **Self-contained output**: the published HTML doesn't depend on the build
  pipeline. Anyone can view it without Python.
