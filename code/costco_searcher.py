"""Costco Travel car-rental scraper via Playwright.

Costco does not require a login to view rental-car prices (verified via
Costco's Rental-Cars-FAQs and a working 2017 community scraper). Akamai
Bot Manager is active on costcotravel.com, so we use Playwright with a
realistic Chromium fingerprint at the bot's existing once-per-day
cadence — do NOT tighten the timer.

robots.txt allows `/Rental-Cars` and `/rentalCarSearch.act` but disallows
`/rc/` (results-detail pages). We only parse what's rendered on the
search-results page itself; no drill-down.

Returns the same CarRentalResult dataclass as code.searcher so the rest of
the pipeline (analyzer, notifier, html_writer) is provider-agnostic.

First-run note: on the first ever execution the rendered results HTML is
dumped to data/debug/costcoSearchCarRentals.html. If selectors below stop
matching after a Costco redesign, wipe that file, re-run, and tune from
the captured DOM.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from code.searcher import CarOffer, CarRentalResult, _matches_class

BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data" / "api_cache"
DEBUG_DIR = BASE_DIR / "data" / "debug"

SEARCH_URL = "https://www.costcotravel.com/Rental-Cars"

# Costco sits behind Akamai Bot Manager. A *launched* automation Chromium (even
# channel="chromium") is served a degraded form whose search handler never
# initializes, so the submit silently no-ops and 0 offers come back. A real,
# headful google-chrome driven over CDP passes Akamai's behavioral check
# (verified 2026-06-01: 29 result cards, POST /rentalCarSearch.act → 200). We
# launch one on this port against a dedicated profile dir (separate from the
# user's main Chrome so the singleton lock doesn't collide) and reuse it across
# daily runs, which also lets the Akamai cookies warm.
CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
CHROME_PROFILE = Path.home() / ".cache" / "chrome-rental-bot"


# ────────────────────────────────────────────────────────────────────────────────
# Cache & debug helpers (parallel to searcher.py's patterns)
# ────────────────────────────────────────────────────────────────────────────────

def _cache_path(prefix: str, params: dict[str, Any]) -> Path:
    digest = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{prefix}_{digest}.json"


def _read_cache(path: Path, ttl_seconds: int) -> dict | None:
    if not path.exists():
        return None
    if ttl_seconds and (time.time() - path.stat().st_mtime) > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _save_debug(name: str, html: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    target = DEBUG_DIR / f"{name}.html"
    if not target.exists():
        target.write_text(html)


# ────────────────────────────────────────────────────────────────────────────────
# Format conversions
# ────────────────────────────────────────────────────────────────────────────────

def _date_mdy(iso: str) -> str:
    """YYYY-MM-DD → MM/DD/YYYY for Costco's date widgets."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%Y")


