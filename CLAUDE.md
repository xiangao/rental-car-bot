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
- **Dashboard "SUV price history" is in-class only.** `html_writer._history_table`
  renders the CSV's `cheapest_in_class_*` columns (the requested class — an SUV
  for every current trip): Date · SUV price · Supplier · Vehicle · Book. The
  cheapest-overall column was dropped (it tracked whatever compact was cheapest,
  noise for an SUV search). The per-row "Book ↗" link is the trip's current
  `deep_link` (for Costco that's the search-form URL — robots.txt forbids
  drill-down, so there is no per-offer URL to store); history rows reuse it.

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

> **2026-05-31 — site redesign + Akamai POST block (IMPORTANT).**
> Costco redesigned the rental search form. Three `_fetch_results_html` /
> `_fill_typeahead` bugs were found and fixed (all verified live):
> 1. **Location typeahead** — markup changed from jQuery-UI
>    (`ul.ui-autocomplete` / `.ui-menu-item`) to `<ul class="ui-list">` with
>    `<li class="airport" data-value="YYC" role="option">`. Fix: read the
>    input's `aria-controls` (a UUID id → use a `[id="…"]` attribute selector,
>    NOT `#id`, since it starts with a digit) and click `li[data-value='YYC']`.
>    Avoid generic `[role=option]` — its first item is a country header.
> 2. **Time `<select>`** — the 12:00 slots are *labelled* "Noon"/"Midnight",
>    so `select_option(label=…)` times out. Fix: `select_option(value=…)`
>    (values are still the "HH:MM AM/PM" strings).
> 3. **Date** — inputs are jQuery-UI datepickers (`hasDatepicker`); injecting
>    `.value` leaves the picker's internal model null → the search silently
>    no-ops. Fix: `$(input).datepicker('setDate', new Date(y, m, day))`
>    (jQuery is global as `window.jQuery`).
>
> **Remaining wall:** even with every field valid, clicking Search fires
> `POST /rentalCarSearch.act` that gets **no response** (not 403 — dropped) →
> Akamai blocking the headless browser. This appeared only AFTER ~8 rapid
> debug runs flagged the IP. As of 2026-05-31 it's UNVERIFIED whether a clean
> once-daily run from a cold IP succeeds — the timer was set up (pre-stamped,
> first run Mon 2026-06-01 11:30) precisely to test that. **If it still returns
> 0 offers from a cold IP, the realistic options are: try `provider:booking`
> again, a stealthier browser (patchright/camoufox) + residential proxy, or a
> different data source.** Do NOT burst-test against Costco while debugging.
>
> **2026-06-01 — RESOLVED (cold-IP test failed; fixed via real Chrome over CDP).**
> The cold 11:30 run returned 0 offers, confirming the wall. A live diagnostic
> showed the *launched* Chromium (even `channel="chromium"`) is served a
> degraded form: clicking Search fires **no** `/rentalCarSearch.act` at all
> (only Akamai's sensor beacon) because the search handler never initializes.
> Fix: `_fetch_results_html` now drives a **real, headful `google-chrome`** over
> CDP (`connect_over_cdp(http://localhost:9222)`) instead of launching Chromium.
> `_ensure_chrome()` launches/reuses google-chrome on a dedicated profile
> (`~/.cache/chrome-rental-bot`, `DISPLAY` defaults to `:1`). Verified: 27
> offers, `POST /rentalCarSearch.act` → 200. The 3 form fixes above are still
> needed; this only changes the *browser*. **Caveat:** headful Chrome needs an
> active X display, so the daily run must happen while the desktop session is up.

- **Login:** none required for browsing. Membership only matters at booking.
- **Currency:** Costco serves CAD by default for YYC pickups (geo-detected
  from the browser's timezone). The scraper honors `data-currency-code` on
  each offer, so the CSV will store whatever Costco actually quoted. Keep
  `currency: CAD` in `trips.yaml` for Calgary; if you ever scrape a US
  pickup, expect USD and update the trip accordingly.
- **Driver age:** Costco only branches on under-25 vs 25+ via a checkbox
  (`#driversAgeWidget`). The searcher converts `driver_age >= 25` to a
  checked box. The numeric value in `trips.yaml` is informational only.
- **Akamai Bot Manager:** active on costcotravel.com, and it scores *behavior*,
  not just the TLS handshake. History: headless-shell Chromium is rejected at
  the network layer (`ERR_HTTP2_PROTOCOL_ERROR`); full `channel="chromium"`
  clears the handshake but (as of 2026-06-01) is served a non-functional form.
  **Current approach: drive a real, headful `google-chrome` over CDP** —
  `_ensure_chrome()` launches/reuses it on `:9222` against a dedicated profile,
  and `_fetch_results_html` attaches via `connect_over_cdp`. No
  `playwright install` needed for this path (uses system Chrome), but headful
  Chrome requires an active X display (`DISPLAY`, default `:1`).
- **Robots.txt:** `/Rental-Cars` and `/rentalCarSearch.act` are allowed;
  `/rc/` (results-detail pages) is disallowed. The scraper stops at the
  results listing — do not add drill-down code. `deep_link` is set to the
  search-form URL, not an individual offer URL.
- **Cadence:** the user systemd timer is `OnCalendar=*-*-* 11:30:00` (daily,
  changed from 09:00 on 2026-05-31). **Do not tighten it.** Once-per-day stays
  well below Akamai's scoring thresholds; minute-or-hour cadence — *or a burst
  of debugging runs* — will get the IP flagged (see 2026-05-31 note below).
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
