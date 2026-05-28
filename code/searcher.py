"""RapidAPI booking-com15 car-rental client.

Two endpoints:
  - searchDestination: resolves a free-text/IATA pickup/dropoff string to a Booking location id
  - searchCarRentals : returns car-rental offers for a (pickup, dropoff, datetime, age) tuple

Locations are cached forever (IATA codes are stable). Offer searches use a TTL cache
to avoid burning quota on manual re-runs.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

API_HOST = "booking-com15.p.rapidapi.com"
DEST_URL = f"https://{API_HOST}/api/v1/cars/searchDestination"
SEARCH_URL = f"https://{API_HOST}/api/v1/cars/searchCarRentals"

CACHE_DIR = BASE_DIR / "data" / "api_cache"
LOCATIONS_CACHE = CACHE_DIR / "locations.json"
DEBUG_DIR = BASE_DIR / "data" / "debug"   # raw responses for one-off inspection

# Substring fragments that mark a Booking category as a midsize SUV / etc.
# Booking categories look like "Intermediate SUV", "Standard SUV", "Compact",
# "Mini", "Economy", "Full-Size", "Premium SUV", "Minivan", ... — substring match,
# case-insensitive, first hit wins per offer. Add more fragments as we see them.
CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "economy":     ("economy",),
    "compact":     ("compact", "mini"),
    "midsize":     ("midsize", "intermediate"),
    "fullsize":    ("full-size", "fullsize", "standard"),
    "suv":         ("suv",),
    "midsize_suv": ("intermediate suv", "standard suv", "midsize suv"),
    "minivan":     ("minivan", "people carrier", "mpv"),
    "any":         (),   # match everything
}


# ────────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────────

@dataclass
class CarOffer:
    price: float
    currency: str
    supplier: str
    category: str
    vehicle_example: str
    transmission: str
    fuel_policy: str
    mileage: str
    pickup_location: str
    dropoff_location: str
    rating: float | None
    cancellation: str
    deep_link: str


@dataclass
class CarRentalResult:
    trip_name: str
    cheapest: CarOffer | None                  # cheapest across the entire response
    cheapest_in_class: CarOffer | None          # cheapest matching trip["car_class"], or None
    sampled_offers: list[CarOffer] = field(default_factory=list)
    offer_count: int = 0


# ────────────────────────────────────────────────────────────────────────────────
# HTTP / cache helpers
# ────────────────────────────────────────────────────────────────────────────────

def _api_key() -> str:
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        raise RuntimeError("RAPIDAPI_KEY is not set; copy .env.example to .env and paste your key")
    return key


def _headers() -> dict[str, str]:
    return {"x-rapidapi-key": _api_key(), "x-rapidapi-host": API_HOST}


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


def _save_debug(name: str, payload: dict) -> None:
    """Persist raw API responses on first encounter so we can tune field paths."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    target = DEBUG_DIR / f"{name}.json"
    if not target.exists():
        target.write_text(json.dumps(payload, indent=2))


def _get(url: str, params: dict[str, Any]) -> dict:
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ────────────────────────────────────────────────────────────────────────────────
# Location resolution
# ────────────────────────────────────────────────────────────────────────────────

