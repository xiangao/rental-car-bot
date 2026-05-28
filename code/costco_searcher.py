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
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from code.searcher import CarOffer, CarRentalResult, _matches_class

BASE_DIR = Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / "data" / "api_cache"
DEBUG_DIR = BASE_DIR / "data" / "debug"

SEARCH_URL = "https://www.costcotravel.com/Rental-Cars"

# Recent stable desktop Chrome on Linux — match what real users send.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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

def _new_context(p: Playwright):
    """Full headless Chromium with a realistic-looking fingerprint.

    Use channel='chromium' (not the default headless shell) — Akamai Bot
    Manager rejects the headless-shell TLS/HTTP-2 fingerprint at the
    network layer with ERR_HTTP2_PROTOCOL_ERROR, while the full chromium
    build passes the initial handshake.
    """
    browser = p.chromium.launch(
        channel="chromium",
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    context = browser.new_context(
        user_agent=UA,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Edmonton",  # Calgary-equivalent for the user's typical search
    )
    # Light stealth: hide the navigator.webdriver flag the way real browsers don't expose it.
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return browser, context


def _fill_typeahead(page: Page, selector: str, value: str) -> None:
    """Type into a Costco typeahead, wait for the dropdown, click the first match."""
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=80)
    # The autocomplete panel typically renders within ~1s.
    try:
        page.wait_for_selector("ul.ui-autocomplete li, .ui-menu-item", timeout=5000)
        page.click("ul.ui-autocomplete li:first-child, .ui-menu-item:first-child")
    except PlaywrightTimeout:
        # Fall back: press Enter and hope Costco accepts the literal text.
        page.press(selector, "Enter")


def _fetch_results_html(trip: dict) -> str:
    """Drive Playwright through the Costco form and return the results-page HTML."""
    pickup_date = _date_mdy(trip["pickup_date"])
    dropoff_date = _date_mdy(trip["dropoff_date"])
    pickup_time = _time_12h(trip["pickup_time"])
    dropoff_time = _time_12h(trip["dropoff_time"])
    same_location = trip["pickup_location"] == trip["dropoff_location"]

    with sync_playwright() as p:
        browser, context = _new_context(p)
        try:
            page = context.new_page()
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

            # Dates: Costco's widgets accept direct value injection + change event.
            for sel, val in (
                ("#pickUpDateWidget", pickup_date),
                ("#dropOffDateWidget", dropoff_date),
            ):
                page.evaluate(
                    "([sel, val]) => {"
                    "  const el = document.querySelector(sel);"
                    "  if (el) { el.value = val;"
                    "    el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "    el.dispatchEvent(new Event('change', {bubbles: true})); }"
                    "}",
                    [sel, val],
                )

            # Times.
            for sel, val in (
                ("#pickupTimeWidget", pickup_time),
                ("#dropoffTimeWidget", dropoff_time),
            ):
                try:
                    page.select_option(sel, label=val)
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

            # Submit. Costco's submit button is inside the form; selector kept loose.
            page.click(
                "button[type='submit'], "
                "input[type='submit'], "
                "#findMyCarButton, "
                "button:has-text('Search')"
            )

            # Wait for results to render. Try several possible result-container hints.
            try:
                page.wait_for_selector(
                    ".car-class-result, .rate-result, .results, [data-testid*='result'], "
                    "table.results, .vehicle-card",
                    timeout=60000,
                )
            except PlaywrightTimeout:
                # We still want to capture whatever rendered for debugging.
                pass

            page.wait_for_load_state("networkidle", timeout=30000)
            html = page.content()
        finally:
            context.close()
            browser.close()

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
