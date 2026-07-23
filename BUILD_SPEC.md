# Build Spec: Shaw Lido IMAX Showtime Watcher

## Tooling

Use **uv** for this project (not raw `pip`/`venv`):
- Prefer a single-file script with inline PEP 723 dependency metadata (see
  the `# /// script` block at the top of `watch_shaw_lido.py` for the
  pattern) so it runs standalone via `uv run watch_shaw_lido.py` with no
  separate install step — `uv` reads the dependency block and handles the
  environment itself.
- If the rewritten scraper grows enough to want a proper package (multiple
  modules, tests), scaffold it with `uv init` instead and add deps via
  `uv add <package>`.
- All setup/run/schedule instructions in the final deliverable should use
  `uv run ...`, not `python3 ...` / `pip install ...`.

## Goal

A self-contained script (no dependency on third-party mirrors) that:

1. Pulls **live showtime + seat-availability data directly from Shaw
   Theatres' own backend** (shaw.sg), the same way
   `https://shaw-availability.pages.dev/` does — we are duplicating that
   site's function, not scraping the mirror site itself.
2. Filters to **Lido IMAX**, date window **27 Jul – 1 Aug 2026**.
3. Diffs against the previous run's results.
4. Pushes an **ntfy.sh** notification the moment new showtimes appear (or,
   optionally, when availability/status changes — see "Nice-to-haves").
5. Runs unattended on a schedule (cron / Task Scheduler).

## Why this approach

We already have a working version (`watch_shaw_lido.py`, attached) that
polls `shaw-availability.pages.dev` and diffs its HTML report. That works,
but it's a second-hand dependency — if that site goes down, changes its
markup, or lags behind, we lose visibility. We want our own scraper hitting
Shaw's real data source.

## Step 0 — Reverse-engineer Shaw's actual data source (do this first)

`https://shaw.sg/showtimes?date=YYYY-MM-DD&movie=<slug>` is a client-rendered
page — the HTML shell has no showtime data; it's fetched via JS after load.
I (Claude, in the chat) don't have browser/network-inspection access to
confirm the exact endpoint, so this is the first real task:

1. Open `https://shaw.sg/showtimes?date=2026-07-27&movie=The+Odyssey_1238`
   in a real browser with DevTools → Network tab open (or use Playwright
   with request interception).
2. Identify the XHR/fetch request(s) that return the actual showtime/seat
   data — likely JSON, possibly a `/api/...` path or a Next.js
   `/_next/data/.../showtimes.json` route. Capture:
   - Full URL pattern (including how date, movie ID, and cinema/venue are
     passed — query params vs path segments)
   - Required headers (some sites gate on `Referer`, `Origin`, or a
     session/anti-bot cookie/token — check for this)
   - Response JSON shape: movie name, venue/hall name, format (IMAX vs
     standard), showtime, total seats, seats sold/available, per-seat map
     if present, session/booking ID, booking URL construction
3. Confirm whether the same endpoint can be queried **without** a specific
   movie ID (i.e., "give me everything showing at Lido on date X") — this
   is simpler than needing to know every movie's internal ID ahead of time.
   If not, we'll need a secondary call to resolve "The Odyssey" → its
   current movie ID/slug (this can change per release).
4. Sanity-check whether the response includes a `soldOut`/status field
   directly, or whether we need to derive "Available / Selling Fast / Sold
   Out" from seat counts ourselves (the mirror site's thresholds aren't
   known — pick reasonable defaults, e.g. >20% available = Available,
   5–20% = Selling Fast, <5% = Sold Out, and make them configurable).
5. Write down what you found at the top of the new script as a comment,
   since this endpoint isn't publicly documented and may shift.

If Shaw's endpoint turns out to require a session cookie obtained by first
loading the HTML page (common anti-scraping pattern), the scraper will need
to do a two-step fetch: load the page first (to get cookies), then call the
API with those cookies attached. Handle this with a `requests.Session()`
(or Playwright if a real browser context turns out to be required).

## Step 1 — Data model

Match (at least) the fields the mirror site exposes, since they're a good
proxy for "useful":

