"""CSV history, plaintext summary, and desktop notifications for the bot."""

import csv
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from code.analyzer import AlertResult
from code.searcher import CarOffer, CarRentalResult


CSV_FIELDS = [
    "timestamp",
    "trip",
    "cheapest_price",
    "cheapest_supplier",
    "cheapest_category",
    "cheapest_vehicle",
    "cheapest_in_class_price",
    "cheapest_in_class_supplier",
    "cheapest_in_class_category",
    "cheapest_in_class_vehicle",
    "currency",
    "offer_count",
]


def _row_for(result: CarRentalResult) -> dict:
    overall = result.cheapest
    in_class = result.cheapest_in_class
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "trip": result.trip_name,
        "cheapest_price":    f"{overall.price:.2f}" if overall else "",
        "cheapest_supplier": overall.supplier if overall else "",
        "cheapest_category": overall.category if overall else "",
        "cheapest_vehicle":  overall.vehicle_example if overall else "",
        "cheapest_in_class_price":    f"{in_class.price:.2f}" if in_class else "",
        "cheapest_in_class_supplier": in_class.supplier if in_class else "",
        "cheapest_in_class_category": in_class.category if in_class else "",
        "cheapest_in_class_vehicle":  in_class.vehicle_example if in_class else "",
        "currency":     (overall or in_class).currency if (overall or in_class) else "",
        "offer_count":  result.offer_count,
    }


def append_to_csv(csv_path: Path, result: CarRentalResult) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(_row_for(result))


# ────────────────────────────────────────────────────────────────────────────────

def _format_offer_line(prefix: str, offer: CarOffer) -> str:
    rating = f" ★{offer.rating:.1f}" if offer.rating is not None else ""
    return (
        f"  {prefix}{offer.currency} {offer.price:,.2f}  "
        f"{offer.supplier}{rating}  —  {offer.category}"
        f"{' (' + offer.vehicle_example + ')' if offer.vehicle_example else ''}"
    )


def write_summary(
    output_path: Path,
    pairs: list[tuple[CarRentalResult, AlertResult]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"Rental Car Price Report — {now}", ""]
    for result, alert in pairs:
        lines.append(f"=== {result.trip_name} — {result.offer_count} offers ===")
        if result.cheapest:
            lines.append(_format_offer_line("Cheapest overall : ", result.cheapest))
        if result.cheapest_in_class:
            lines.append(_format_offer_line("Cheapest in class: ", result.cheapest_in_class))
        if alert.avg_price > 0:
            lines.append(
                f"  Avg(7d in-class): {alert.avg_price:,.2f}"
                f"  ({alert.pct_below * 100:+.1f}%)"
            )
        if alert.should_alert and alert.avg_price > 0:
            lines.append("  ** ALERT: notable drop **")
        elif alert.should_alert:
            lines.append("  (first-week run; alerts always fire until 7 samples)")
        lines.append("")
    output_path.write_text("\n".join(lines))


# ────────────────────────────────────────────────────────────────────────────────

def _check_notify_send() -> bool:
    if shutil.which("notify-send"):
        return True
    print("WARNING: notify-send not available, skipping desktop notification")
    return False


def send_trip_notification(result: CarRentalResult, alert: AlertResult) -> None:
    if not _check_notify_send():
        return

    headline_offer = result.cheapest_in_class or result.cheapest
    if headline_offer is None:
        return

    lines: list[str] = []
    if alert.avg_price > 0 and alert.should_alert:
        lines.append(
            f"{headline_offer.currency} {headline_offer.price:,.0f}"
            f" — {alert.pct_below * 100:.0f}% below 7d avg"
            f" ({alert.avg_price:,.0f})"
        )
    else:
        lines.append(f"{headline_offer.currency} {headline_offer.price:,.0f}")
    lines.append(
        f"{headline_offer.supplier} — {headline_offer.category}"
        f"{' (' + headline_offer.vehicle_example + ')' if headline_offer.vehicle_example else ''}"
    )
    if (
        result.cheapest
        and result.cheapest is not headline_offer
        and result.cheapest.price < headline_offer.price
    ):
        lines.append(
            f"Cheapest overall: {result.cheapest.currency} {result.cheapest.price:,.0f}"
            f"  {result.cheapest.supplier} — {result.cheapest.category}"
        )

    subprocess.run(
        ["notify-send", f"🚗 {result.trip_name}", "\n".join(lines), "--urgency=normal"],
        check=False,
    )