def _load_location_cache() -> dict[str, dict]:
    if LOCATIONS_CACHE.exists():
        try:
            return json.loads(LOCATIONS_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_location_cache(cache: dict[str, dict]) -> None:
    LOCATIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LOCATIONS_CACHE.write_text(json.dumps(cache, indent=2))


def resolve_location(query: str) -> dict:
    """Return Booking's location record for ``query``. Caches permanently."""
    cache = _load_location_cache()
    if query in cache:
        return cache[query]

    payload = _get(DEST_URL, {"query": query})
    _save_debug("searchDestination", payload)

    items = payload.get("data") or []
    if not items:
        raise RuntimeError(f"searchDestination returned no results for {query!r}")

    # Prefer airports when the query looks like an IATA code (3 uppercase letters).
    if len(query) == 3 and query.isupper():
        airports = [it for it in items if str(it.get("type", "")).lower() == "airport"]
        if airports:
            items = airports

    chosen = items[0]
    cache[query] = chosen
    _save_location_cache(cache)
    return chosen


# ────────────────────────────────────────────────────────────────────────────────
# Car-rental search
# ────────────────────────────────────────────────────────────────────────────────

def _build_search_params(trip: dict, pickup_loc: dict, dropoff_loc: dict, currency: str) -> dict:
    """Translate a trip dict into the searchCarRentals query parameters."""
    return {
        "pick_up_latitude":   pickup_loc.get("latitude")  or pickup_loc.get("coordinates", {}).get("latitude"),
        "pick_up_longitude":  pickup_loc.get("longitude") or pickup_loc.get("coordinates", {}).get("longitude"),
        "drop_off_latitude":  dropoff_loc.get("latitude")  or dropoff_loc.get("coordinates", {}).get("latitude"),
        "drop_off_longitude": dropoff_loc.get("longitude") or dropoff_loc.get("coordinates", {}).get("longitude"),
        "pick_up_date":  trip["pickup_date"],
        "drop_off_date": trip["dropoff_date"],
        "pick_up_time":  trip["pickup_time"],
        "drop_off_time": trip["dropoff_time"],
        "driver_age":    str(trip["driver_age"]),
        "currency_code": currency,
    }


def _g(d: dict | None, *keys: str, default=None):
    """Nested dict getter; returns ``default`` on the first missing key."""
    cur: Any = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _parse_offer(item: dict, pickup_name: str, dropoff_name: str, currency: str) -> CarOffer | None:
    """Map a raw search-result item to a CarOffer; returns None when no usable price."""
    # Booking responses on this RapidAPI wrapper have varied a bit over time; try a
    # handful of likely shapes before giving up. All `_g` paths default to None,
    # so missing nodes don't raise.
    price = (
        _g(item, "pricing_info", "drive_away_price")
        or _g(item, "pricing_info", "price")
        or _g(item, "price")
        or _g(item, "pricing", "driveAwayPrice")
        or _g(item, "pricing", "price")
    )
    if price is None:
        return None
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return None

    supplier = (
        _g(item, "supplier_info", "name")
        or _g(item, "supplier", "name")
        or _g(item, "supplier")
        or "Unknown"
    )

    category = (
        _g(item, "vehicle_info", "group")
        or _g(item, "vehicle_info", "category")
        or _g(item, "vehicle", "v_group")
        or _g(item, "vehicle", "group")
        or _g(item, "category")
        or "Unknown"
    )

    vehicle_example = (
        _g(item, "vehicle_info", "v_name")
        or _g(item, "vehicle_info", "name")
        or _g(item, "vehicle", "v_name")
        or _g(item, "vehicle", "name")
        or ""
    )

    transmission = (
        _g(item, "vehicle_info", "transmission")
        or _g(item, "vehicle", "transmission")
        or "Unknown"
    )

    fuel_policy = (
        _g(item, "rental_conditions", "fuel_policy")
        or _g(item, "vehicle_info", "fuel_policy")
        or "Unknown"
    )

    mileage = (
        _g(item, "rental_conditions", "mileage")
        or _g(item, "vehicle_info", "mileage")
        or "Unknown"
    )

    rating_raw = _g(item, "supplier_info", "rating") or _g(item, "supplier", "rating")
    try:
        rating = float(rating_raw) if rating_raw is not None else None
    except (TypeError, ValueError):
        rating = None

    cancellation = (
        _g(item, "rental_conditions", "cancellation_policy")
        or _g(item, "free_cancellation")
        or "See provider"
    )

    deep_link = (
        _g(item, "forward_url")
        or _g(item, "deep_link")
        or _g(item, "pricing_info", "url")
        or ""
    )

    return CarOffer(
        price=price_f,
        currency=currency,
        supplier=str(supplier),
        category=str(category),
        vehicle_example=str(vehicle_example),
        transmission=str(transmission),
        fuel_policy=str(fuel_policy),
        mileage=str(mileage),
        pickup_location=pickup_name,
        dropoff_location=dropoff_name,
        rating=rating,
        cancellation=str(cancellation),
        deep_link=str(deep_link),
    )


def _matches_class(offer: CarOffer, car_class: str) -> bool:
    fragments = CLASS_KEYWORDS.get(car_class.lower(), ())
    if not fragments:                    # "any" or unknown class → match all
        return True
    blob = f"{offer.category} {offer.vehicle_example}".lower()
    return any(frag in blob for frag in fragments)


def search_trip(trip: dict, *, cache_ttl_hours: int, top_n: int = 5) -> CarRentalResult:
    """Resolve locations, fetch offers, and pick cheapest overall + cheapest-in-class."""
    currency = trip.get("currency", "USD")
    pickup_loc = resolve_location(trip["pickup_location"])
    dropoff_loc = resolve_location(trip["dropoff_location"])

    params = _build_search_params(trip, pickup_loc, dropoff_loc, currency)
    cache_path = _cache_path("rentals", {**params, "currency": currency})
    payload = _read_cache(cache_path, cache_ttl_hours * 3600) if cache_ttl_hours else None
    if payload is None:
        payload = _get(SEARCH_URL, params)
        _write_cache(cache_path, payload)
    _save_debug("searchCarRentals", payload)

    raw_items = (
        _g(payload, "data", "search_results")
        or _g(payload, "data", "results")
        or _g(payload, "data")
        or []
    )
    if not isinstance(raw_items, list):
        raw_items = []

    pickup_name = pickup_loc.get("name") or trip["pickup_location"]
    dropoff_name = dropoff_loc.get("name") or trip["dropoff_location"]

    offers: list[CarOffer] = []
    for item in raw_items:
        parsed = _parse_offer(item, pickup_name, dropoff_name, currency)
        if parsed is not None:
            offers.append(parsed)
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


# Convenience for ad-hoc debugging from the REPL: `python -m code.searcher`
if __name__ == "__main__":
    import yaml
    with open(BASE_DIR / "config" / "trips.yaml") as f:
        cfg = yaml.safe_load(f)
    for trip in cfg["trips"]:
        result = search_trip(trip, cache_ttl_hours=0)
        print(f"\n=== {result.trip_name} — {result.offer_count} offers ===")
        if result.cheapest:
            o = result.cheapest
            print(f"Cheapest overall:  {o.currency} {o.price:.2f}  {o.supplier}  {o.category}  ({o.vehicle_example})")
        if result.cheapest_in_class:
            o = result.cheapest_in_class
            print(f"Cheapest in class: {o.currency} {o.price:.2f}  {o.supplier}  {o.category}  ({o.vehicle_example})")
