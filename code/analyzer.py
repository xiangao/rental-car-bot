"""Rolling-window price comparison for car-rental quotes.

Mirrors flight-bot/code/analyzer.py: read the last ``days`` of history for a trip
from the CSV, average them, and alert when the latest price drops by at least
``threshold`` (fraction; 0.10 = 10% below the rolling mean).

When fewer than 7 historical samples exist we always alert — the first week of
runs always notifies so the user sees the bot is alive.
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


PRICE_COLUMN = "cheapest_in_class_price"   # falls back to "cheapest_price" if blank
TRIP_COLUMN = "trip"


@dataclass
class AlertResult:
    should_alert: bool
    current_price: float
    avg_price: float
    pct_below: float    # 0.13 means 13% below avg; 0.0 means no comparable history


def _row_price(row: dict) -> float | None:
    raw = row.get(PRICE_COLUMN) or row.get("cheapest_price")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load_recent_prices(csv_path: Path, trip_name: str, days: int = 7) -> list[float]:
    if not csv_path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    prices: list[float] = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            if row.get(TRIP_COLUMN) != trip_name:
                continue
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            price = _row_price(row)
            if price is not None:
                prices.append(price)
    return prices


def analyze(
    csv_path: Path,
    trip_name: str,
    current_price: float | None,
    threshold: float = 0.10,
    days: int = 7,
) -> AlertResult:
    if current_price is None:
        return AlertResult(False, 0.0, 0.0, 0.0)

    recent = load_recent_prices(csv_path, trip_name, days=days)
    if len(recent) < 7:
        # First week: always alert so the user sees the bot is working.
        return AlertResult(True, current_price, 0.0, 0.0)

    avg = sum(recent) / len(recent)
    pct_below = (avg - current_price) / avg if avg > 0 else 0.0
    return AlertResult(
        should_alert=pct_below >= threshold,
        current_price=current_price,
        avg_price=avg,
        pct_below=pct_below,
    )