```
{
  "date": "2026-07-30",
  "movie": "The Odyssey",
  "venue": "Lido IMAX",
  "time": "20:30",
  "seats_available": 174,
  "seats_total": 413,
  "availability_pct": 42.1,
  "status": "Available" | "Selling Fast" | "Sold Out",
  "best_seats": ["A8-10", "B8"],   # optional, nice-to-have
  "booking_url": "https://shaw.sg/showtimes/513029"
}
```

## Step 2 — Filtering

- Venue contains `"Lido"` AND format is IMAX (confirm how IMAX is denoted
  in the real API — separate `format` field vs baked into venue name).
- **Date: `>= 2026-07-27`, open-ended (no upper bound).** Lido appears to
  release showtimes in batches rather than all at once (unconfirmed
  whether there's a fixed day-of-week pattern — don't build anything that
  depends on that assumption). The event we actually care about is "a date
  in this range that wasn't in the previous run's result set just showed
  up" — not a specific weekday. Don't hardcode an end date; each week the
  watcher should naturally start catching whatever comes next too, as Shaw
  keeps releasing further out.
- Movie: default to no filter (any movie at Lido IMAX from 27 Jul onward),
  but support an optional `--movie "The Odyssey"` filter via CLI arg, since
  that's today's specific interest but shouldn't be hardcoded long-term.

## Step 3 — State + diffing

- Persist last-seen results keyed by a stable unique ID (booking URL/session
  ID is ideal — don't invent a synthetic key unless the API gives nothing
  stable).
- On each run: fetch fresh → compare keys against `seen_state.json` →
  anything new triggers a notification → always overwrite state with the
  full current set afterward.
- First-ever run should seed state silently (no notification blast for
  "everything is new").

## Step 4 — Notifications (ntfy.sh)

- `POST https://ntfy.sh/<topic>` with the message body as plain text,
  `Title` header, `Priority: high` header, and a `Click` header set to the
  booking URL of the first new showtime (or the shaw.sg showtimes page if
  there are several).
- Topic name should be a CLI arg / config value, not hardcoded — treat it
  like a secret (anyone who knows your topic name can read/spam it, since
  ntfy.sh topics are unauthenticated by default).
- Reuse the message formatting style from `watch_shaw_lido.py` (one line
  per new showtime: date, time, availability, status).

## Step 5 — Scheduling

- Same as before: cron or Task Scheduler, every 30 min, roughly
  7am–11pm SGT (no point polling overnight).
- Script should be a single run-and-exit process (not a long-lived loop),
  so cron/Task Scheduler owns the scheduling.
- Use `uv run <script>.py` as the invoked command (works whether the
  script has inline PEP 723 deps or lives in a `uv`-managed project) —
  e.g. cron: `*/30 7-23 * * * cd /path/to/project && uv run watcher.py`.

## Reference implementation

`watch_shaw_lido.py` (attached separately) already implements steps 2–5
against the mirror site's HTML — its diffing, state-file, and ntfy logic
can be lifted almost as-is. The only real new work is Step 0/1: swapping
the HTML-scraping `fetch_showtimes()` function for one that calls Shaw's
real API and returns the same shape of data.

## Acceptance criteria

- [ ] Running `--once -v` against today's date prints a sane count of
      Lido IMAX showtimes with correct-looking seat numbers (spot-check
      one against the real shaw.sg booking page).
- [ ] Running it twice in a row with no real-world change produces "no new
      showtimes" both times (idempotent).
- [ ] Manually clearing `seen_state.json` and re-running produces a "first
      run" seed with no notification spam.
- [ ] When Shaw publishes the next week (test by watching for 30/31 Jul or
      1 Aug to appear, since only 27–29 Jul exist as of writing), the next
      scheduled run sends exactly one ntfy notification listing the new
      showtimes.
- [ ] The following week (whenever Lido drops it) gets caught too, without
      needing a code change to bump an end-date constant — confirm this by
      re-running once a week or two out and checking new dates get picked
      up automatically.
- [ ] Script fails loudly (non-zero exit, clear stderr message) if Shaw's
      API shape changes or the request is blocked, rather than silently
      reporting zero results.

## Nice-to-haves (skip unless there's time)

- Notify on **status changes** too (e.g. a previously "Available" show
  flips to "Selling Fast") — useful for "should I book now" pressure, not
  just "does this showtime exist."
- Support multiple venues/date windows via a small YAML/JSON config instead
  of hardcoded constants.
- Retry/backoff on transient network errors instead of failing the whole
  run.
