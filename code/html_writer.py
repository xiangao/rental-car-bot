"""Render the rental-car dashboard as a single static HTML file.

CSS reuses the look-and-feel of flight-bot for cross-bot visual consistency,
but the card layout is purpose-built for car-rental data (no segments / layovers).
"""

import csv
import html
from datetime import datetime, timedelta
from pathlib import Path

from code.searcher import CarOffer, CarRentalResult


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f0f4fa; color: #222; padding: 24px; max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.3rem; font-weight: 600; margin-bottom: 4px; }
.meta { font-size: 0.85rem; color: #666; margin-bottom: 28px; }

.card { background: #fff; border-radius: 10px;
        box-shadow: 0 1px 4px rgba(0,0,0,.09); margin-bottom: 24px; overflow: hidden; }
.card-header { padding: 16px 20px 12px; border-bottom: 1px solid #f1f5f9; }
.trip-name { font-size: 0.8rem; font-weight: 700; text-transform: uppercase;
             letter-spacing: 0.07em; color: #64748b; }
.trip-detail { font-size: 0.95rem; color: #475569; margin-top: 2px; }
.card-body { padding: 16px 20px 20px; }

.headline { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
.headline .price { font-size: 1.9rem; font-weight: 700; color: #0f172a; }
.headline .supplier { font-size: 1rem; color: #334155; }
.headline .rating { font-size: 0.85rem; color: #b45309; }
.subline { font-size: 0.9rem; color: #475569; margin-bottom: 6px; }
.cls-line { font-size: 0.85rem; color: #475569; }
.alert-badge { display: inline-block; background: #fef3c7; color: #92400e;
               border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: 600;
               margin-left: 8px; }

table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 0.88rem; }
th, td { padding: 7px 8px; text-align: left; border-bottom: 1px solid #f1f5f9; }
th { color: #64748b; font-weight: 600; font-size: 0.75rem; text-transform: uppercase;
     letter-spacing: 0.04em; }
td.price-cell { font-weight: 600; color: #0f172a; }
td.low { background: #ecfeff; color: #155e75; }
td.empty { color: #cbd5e1; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }

.section-title { font-size: 0.75rem; font-weight: 700; text-transform: uppercase;
                 letter-spacing: 0.06em; color: #64748b; margin: 18px 0 4px; }
"""


def _esc(s: str | None) -> str:
    return html.escape(str(s)) if s is not None else ""


def _fmt_price(offer: CarOffer | None) -> str:
    if offer is None:
        return "—"
    return f"{offer.currency} {offer.price:,.0f}"


def _offer_table(offers: list[CarOffer]) -> str:
    if not offers:
        return '<p style="color:#94a3b8;font-size:0.85rem;margin-top:14px;">No offers in this run.</p>'
    rows = ['<table><tr><th>Price</th><th>Supplier</th><th>Class</th><th>Vehicle</th><th>Mileage</th><th></th></tr>']
    for o in offers:
        link = f'<a href="{_esc(o.deep_link)}" target="_blank" rel="noopener">Book →</a>' if o.deep_link else ""
        rows.append(
            "<tr>"
            f'<td class="price-cell">{o.currency} {o.price:,.0f}</td>'
            f'<td>{_esc(o.supplier)}</td>'
            f'<td>{_esc(o.category)}</td>'
            f'<td>{_esc(o.vehicle_example)}</td>'
            f'<td>{_esc(o.mileage)}</td>'
            f'<td>{link}</td>'
            "</tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def _history_for_trip(csv_path: Path, trip_name: str, days: int = 30) -> list[dict]:
    if not csv_path.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    by_day: dict[str, dict] = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            if row.get("trip") != trip_name:
                continue
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            day = ts.date().isoformat()
            existing = by_day.get(day)
            if existing is None or ts > datetime.fromisoformat(existing["timestamp"]):
                by_day[day] = row
    return sorted(by_day.values(), key=lambda r: r["timestamp"], reverse=True)


def _history_table(rows: list[dict]) -> str:
    if not rows:
        return '<p style="color:#94a3b8;font-size:0.85rem;">No history yet.</p>'

    def _to_float(s: str | None) -> float | None:
        try:
            return float(s) if s else None
        except ValueError:
            return None

    overall_prices = [p for r in rows if (p := _to_float(r.get("cheapest_price"))) is not None]
    inclass_prices = [p for r in rows if (p := _to_float(r.get("cheapest_in_class_price"))) is not None]
    lo_overall = min(overall_prices) if overall_prices else None
    lo_inclass = min(inclass_prices) if inclass_prices else None

    out = ['<table><tr><th>Date</th><th>Cheapest in class</th><th>Cheapest overall</th><th>Supplier</th></tr>']
    for r in rows[:30]:
        date = r["timestamp"][:10]
        c_in = _to_float(r.get("cheapest_in_class_price"))
        c_ov = _to_float(r.get("cheapest_price"))
        currency = _esc(r.get("currency", ""))
        c_in_html = (
            f'<td class="price-cell {"low" if lo_inclass is not None and c_in <= lo_inclass else ""}">'
            f"{currency} {c_in:,.0f}</td>"
        ) if c_in is not None else '<td class="empty">—</td>'
        c_ov_html = (
            f'<td class="price-cell {"low" if lo_overall is not None and c_ov <= lo_overall else ""}">'
            f"{currency} {c_ov:,.0f}</td>"
        ) if c_ov is not None else '<td class="empty">—</td>'
        supplier = _esc(r.get("cheapest_in_class_supplier") or r.get("cheapest_supplier", ""))
        out.append(f"<tr><td>{date}</td>{c_in_html}{c_ov_html}<td>{supplier}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def _render_card(result: CarRentalResult, trip_cfg: dict, alert_pct_below: float, csv_path: Path) -> str:
    in_class = result.cheapest_in_class
    overall = result.cheapest
    headline = in_class or overall

    dates = f"{trip_cfg['pickup_date']} → {trip_cfg['dropoff_date']}"
    locs = (
        f"{trip_cfg['pickup_location']} → {trip_cfg['dropoff_location']}"
        if trip_cfg['pickup_location'] != trip_cfg['dropoff_location']
        else trip_cfg['pickup_location']
    )
    detail = f"{locs}  ·  {dates}  ·  driver age {trip_cfg['driver_age']}  ·  class: {trip_cfg.get('car_class', 'any')}"

    alert_badge = (
        f'<span class="alert-badge">★ {alert_pct_below * 100:.0f}% below 7d avg</span>'
        if alert_pct_below >= 0.10
        else ""
    )

    if headline is None:
        body = '<p style="color:#dc2626;">No offers returned for this trip.</p>'
    else:
        rating = f'<span class="rating">★{headline.rating:.1f}</span>' if headline.rating else ""
        cls_line = ""
        if in_class and overall and in_class is not overall:
            cls_line = (
                f'<div class="cls-line">Cheapest overall: '
                f"{overall.currency} {overall.price:,.0f} "
                f"({_esc(overall.supplier)} — {_esc(overall.category)})</div>"
            )
        elif in_class is None and overall is not None:
            cls_line = (
                '<div class="cls-line" style="color:#92400e;">'
                "No offer matched the requested class; showing cheapest overall.</div>"
            )
        body = (
            f'<div class="headline">'
            f'<span class="price">{_fmt_price(headline)}</span>'
            f'<span class="supplier">{_esc(headline.supplier)}</span>{rating}{alert_badge}'
            "</div>"
            f'<div class="subline">{_esc(headline.category)}'
            f"{' — ' + _esc(headline.vehicle_example) if headline.vehicle_example else ''}"
            f"  ·  {_esc(headline.transmission)}  ·  {_esc(headline.mileage)}"
            f"  ·  {_esc(headline.cancellation)}</div>"
            f"{cls_line}"
            f'<div class="section-title">Top offers this run</div>'
            f"{_offer_table(result.sampled_offers)}"
            f'<div class="section-title">Price history</div>'
            f"{_history_table(_history_for_trip(csv_path, result.trip_name))}"
        )

    return (
        '<div class="card">'
        '<div class="card-header">'
        f'<div class="trip-name">{_esc(result.trip_name)}</div>'
        f'<div class="trip-detail">{_esc(detail)}</div>'
        "</div>"
        f'<div class="card-body">{body}</div>'
        "</div>"
    )


def write_html(
    output_path: Path,
    pairs: list[tuple[CarRentalResult, "AlertResult", dict]],   # noqa: F821 — AlertResult typed lazily
    csv_path: Path,
) -> None:
    """Write the dashboard HTML.

    ``pairs`` items are ``(result, alert, trip_cfg)`` triples; the trip_cfg
    contributes pickup/dropoff dates + class for the card header.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "\n".join(
        _render_card(result, trip_cfg, alert.pct_below if alert.avg_price > 0 else 0.0, csv_path)
        for result, alert, trip_cfg in pairs
    )
    output_path.write_text(
        "<!doctype html>\n<html lang='en'>\n<head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Rental car prices</title>"
        f"<style>{_CSS}</style>"
        "</head><body>"
        "<h1>🚗 Rental car price monitor</h1>"
        f"<div class='meta'>Updated {now}</div>"
        f"{cards}"
        "</body></html>\n"
    )
