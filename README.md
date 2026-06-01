# rental-car-bot

Daily rental-car price monitor. Tracks one or more trips via the Booking.com Cars
API on RapidAPI, publishes an HTML dashboard to GitHub Pages, sends a desktop
notification when the cheapest in-class quote drops noticeably below its 7-day
average, and stores the full price history as CSV.

Sibling project to `flight-bot` and `house-bot` — same daily-run + gh-pages +
notify-send + systemd-timer pattern.

**Live site:** https://xiangao.github.io/rental-car-bot-site/ (after you bootstrap `site/`)

> **Status (2026-06-01):** the active trip uses the **Costco** provider
> (`config/trips.yaml`) because the booking-com15 API was erroring in late May.
> Costco is behind Akamai Bot Manager, which serves *launched* automation
> browsers a non-functional search form. The scraper now drives a **real,
> headful Google Chrome over CDP** (auto-launched on `:9222`), which passes
> Akamai — verified returning 27 offers. **Requires an active desktop (X)
> session when it runs.** See the `CLAUDE.md` Costco "2026-06-01" note for details.

## Setup

### 1. RapidAPI key

1. Create an account at https://rapidapi.com.
2. Subscribe to **Booking COM** by DataCrawler:
   https://rapidapi.com/DataCrawler/api/booking-com15/
   The free tier (Basic) is normally enough — one trip × one daily run uses about
   60 calls/month (one location-resolve cached forever + one car search/day).
3. Copy `.env.example` to `.env` and paste your key:

   ```
   RAPIDAPI_KEY=your-key-here
   ```

### 2. Install dependencies

```bash
cd ~/projects/claude/rental-car-bot
test -d .venv || python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure trips

Edit `config/trips.yaml`. Each trip needs pickup/dropoff codes, dates, times,
driver age, and an optional `car_class` filter. Add as many trip blocks as you
want — they all share the same alert threshold and history window.

The `csv_name` field is the stable key used in `data/prices.csv`; never rename
it after the first run or you'll lose history continuity.

Valid `car_class` values (Booking matches by substring on its category strings;
unknown values fall back to "any" and match every offer):

| Value         | Matches Booking categories containing…              |
| ------------- | --------------------------------------------------- |
| `economy`     | "economy"                                           |
| `compact`     | "compact", "mini"                                   |
| `midsize`     | "midsize", "intermediate"                           |
| `fullsize`    | "full-size", "fullsize", "standard"                 |
| `suv`         | "suv"                                               |
| `midsize_suv` | "intermediate suv", "standard suv", "midsize suv"   |
| `minivan`     | "minivan", "people carrier", "mpv"                  |
| `any`         | any (no filter)                                     |

To tune the substring map after seeing real responses, edit `CLASS_KEYWORDS`
in `code/searcher.py`.

### 4. First manual run

```bash
source .venv/bin/activate
python main.py
```

You should see:

- `data/prices.csv` — appended one row per trip
- `output/latest.txt` — plaintext summary
- `output/rentals.html` — the dashboard (open in browser)
- `data/api_cache/` — raw responses cached for 6 hours
- `data/debug/` — first-encounter raw JSON for each endpoint (one-time, for
  field-path tuning)
- A desktop notification per trip (first week always alerts)

### 5. GitHub Pages site (optional but matches flight-bot/house-bot)

Create an empty `xiangao/rental-car-bot-site` repo on GitHub, then:

```bash
cd ~/projects/claude/rental-car-bot
mkdir site && cd site
git init
git checkout -b gh-pages
git remote add origin git@github.com:xiangao/rental-car-bot-site.git
cd ..
python main.py   # writes site/index.html and pushes
```

Enable GitHub Pages → Source = `gh-pages` branch in the site repo's Settings.

### 6. Daily schedule (systemd timer)

```bash
cp rental-car-bot.service rental-car-bot.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rental-car-bot.timer
systemctl --user list-timers | grep rental-car-bot
```

Logs:

```bash
journalctl --user -u rental-car-bot.service -e
```

## How the alert threshold works

The bot alerts when the **cheapest-in-class** price (or cheapest overall if no
class filter) is ≥ `search.alert_threshold` (default 10%) below the rolling
`search.history_days`-day average for that trip. For the first 7 days of
history the bot always alerts so you can see it's running. Tune both thresholds
in `config/trips.yaml`.

## What's in each directory

```
code/        searcher.py · analyzer.py · notifier.py · html_writer.py
config/      trips.yaml (your trips + search settings)
data/        prices.csv (history) · api_cache/ (TTL'd JSON) · debug/ (first responses)
output/      latest.txt · rentals.html
site/        gh-pages sibling repo (you create after first run)
```

## Costs

Booking.com 15 (RapidAPI) free tier is typically 500 requests/month. One
trip-per-day uses ~30 calls/month (1 search/day × 1 trip; resolveLocation is
cached forever after the first run). Adding trips scales linearly.

## Related bots

- `~/projects/claude/flight-bot/`  → flight prices (SerpAPI + Ignav)
- `~/projects/claude/house-bot/`   → Redfin listings
