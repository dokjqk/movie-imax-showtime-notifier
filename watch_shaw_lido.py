#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""
watch_shaw_lido.py

Polls Shaw Theatres' own backend (shaw.sg) directly -- no third-party
mirror involved -- for Lido IMAX showtimes, diffs against the previous run,
and pushes an ntfy.sh notification the moment new showtimes appear (or, by
default, also when an existing showtime's availability status changes).

Meant to run on a schedule (cron / Task Scheduler) every ~30 min. Each run
is a single process that exits; state persists in a small JSON file
(seen_state.json) next to this script.

--- Reverse-engineering notes (Step 0), recovered 2026-07-23 ---

shaw.sg is a Next.js app. Its showtimes page fetches data client-side via
React Query, which calls a small same-origin proxy:

    GET https://shaw.sg/internal/get_show_times
        ?date=YYYY-MM-DD      (required)
        &locationId=<int>     (required; Lido = 1, see LIDO_LOCATION_ID)
        &movieId=<int>        (optional; 0 or omitted = every movie)
        &promotionId=<int>    (optional; 0 = none)
    Required headers:
        x-api-forward-to: internal
        x-app: PWSM
    No cookies/session/Referer needed -- this route is a public passthrough
    to Shaw's real backend (snow-pwsm-legacy.sice.tech); shaw.sg's own
    server injects whatever auth that backend wants server-side, so no
    two-step "load the HTML page for cookies first" dance was necessary.

Response: JSON array of movies, each with a `showTimes` array, e.g.:
    [{
      "movieId": 1238, "primaryTitle": "The Odyssey", ...,
      "showTimes": [{
        "performanceId": 513029,
        "displayDate": "2026-07-27", "displayTime": "1:00 PM",
        "locationVenueName": "Lido IMAX",
        "locationVenueBrandCode": "IMAX",   # <- how IMAX is denoted
        "seatingStatus": "AV",              # AV=Available, SF=Selling Fast,
                                             # SO=Sold Out -- confirmed from
                                             # the frontend's own status
                                             # enum, so Shaw hands us a
                                             # status directly; no need to
                                             # derive it from seat counts.
        ...
      }]
    }]

Booking URL = f"https://shaw.sg/showtimes/{performanceId}" (verified live --
performanceId 513029 from the build spec's example is real).

Exact seat counts are NOT in get_show_times; they need a second,
per-performance call:

    GET https://shaw.sg/internal/get_layouts?performanceId=<id>
    (same two headers as above)

which returns every element of the physical seat map. Elements with
elementCategoryCode == "SEAT" are real seats; elementStatusCodeCurrent is
"AV" (available), "SO" (sold), or "BL" (blocked/held, not for public sale).
seats_total = count of SEAT elements, seats_available = count with status
"AV" (verified against performanceId 513029: 413 total seats, exactly
matching the build spec's example).

Lido's numeric locationId (1), the location-code table, and the movie
slug -> movieId convention (shaw.sg URLs like
".../showtimes?movie=The+Odyssey_1238" -- the digits after the last "_"
*are* the movieId) were all recovered from shaw.sg's own webpack bundle
(app/(showtime)/showtimes/page-*.js and its shared chunks), not guessed.
Since the API can be queried with no movie filter at all (movieId=0/omit),
this script never resolves a movie name to an ID -- it fetches everything
showing at Lido and filters by name client-side instead.
---------------------------------------------------------------------------

Setup (hosted, recommended -- runs on GitHub's schedule, not your PC, and
publishes a status page via GitHub Pages): see README.md.

Setup (local, run-it-yourself instead):
  1. Install uv if you don't have it already: https://docs.astral.sh/uv/
  2. Pick an ntfy.sh topic only you know, e.g. "john-shaw-lido-8f2k", and
     open https://ntfy.sh/<topic> in a browser or the ntfy app to watch it.
  3. Test it once manually:
       uv run watch_shaw_lido.py --once -v --topic john-shaw-lido-8f2k
  4. Schedule it yourself (every 30 min, 7am-11pm SGT; uv reads the
     dependency block above itself, no separate install step):
       Linux/macOS (cron):
         */30 7-23 * * * cd /path/to/project && uv run watch_shaw_lido.py --topic john-shaw-lido-8f2k
       Windows Task Scheduler:
         Program:  uv
         Args:     run watch_shaw_lido.py --topic john-shaw-lido-8f2k
         Start in: C:\\path\\to\\project
         Trigger:  every 30 minutes, 7:00 AM - 11:00 PM
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# Configuration - edit these, or override via CLI flags
# ----------------------------------------------------------------------------
API_BASE = "https://shaw.sg/internal"
API_HEADERS = {"x-api-forward-to": "internal", "x-app": "PWSM"}

NTFY_TOPIC = "TOPIC_REPLACE_ME"  # e.g. "john-shaw-lido-8f2k"; see README for setup
NTFY_SERVER = "https://ntfy.sh"

LIDO_LOCATION_ID = 1           # numeric id from GET /internal/get_simple_locations
IMAX_BRAND_CODE = "IMAX"       # locationVenueBrandCode value for Lido IMAX

DATE_START = date(2026, 7, 27)
LOOKAHEAD_DAYS = 21             # query this many days past max(DATE_START, today);
                                # the window's far edge slides forward as "today"
                                # advances, so newly-released weeks are picked up
                                # automatically -- no end-date constant to bump
                                # (see BUILD_SPEC step 2)

MOVIE_FILTER = None             # e.g. "The Odyssey"; None = any movie

# Fallback thresholds, only used on the rare showtime where Shaw's own
# seatingStatus is missing/unrecognized (currently it never is -- see notes
# above -- but keep this configurable per BUILD_SPEC step 0.4).
AVAILABLE_PCT = 20.0
SELLING_FAST_PCT = 5.0

STATE_FILE = Path(__file__).parent / "seen_state.json"
STATUS_PAGE_FILE = Path(__file__).parent / "docs" / "index.html"

STATUS_MAP = {"AV": "Available", "SF": "Selling Fast", "SO": "Sold Out"}

# Loosely based on Shaw's own showtimes-page legend (grey/green/red), shifted
# for legible white text on the badge rather than matching hex-for-hex.
STATUS_COLORS = {"Available": "#8a8a8a", "Selling Fast": "#2b8a3e", "Sold Out": "#e03131", "Unknown": "#868e96"}

SHOWTIMES_PAGE_URL = "https://shaw.sg/showtimes"

# ----------------------------------------------------------------------------


def _get(path, params, verbose=False, retries=2):
    url = f"{API_BASE}{path}"
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=API_HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if verbose:
                print(f"[fetch] {url} params={params} attempt={attempt} failed: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Shaw API request failed after {retries + 1} attempt(s): {url} params={params}") from last_exc


def _to_24h(display_time):
    """'8:30 PM' -> '20:30'."""
    return datetime.strptime(display_time, "%I:%M %p").strftime("%H:%M")


def fetch_seat_counts(performance_id, verbose=False):
    """Return (seats_available, seats_total) via the per-performance seat map."""
    try:
        elements = _get("/get_layouts", {"performanceId": performance_id}, verbose=verbose, retries=1)
    except RuntimeError as exc:
        if verbose:
            print(f"[seats] performance {performance_id}: {exc}", file=sys.stderr)
        return None, None

    seats = [e for e in elements if e.get("elementCategoryCode") == "SEAT"]
    total = len(seats)
    available = sum(1 for s in seats if s.get("elementStatusCodeCurrent") == "AV")
    return available, total


def derive_status(seats_available, seats_total):
    if not seats_total:
        return "Sold Out"
    pct = seats_available / seats_total * 100
    if pct > AVAILABLE_PCT:
        return "Available"
    if pct > SELLING_FAST_PCT:
        return "Selling Fast"
    return "Sold Out"


def fetch_showtimes(movie_filter=MOVIE_FILTER, date_start=DATE_START, lookahead_days=LOOKAHEAD_DAYS,
                     fetch_seats=True, verbose=False):
    """Query Shaw's real API for every Lido IMAX showtime in the target window."""
    window_start = max(date_start, date.today())
    window_end = window_start + timedelta(days=lookahead_days)

    results = {}  # keyed by performanceId; dedupes late-night shows returned by two query dates
    total_seen_at_lido = 0

    d = window_start
    while d <= window_end:
        movies = _get("/get_show_times", {"date": d.isoformat(), "locationId": LIDO_LOCATION_ID}, verbose=verbose)
        if not isinstance(movies, list):
            raise RuntimeError(f"Unexpected response shape from Shaw API for date={d}: {type(movies)}")

        for movie in movies:
            title = movie.get("primaryTitle", "")
            if movie_filter and movie_filter.lower() not in title.lower():
                continue
            for st in movie.get("showTimes", []):
                total_seen_at_lido += 1
                if st.get("locationVenueBrandCode") != IMAX_BRAND_CODE:
                    continue

                showtime_date = date.fromisoformat(st["displayDate"])
                if showtime_date < date_start:
                    continue

                perf_id = st["performanceId"]
                if perf_id in results:
                    continue  # already captured from an earlier query date

                seats_available = seats_total = None
                if fetch_seats:
                    seats_available, seats_total = fetch_seat_counts(perf_id, verbose=verbose)

                if st.get("seatingStatus") in STATUS_MAP:
                    status = STATUS_MAP[st["seatingStatus"]]
                elif seats_total:
                    status = derive_status(seats_available, seats_total)
                else:
                    status = "Unknown"

                pct = round(seats_available / seats_total * 100, 1) if seats_total else None

                results[perf_id] = {
                    "key": str(perf_id),
                    "date": str(showtime_date),
                    "movie": title,
                    "venue": st.get("locationVenueName", "Lido IMAX"),
                    "time": _to_24h(st["displayTime"]),
                    "seats_available": seats_available,
                    "seats_total": seats_total,
                    "availability_pct": pct,
                    "status": status,
                    "booking_url": f"https://shaw.sg/showtimes/{perf_id}",
                }
        d += timedelta(days=1)

    if total_seen_at_lido == 0:
        raise RuntimeError(
            "Shaw API returned zero showtimes at Lido across the entire query window "
            f"({window_start} to {window_end}). This almost certainly means the API shape "
            "changed or the request is being blocked -- not that Lido has no movies -- so "
            "refusing to report a false 'no showtimes' result."
        )

    rows = sorted(results.values(), key=lambda r: (r["date"], r["time"]))
    if verbose:
        print(f"[fetch] window={window_start}..{window_end} lido_total={total_seen_at_lido} lido_imax={len(rows)}",
              file=sys.stderr)
    return rows


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(rows):
    # rows is already sorted by (date, time) -- keep that order in the file
    # instead of alphabetizing by performanceId key.
    STATE_FILE.write_text(json.dumps({r["key"]: r for r in rows}, indent=2))


def generate_status_page(rows, path=STATUS_PAGE_FILE):
    """Write a small static HTML page listing the current showtimes -- meant
    to be committed + served via GitHub Pages so there's something to look
    at between ntfy pings, since the page itself has no way to poll Shaw
    live (see README for why)."""
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def row_html(r):
        color = STATUS_COLORS.get(r["status"], STATUS_COLORS["Unknown"])
        if r["seats_total"]:
            seats = f"{r['seats_available']}/{r['seats_total']} ({r['availability_pct']}%)"
        else:
            seats = "–"
        return f"""<tr>
      <td>{escape(r['date'])}</td>
      <td>{escape(r['time'])}</td>
      <td>{escape(r['movie'])}</td>
      <td><span class="badge" style="background:{color}">{escape(r['status'])}</span></td>
      <td>{escape(seats)}</td>
      <td><a href="{escape(r['booking_url'])}" target="_blank" rel="noopener">Book</a></td>
    </tr>"""

    body_rows = "\n".join(row_html(r) for r in rows) or (
        '<tr><td colspan="6" class="empty">No Lido IMAX showtimes in the current window.</td></tr>'
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lido IMAX Showtimes</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem;
          background: Canvas; color: CanvasText; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: GrayText; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, transparent); }}
  th {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; color: GrayText; }}
  .badge {{ color: #fff; padding: 0.15rem 0.55rem; border-radius: 999px; font-size: 0.8rem; white-space: nowrap; }}
  .empty {{ text-align: center; color: GrayText; padding: 2rem 0; }}
  a {{ color: inherit; }}
  @media (max-width: 600px) {{
    table, thead, tbody, th, td, tr {{ display: block; }}
    thead {{ display: none; }}
    tr {{ border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, transparent); padding: 0.5rem 0; }}
    td {{ border: none; padding: 0.15rem 0; }}
    td:before {{ content: attr(data-label); font-weight: 600; display: inline-block; width: 6rem; color: GrayText; }}
  }}
</style>
</head>
<body>
  <h1>Lido IMAX Showtimes</h1>
  <p class="meta">Last updated {escape(updated)} &middot; source: shaw.sg (direct API, not a mirror) &middot;
     pings on new showtimes / status changes go to ntfy</p>
  <table>
    <thead><tr><th>Date</th><th>Time</th><th>Movie</th><th>Status</th><th>Seats</th><th></th></tr></thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
</body>
</html>
"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def send_ntfy(topic, title, message, click_url=None, priority="default"):
    headers = {"Title": title, "Priority": priority}
    if click_url:
        headers["Click"] = click_url
    requests.post(f"{NTFY_SERVER}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15)


def main():
    ap = argparse.ArgumentParser(description="Watch Shaw Theatres' real API for new Lido IMAX showtimes.")
    ap.add_argument("--topic", default=NTFY_TOPIC, help="ntfy.sh topic to push to")
    ap.add_argument("--once", action="store_true",
                     help="run a single check (this is always the behavior; flag kept for parity with cron docs)")
    ap.add_argument("--movie", default=MOVIE_FILTER, help='optional movie title filter, e.g. "The Odyssey"')
    ap.add_argument("--date-start", default=str(DATE_START), help="earliest date to include (YYYY-MM-DD)")
    ap.add_argument("--lookahead-days", type=int, default=LOOKAHEAD_DAYS)
    ap.add_argument("--no-seat-detail", action="store_true",
                     help="skip the extra per-showtime seat-count call (faster; status still comes from Shaw directly)")
    ap.add_argument("--no-status-change-notify", action="store_true",
                     help="only notify on brand-new showtimes, not status changes")
    ap.add_argument("--status-page", default=str(STATUS_PAGE_FILE),
                     help="path to write the static status HTML page to; pass an empty string to skip")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.topic == "REPLACE_ME_WITH_YOUR_TOPIC":
        print("Set NTFY_TOPIC in the script (or pass --topic) before running.", file=sys.stderr)
        sys.exit(1)

    date_start = date.fromisoformat(args.date_start)

    current_rows = fetch_showtimes(
        movie_filter=args.movie,
        date_start=date_start,
        lookahead_days=args.lookahead_days,
        fetch_seats=not args.no_seat_detail,
        verbose=args.verbose,
    )
    current = {r["key"]: r for r in current_rows}

    if args.status_page:
        generate_status_page(current_rows, path=args.status_page)

    previous = load_state()

    if args.verbose:
        print(f"[state] previously_seen={len(previous)} now_seen={len(current)}", file=sys.stderr)

    if not previous:
        print(f"First run: recorded {len(current)} existing showtime(s), no notification sent.")
        save_state(current_rows)
        return

    new_rows = [r for key, r in current.items() if key not in previous]
    new_keys = {r["key"] for r in new_rows}
    changed_rows = []
    if not args.no_status_change_notify:
        for key, r in current.items():
            prev = previous.get(key)
            if prev and key not in new_keys and prev.get("status") != r["status"]:
                changed_rows.append((prev.get("status"), r))

    if new_rows or changed_rows:
        lines = []
        for r in sorted(new_rows, key=lambda r: (r["date"], r["time"]))[:10]:
            lines.append(f"NEW  {r['date']} {r['time']}  {r['movie']}  ({r['status']})")
        if len(new_rows) > 10:
            lines.append(f"...and {len(new_rows) - 10} more new")
        for old_status, r in changed_rows[:10]:
            lines.append(f"CHG  {r['date']} {r['time']}  {r['movie']}  {old_status} -> {r['status']}")
        if len(changed_rows) > 10:
            lines.append(f"...and {len(changed_rows) - 10} more status change(s)")
        message = "\n".join(lines)

        if len(new_rows) == 1:
            click_url = new_rows[0]["booking_url"]
        elif new_rows:
            click_url = SHOWTIMES_PAGE_URL
        elif len(changed_rows) == 1:
            click_url = changed_rows[0][1]["booking_url"]
        else:
            click_url = SHOWTIMES_PAGE_URL

        title_parts = []
        if new_rows:
            title_parts.append(f"{len(new_rows)} new")
        if changed_rows:
            title_parts.append(f"{len(changed_rows)} status change(s)")
        title = f"Lido IMAX: {', '.join(title_parts)}"

        send_ntfy(args.topic, title=title, message=message, click_url=click_url, priority="high")
        print(f"Notified: {len(new_rows)} new showtime(s), {len(changed_rows)} status change(s).")
    else:
        print("No new showtimes or status changes since last check.")

    save_state(current_rows)


if __name__ == "__main__":
    main()