def _time_12h(hhmm: str) -> str:
    """24h HH:MM → '12:00 PM' for Costco's time widgets."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")


def _price_to_float(s: str) -> float | None:
    m = re.search(r"\$?([0-9][0-9,]*(?:\.[0-9]+)?)", s or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ────────────────────────────────────────────────────────────────────────────────
# Browser flow
# ────────────────────────────────────────────────────────────────────────────────

def _chrome_up() -> bool:
    """True if a Chrome DevTools endpoint is already listening on the CDP port."""
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _ensure_chrome() -> None:
    """Make sure a real, headful google-chrome is listening on the CDP port.

    Reuses an already-running instance (so cookies warm across runs); otherwise
    launches one against CHROME_PROFILE. Headful needs an X display — the bot
    runs on the user's desktop session, so DISPLAY defaults to ":1" when unset
    (e.g. under the systemd user timer).
    """
    if _chrome_up():
        return
    chrome = shutil.which("google-chrome") or "/usr/bin/google-chrome"
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    # A stale singleton lock makes a second launch silently forward to the old
    # instance and DROP --remote-debugging-port, so clear it first.
    for lock in CHROME_PROFILE.glob("Singleton*"):
        try:
            lock.unlink()
        except OSError:
            pass
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":1")
    # Under the systemd --user timer, DISPLAY/XAUTHORITY aren't inherited. Point
    # at the active session's gdm cookie so headful Chrome can attach to the X
    # server (the login session keeps this file for as long as the user is in).
    if "XAUTHORITY" not in env:
        xauth = Path(f"/run/user/{os.getuid()}/gdm/Xauthority")
        if xauth.exists():
            env["XAUTHORITY"] = str(xauth)
    subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # outlive this process so the next run can reuse it
        env=env,
    )
    for _ in range(40):
        if _chrome_up():
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome did not come up on {CDP_URL}. Headful Chrome needs an X "
        f"display (DISPLAY={env.get('DISPLAY')}); Costco's Akamai requires a "
        f"real browser, so a desktop session must be active when this runs."
    )


def _fill_typeahead(page: Page, selector: str, value: str) -> None:
    """Type into a Costco typeahead and click the matching airport entry.

    Costco's typeahead renders ``<ul class="ui-list" role="listbox">`` with
    ``<li class="airport" data-value="YYC" role="option">`` items, and the input's
    ``aria-controls`` attribute points at that ``<ul>``'s dynamically-generated id.
    We prefer the exact ``data-value`` match (the IATA/location code), else the
    first real entry — NOT the generic ``[role=option]``, whose first item is a
    country header. (The pre-2026 jQuery-UI markup — ``ul.ui-autocomplete`` /
    ``.ui-menu-item`` — no longer exists; selecting it left the location unset and
    the whole search silently failed.)
    """
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=80)
    try:
        list_id = page.get_attribute(selector, "aria-controls", timeout=5000)
        # aria-controls ids begin with a digit, so a "#id" CSS selector is invalid —
        # use an attribute selector, which accepts any value.
        list_sel = f'[id="{list_id}"]' if list_id else "ul.ui-list[role='listbox']"
        page.wait_for_selector(f"{list_sel} li[data-value]", timeout=5000)
        exact = page.locator(f"{list_sel} li[data-value='{value}']")
        if exact.count():
            exact.first.click()
        else:
            page.locator(f"{list_sel} li[data-value]").first.click()
    except PlaywrightTimeout:
        # Fall back: press Enter and hope Costco accepts the literal text.
        page.press(selector, "Enter")


def _fetch_results_html(trip: dict) -> str:
    """Drive Playwright through the Costco form and return the results-page HTML."""
    pickup_time = _time_12h(trip["pickup_time"])
    dropoff_time = _time_12h(trip["dropoff_time"])
    same_location = trip["pickup_location"] == trip["dropoff_location"]

    _ensure_chrome()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        # Use the real profile's default context (not a fresh new_context) so
        # the warmed Akamai cookies carry over between runs.
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
            # Let Akamai's sensor script finish and the React form mount.
            page.wait_for_load_state("networkidle", timeout=45000)
            page.wait_for_selector("#search_rental_cars_form", timeout=30000)

            # Pickup location (typeahead).
            _fill_typeahead(page, "#pickupLocationTextWidget", trip["pickup_location"])

            # Drop-off — if different from pickup, switch radio and fill.
            if not same_location:
                page.check("input[name='carDropOfLocationType'][value='differentLocation']")
                _fill_typeahead(page, "#dropoffLocationTextWidget", trip["dropoff_location"])

            # Dates: the widgets are jQuery-UI datepickers (class "hasDatepicker").
            # Injecting the input's .value leaves the picker's *internal* selected
            # date null, so Costco's search validates against null and silently
            # no-ops. Setting it through the datepicker API populates both the input
            # value and the internal model in one shot.
            for sel, iso in (
                ("#pickUpDateWidget", trip["pickup_date"]),
                ("#dropOffDateWidget", trip["dropoff_date"]),
            ):
                d = datetime.strptime(iso, "%Y-%m-%d")
                page.evaluate(
                    "([sel, y, m, day]) => {"
                    "  const jq = window.jQuery || window.$;"
                    "  if (!jq) return;"
                    "  const el = jq(sel);"
                    "  if (el.length) { el.datepicker('setDate', new Date(y, m, day));"
                    "    el.trigger('change'); }"
                    "}",
                    [sel, d.year, d.month - 1, d.day],   # JS months are 0-based
                )

            # Times: each <option>'s VALUE is the "HH:MM AM/PM" string, but the
            # 12:00 slots are *labelled* "Noon"/"Midnight" — so select by value
            # (selecting by label times out on those slots).
            for sel, val in (
                ("#pickupTimeWidget", pickup_time),
                ("#dropoffTimeWidget", dropoff_time),
            ):
                try:
                    page.select_option(sel, value=val)
                except Exception:
                    # Fall back: direct value set.
                    page.evaluate(
                        "([sel, val]) => {"
                        "  const el = document.querySelector(sel);"
                        "  if (el) { el.value = val;"
                        "    el.dispatchEvent(new Event('change', {bubbles: true})); }"
                        "}",
                        [sel, val],
                    )

            # Driver-age checkbox (25+).
            if int(trip.get("driver_age", 0)) >= 25:
                try:
                    page.check("#driversAgeWidget")
                except Exception:
                    pass

            # Submit. Click the rental search button explicitly — the page has
            # several other submit buttons (Hotels/Packages tabs), so a generic
            # "button[type=submit]" can hit the wrong one.
            try:
                page.click("#findMyCarButton")
            except Exception:
                page.click(
                    "button[type='submit'], input[type='submit'], button:has-text('Search')"
                )

            # Wait for the results cards to render (the parser keys on
            # <a class="car-result-card">). Keep the older hints as fallbacks.
            try:
                page.wait_for_selector(
                    "a.car-result-card, .car-result-card, .car-class-result, .rate-result, "
                    ".results, [data-testid*='result'], table.results, .vehicle-card",
                    timeout=60000,
                )
            except PlaywrightTimeout:
                # We still want to capture whatever rendered for debugging.
                pass

            page.wait_for_load_state("networkidle", timeout=30000)
            html = page.content()
        finally:
            # Close only the tab; leave the shared real Chrome running so the
            # next daily run reuses it (warm Akamai cookies, faster startup).
            page.close()

    return html


# ────────────────────────────────────────────────────────────────────────────────
# Results parsing
# ────────────────────────────────────────────────────────────────────────────────

# Each offer on the Costco results page is an <a> with class="card car-result-card …"
# and a rich set of data-* attributes. We anchor on `data-price=` and read the
# rest off the same tag, which is far more robust than fuzzy text matching.
_CARD_RE = re.compile(
    r'<a\b[^>]*\bclass="[^"]*\bcar-result-card\b[^"]*"[^>]*>(?P<inner>.*?)</a>',
    re.S,
)
_ATTR_RE = re.compile(r'\b([a-z][a-z0-9-]*)="([^"]*)"', re.I)
_VEHICLE_RE = re.compile(
    r'<span[^>]*class="[^"]*\bcar-type\b[^"]*"[^>]*>(.*?)</span>', re.S,
)


def _parse_card_tag(tag_html: str) -> dict:
    """Extract data-* and other attributes from an <a> opening tag."""
    end = tag_html.find(">")
    head = tag_html[: end + 1] if end != -1 else tag_html
    return {k.lower(): v for k, v in _ATTR_RE.findall(head)}


def _parse_results(html: str, currency: str, pickup_name: str, dropoff_name: str) -> list[CarOffer]:
    """Extract offer rows from a Costco rental-results page.

    Costco renders each (supplier × car-class) combination as an <a class="card
    car-result-card …"> element with the price, supplier brand, category name,
    SIPP code, transmission, seat count, etc. all exposed as data-* attributes.
    A sibling <span class="car-type"> inside the card holds the vehicle example
    ("Chevrolet Spark or similar"). The detected `currency` argument is
    overridden by the page's `data-currency-code` (typically CAD when accessed
    from a Canadian IP, USD from a US IP).
    """
    offers: list[CarOffer] = []
    # Costco renders the same offer in adjacent matrix cells; dedupe on the
    # (supplier, category, price) triple, which is unique per offer.
    seen: set[tuple[str, str, float]] = set()

    for card in _CARD_RE.finditer(html):
        attrs = _parse_card_tag(card.group(0))

        price_raw = attrs.get("data-price")
        try:
            price = float(price_raw) if price_raw else None
        except ValueError:
            price = None
        if price is None or price <= 0:
            continue

        supplier = attrs.get("data-brand", "Unknown").strip() or "Unknown"
        category = attrs.get("data-category-name", "Unknown").strip() or "Unknown"
        transmission = attrs.get("data-car-transmission", "automatic").strip().title()

        observed_currency = (attrs.get("data-currency-code") or currency or "USD").strip().upper()

        # Vehicle example: "<Make Model> or similar"
        vehicle_example = ""
        veh = _VEHICLE_RE.search(card.group("inner"))
        if veh:
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", veh.group(1))).strip()
            vehicle_example = re.sub(r"\s+or similar$", "", txt, flags=re.I)

        key = (supplier, category, price)
        if key in seen:
            continue
        seen.add(key)

        offers.append(
            CarOffer(
                price=price,
                currency=observed_currency,
                supplier=supplier,
                category=category,
                vehicle_example=vehicle_example,
                transmission=transmission or "Automatic",
                fuel_policy="Full to full",
                mileage="Unlimited",
                pickup_location=pickup_name,
                dropoff_location=dropoff_name,
                rating=None,
                cancellation="See Costco Travel",
                deep_link=SEARCH_URL,
            )
        )

    return offers


# ────────────────────────────────────────────────────────────────────────────────
# Public entry point — same signature as code.searcher.search_trip
# ────────────────────────────────────────────────────────────────────────────────

def search_trip(trip: dict, *, cache_ttl_hours: int, top_n: int = 5) -> CarRentalResult:
    currency = trip.get("currency", "USD")
    pickup_name = trip["pickup_location"]
    dropoff_name = trip["dropoff_location"]

    cache_key = {
        "pickup": pickup_name,
        "dropoff": dropoff_name,
        "pickup_date": trip["pickup_date"],
        "dropoff_date": trip["dropoff_date"],
        "pickup_time": trip["pickup_time"],
        "dropoff_time": trip["dropoff_time"],
        "driver_age": trip.get("driver_age"),
        "currency": currency,
    }
    cache_file = _cache_path("costco_rentals", cache_key)
    cached = _read_cache(cache_file, cache_ttl_hours * 3600) if cache_ttl_hours else None

    if cached and "html" in cached:
        html = cached["html"]
    else:
        html = _fetch_results_html(trip)
        # Only cache a page that actually carried offers. Costco's Akamai serves
        # a card-less block/empty page when it distrusts the browser; caching
        # that would turn a momentary block into a `cache_ttl_hours`-long outage
        # of stale "0 offers". A blocked page is re-fetched on the next run.
        if "car-result-card" in html:
            _write_cache(cache_file, {"html": html, "fetched_at": time.time()})
    _save_debug("costcoSearchCarRentals", html)

    offers = _parse_results(html, currency, pickup_name, dropoff_name)
    offers.sort(key=lambda o: o.price)

    cheapest = offers[0] if offers else None
    car_class = trip.get("car_class", "any")
    in_class = [o for o in offers if _matches_class(o, car_class)]
    cheapest_in_class = in_class[0] if in_class else None

    return CarRentalResult(
        trip_name=trip["name"],
        cheapest=cheapest,
        cheapest_in_class=cheapest_in_class,
        sampled_offers=offers[:top_n],
        offer_count=len(offers),
    )


# Convenience for ad-hoc debugging: `python -m code.costco_searcher`
if __name__ == "__main__":
    import yaml
    with open(BASE_DIR / "config" / "trips.yaml") as f:
        cfg = yaml.safe_load(f)
    for trip in cfg["trips"]:
        if trip.get("provider", "booking") != "costco":
            continue
        result = search_trip(trip, cache_ttl_hours=0)
        print(f"\n=== {result.trip_name} — {result.offer_count} offers ===")
        if result.cheapest:
            o = result.cheapest
            print(f"Cheapest overall:  {o.currency} {o.price:,.2f}  {o.supplier}  {o.category}  ({o.vehicle_example})")
        if result.cheapest_in_class:
            o = result.cheapest_in_class
            print(f"Cheapest in class: {o.currency} {o.price:,.2f}  {o.supplier}  {o.category}  ({o.vehicle_example})")
        if not result.cheapest:
            print(f"No offers parsed. Inspect {DEBUG_DIR / 'costcoSearchCarRentals.html'}")
