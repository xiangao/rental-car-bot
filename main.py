"""Daily orchestrator for rental-car-bot.

Pipeline per trip in config/trips.yaml:
  1. Resolve pickup / dropoff to Booking location IDs (cached forever).
  2. Search car rentals via RapidAPI (cached for `cache_ttl_hours`).
  3. Pick cheapest overall + cheapest in requested class.
  4. Compare against 7-day history → alert if >= threshold below average.
  5. Append CSV history, write summary, notify on alert.

After all trips run, render the dashboard HTML and (if `site/` exists as a git
repo) commit + push to GitHub Pages.
"""

import shutil
import subprocess
from pathlib import Path

import yaml

from code import costco_searcher, searcher
from code.analyzer import analyze
from code.html_writer import write_html
from code.notifier import append_to_csv, send_trip_notification, write_summary
from code.searcher import CarRentalResult

PROVIDERS = {
    "booking": searcher.search_trip,
    "costco": costco_searcher.search_trip,
}


BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "trips.yaml"
CSV_PATH = BASE_DIR / "data" / "prices.csv"
OUTPUT_TXT = BASE_DIR / "output" / "latest.txt"
OUTPUT_HTML = BASE_DIR / "output" / "rentals.html"


def main() -> None:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        trips = cfg["trips"]
        search_cfg = cfg["search"]
    except (FileNotFoundError, KeyError, TypeError) as e:
        print(f"ERROR: could not load {CONFIG_PATH}: {e}")
        return

    pairs: list[tuple[CarRentalResult, "AlertResult", dict]] = []     # noqa: F821

    for trip in trips:
        print(f"\n▶ {trip['name']}")
        provider = trip.get("provider", "booking")
        if provider not in PROVIDERS:
            print(f"  ERROR: unknown provider {provider!r}; skipping")
            continue
        try:
            result = PROVIDERS[provider](
                trip,
                cache_ttl_hours=search_cfg.get("cache_ttl_hours", 6),
                top_n=search_cfg.get("top_n_in_html", 5),
            )
        except Exception as exc:                  # noqa: BLE001 — keep one trip's failure from killing others
            print(f"  ERROR ({provider}): {exc}")
            continue

        csv_trip_key = trip.get("csv_name", trip["name"])
        headline = result.cheapest_in_class or result.cheapest
        if headline is None:
            print("  No offers returned.")
            continue

        alert = analyze(
            CSV_PATH,
            csv_trip_key,
            headline.price,
            threshold=search_cfg.get("alert_threshold", 0.10),
            days=search_cfg.get("history_days", 7),
        )

        # Use the configured csv_name for CSV consistency
        result_for_csv = CarRentalResult(
            trip_name=csv_trip_key,
            cheapest=result.cheapest,
            cheapest_in_class=result.cheapest_in_class,
            sampled_offers=result.sampled_offers,
            offer_count=result.offer_count,
        )
        append_to_csv(CSV_PATH, result_for_csv)

        flag = "  ** ALERT **" if alert.should_alert and alert.avg_price > 0 else ""
        print(f"  Cheapest in class: {headline.currency} {headline.price:,.0f}  ({headline.supplier}, {headline.category}){flag}")
        if result.cheapest and result.cheapest is not headline:
            o = result.cheapest
            print(f"  Cheapest overall:  {o.currency} {o.price:,.0f}  ({o.supplier}, {o.category})")

        if alert.should_alert and alert.avg_price > 0:
            send_trip_notification(result, alert)

        pairs.append((result, alert, trip))

    if not pairs:
        print("\nNo successful trips this run.")
        return

    write_summary(OUTPUT_TXT, [(r, a) for r, a, _ in pairs])
    write_html(OUTPUT_HTML, pairs, CSV_PATH)
    print(f"\nSummary  → {OUTPUT_TXT}")
    print(f"Dashboard → {OUTPUT_HTML}")
    _deploy(OUTPUT_HTML)


def _deploy(html_path: Path) -> None:
    site_dir = BASE_DIR / "site"
    if not site_dir.exists():
        print("  (site/ not present — skipping GitHub Pages deploy)")
        return
    if not (site_dir / ".git").exists():
        print("  (site/.git not present — skipping GitHub Pages deploy)")
        return

    shutil.copy(html_path, site_dir / "index.html")
    try:
        subprocess.run(["git", "add", "index.html"], cwd=site_dir, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=site_dir).returncode == 0:
            print("  Site unchanged — nothing to push")
            return
        subprocess.run(
            ["git", "commit", "-m", "Update rental car prices"],
            cwd=site_dir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "gh-pages"],
            cwd=site_dir, check=True, capture_output=True,
        )
        print("  Site deployed to GitHub Pages")
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: deploy failed: {e}")


if __name__ == "__main__":
    main()
