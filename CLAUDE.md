# CLAUDE.md — rental-car-bot

Project-specific notes for future Claude sessions on this repo.

## What this project is

Daily rental-car price monitor. Sibling of `flight-bot` and `house-bot` under
`~/projects/claude/`. Same architecture — see `../flight-bot/CLAUDE.md` if it
exists, but treat this project as independent (no shared package per the user's
"new project = new project only" rule).

## Architecture in one diagram

```
config/trips.yaml ─┐
                   ▼
 main.py ─► searcher.py ──► booking-com15 RapidAPI (locations + cars)
        ─► analyzer.py (7d rolling avg vs latest)
        ─► notifier.py (CSV append + notify-send)
        ─► html_writer.py ─► output/rentals.html ─► site/index.html (gh-pages push)
```

## Hot spots

- **`code/searcher.py` — `CLASS_KEYWORDS`** — substring map for car-class
  filter. Booking's category strings vary; tune this when a new chapter / class
  shows up that doesn't match the existing fragments. Falls back to "any" for
  unknown classes so the bot still produces output.

- **`code/searcher.py` — `_g()` + `_parse_offer()`** — defensive nested-dict
  reads. The RapidAPI wrapper around booking.com rebrands field paths between
  versions; the multi-fallback chains in `_parse_offer()` survive those without
  code changes. If a path stops matching, **check `data/debug/searchCarRentals.json`
  for the actual field shape** rather than guessing.

- **`data/debug/`** — written once per endpoint, on first encounter. Wipe these
  files if Booking's response shape changes and you want to recapture.

- **`data/api_cache/`** — TTL'd cache per trip-search, keyed by all params. Set
  `search.cache_ttl_hours: 0` in `trips.yaml` to bypass during manual debugging.

- **Location cache** in `data/api_cache/locations.json` is **permanent**. If you
  ever change a trip's `pickup_location` text string and the bot picks the wrong
  Booking record, delete that key from the cache file and re-run.

## Conventions

- **`csv_name` is the stable key** in `data/prices.csv`. Never rename it; if a
  trip changes scope, retire the old `csv_name` and add a new trip entry.
- **`code/` not `src/`** — matches the sibling bots and the user's project layout
  preferences.
- **Don't add features to `searcher.py`** that touch the analyzer or notifier —
  keep the search layer thin and dumb. Class filtering lives there because it's
  about response parsing; *price thresholding* lives in analyzer.py.

## Verification quick-reference

```bash
source .venv/bin/activate
python -m code.searcher          # ad-hoc: prints cheapest per trip without writing anything
python main.py                   # full pipeline
cat output/latest.txt            # plaintext summary
xdg-open output/rentals.html     # dashboard
sqlite3 -csv :memory: \          # quick history scan
  ".import data/prices.csv t" "select trip, count(*) from t group by trip"
```

## Known quirks

1. RapidAPI free tier on booking-com15 is ~500 calls/month. The bot uses ~30/month
   per trip (cached responses don't count). Don't add a 12-hour timer or you'll
   hit quota — daily is right.
2. `notify-send` requires a user session — systemd timers run as `--user`, which
   is fine on a logged-in workstation but silent on headless servers. The bot
   degrades gracefully (prints a warning) if `notify-send` is missing.
3. The site/ deploy uses `gh-pages` branch on a separate repo (`rental-car-bot-site`),
   not the project repo. This matches flight-bot and house-bot; do not change.

## Providers (booking vs costco)

Each trip in `config/trips.yaml` carries a `provider:` field (default
`"booking"`). `main.py` dispatches via the `PROVIDERS` dict to either
`code.searcher.search_trip` (booking-com15 RapidAPI) or
`code.costco_searcher.search_trip` (Costco Travel via Playwright).

**Why both:** booking-com15's cars endpoint was returning `"status":false,
"Something went wrong"` for multiple days in late May 2026 — out of our
control. Costco Travel is a separate vendor with often-better member rates.
The two implementations share only the `CarRentalResult` / `CarOffer`
dataclasses; the rest of the pipeline (analyzer, notifier, html_writer) is
provider-agnostic.

### Costco provider — `code/costco_searcher.py`

- **Login:** none required for browsing. Membership only matters at booking.
- **Currency:** Costco serves CAD by default for YYC pickups (geo-detected
  from the browser's timezone). The scraper honors `data-currency-code` on
  each offer, so the CSV will store whatever Costco actually quoted. Keep
  `currency: CAD` in `trips.yaml` for Calgary; if you ever scrape a US
  pickup, expect USD and update the trip accordingly.
- **Driver age:** Costco only branches on under-25 vs 25+ via a checkbox
  (`#driversAgeWidget`). The searcher converts `driver_age >= 25` to a
  checked box. The numeric value in `trips.yaml` is informational only.
- **Akamai Bot Manager:** active on costcotravel.com. The headless-shell
  build of Chromium gets rejected at the TLS/HTTP-2 layer
  (`ERR_HTTP2_PROTOCOL_ERROR`). The searcher uses the **full Chromium**
  build via `channel="chromium"` plus a light stealth init script.
  Install both: `python -m playwright install chromium`.
- **Robots.txt:** `/Rental-Cars` and `/rentalCarSearch.act` are allowed;
  `/rc/` (results-detail pages) is disallowed. The scraper stops at the
  results listing — do not add drill-down code. `deep_link` is set to the
  search-form URL, not an individual offer URL.
- **Cadence:** the existing user systemd timer (`OnCalendar=*-*-* 09:00:00`)
  is correct. **Do not tighten it.** Once-per-day stays well below Akamai's
  scoring thresholds; minute-or-hour cadence will get the IP flagged.
- **First-run debug:** on first encounter the rendered results HTML is
  dumped to `data/debug/costcoSearchCarRentals.html`. If selectors in
  `_parse_results` stop matching after a Costco redesign, wipe that file,
  re-run, and tune from the captured DOM.
- **Field provenance:** the searcher reads from `<a class="car-result-card">`
  data-* attributes (`data-price`, `data-brand`, `data-category-name`,
  `data-car-transmission`, `data-currency-code`). Costco renders each offer
  twice in adjacent matrix cells; the parser dedupes on
  `(supplier, category, price)`.

### Adding a new provider

The pipeline expects `search_trip(trip, *, cache_ttl_hours, top_n)` →
`CarRentalResult`. Add a new module under `code/`, register it in
`main.PROVIDERS`, and set `provider:` in the trip config. Class filtering
is via `code.searcher.CLASS_KEYWORDS` + `_matches_class` — import them
rather than duplicating.

## Adding a new trip

1. Append a block to `config/trips.yaml` under `trips:`.
2. Pick a `csv_name` distinct from existing ones.
3. Run `python main.py` once; new trip starts with no history → always alerts
   for the first 7 days.
4. If the class filter doesn't match Booking's category for that trip's actual
   results, check `data/debug/searchCarRentals.json` for the real strings and
   add a fragment to `CLASS_KEYWORDS` in `code/searcher.py`.
