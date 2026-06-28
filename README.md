# HEADWAY — Real-Time Transit Anomaly Detection

A live anomaly-detection system built on NYC's public GTFS-realtime subway
feed. It ingests a continuously-updating data stream every 30 seconds,
scores each observation for anomalies as it arrives, and pushes results
to a live console dashboard over a WebSocket — no page refresh, no
batch job, no static CSV.

This exists to demonstrate handling **data that doesn't wait for you**:
ingestion, storage, and detection all running against a feed that's
still moving while the system runs, as opposed to analysis performed
once against a file that's already sitting still.

---

## What it actually measures

GTFS-realtime gives every transit agency's currently-running trains and
their predicted arrival times — it does not give you a clean "is this
anomalous" signal. The metric this project derives is:

> **Headway gap** — for a given subway route, the number of seconds
> between the two soonest predicted arrivals among trains currently
> converging on the same direction.

This is a deliberate simplification, not an oversight:

- It's a *proxy* for platform-level headway (the true "minutes between
  trains" a rider feels), computed from whichever two trains are
  closest to arriving somewhere on the route right now — not from a
  fixed checkpoint matched against the static GTFS schedule.
- Getting *exact* per-platform headway would require joining live
  TripUpdates against the static GTFS schedule (stop sequences, planned
  headways per time-of-day) — a real production system would do this,
  and it's the natural "next iteration" of this project. We chose the
  simpler proxy because it still surfaces the real signal (bunching,
  service gaps, disruptions) without that join, and because shipping
  the simple, explainable version first and noting the exact upgrade
  path is the more honest engineering choice than quietly hiding the
  approximation.

Routes monitored by default: **1, 4, 6, L, G, N** (configurable in
`ingest/fetch_feed.py::MONITORED_FEEDS`) — a small, deliberately
high-signal subset chosen to keep poll size and database growth
reasonable for a portfolio-scale deployment, not a coverage limit of
the approach itself.

---

## Architecture

```
   MTA GTFS-RT feed (protobuf, no API key required)
            │
            │  polled every 30s
            ▼
   ingest/scheduler.py  ──────────────►  detection/rolling_zscore.py
   (APScheduler, runs                    (scores each new reading
   inside the API process)                the instant it arrives)
            │                                      │
            ▼                                      ▼
   storage/ (SQLite or Postgres)  ◄──── persists readings + anomalies
            │
            ▼
   api/main.py (FastAPI)
       ├── REST  /api/readings, /api/anomalies   → history on page load
       └── WebSocket /ws/live                    → live push after that
            │
            ▼
   frontend/ (static HTML/CSS/JS, no build step, no framework)
       live console: vitals strip + SVG chart + anomaly log
```

One process runs ingestion, detection, storage, and the API together
(see `api/main.py`'s lifespan hook). That's a deliberate choice, not a
shortcut — see **"Why one process"** below.

---

## Why rolling z-score, not Isolation Forest (or LSTM, or Prophet...)

The brief asks to "document why you chose it over something fancier."
Real answer, in order of weight:

1. **Cold start.** This system starts with an empty database. A rolling
   z-score is useful after ~10 observations (5 minutes of polling).
   Isolation Forest and most ML detectors need a representative
   training window before their output means anything — fine for a
   batch job over months of history, awkward for a system that's
   supposed to start flagging things on day one.

2. **Interpretability.** "This headway is 4.1 standard deviations above
   the last 30 minutes" is auditable by a human in one sentence. An
   isolation-forest path-length score is not, without translating it
   back into "compared to what, recently?" first.

3. **Concept drift for free.** Subway headways have real time-of-day
   structure — 8am and 3am are different "normal"s. A rolling window
   adapts to the current regime automatically. A statically-trained
   model would flag rush hour as anomalous forever unless it's
   periodically refit.

4. **Cost.** O(1) per observation, no model file, no scikit-learn on
   the hot path — appropriate for a free-tier deployment polling every
   30 seconds.

**Where z-score actually falls short**, and why `detection/isolation_forest.py`
exists as a separate, explicitly *not-on-the-hot-path* module: z-score
can't express "this headway is normal alone, but combined with a
dropping active-train count, it's suspicious" — that's a multivariate
pattern. Isolation Forest is the documented upgrade for exactly that
case, run as a periodic **batch** re-scoring job (`python -m
detection.isolation_forest`) against accumulated history, not wired
into the live 30-second poll — because Isolation Forest is a batch-fit
model, and refitting it on every single poll would be wasteful for
negligible benefit. This is the right shape for the upgrade: add
complexity once you can point at what the simple model is failing to
catch, not by default.

---

## Why one process (ingest + API together)

`api/main.py`'s FastAPI lifespan hook starts the APScheduler polling job
in a background thread inside the same process serving the dashboard.
This is correct at this scale for two independent reasons:

- **SQLite's write model.** SQLite allows one writer at a time. Running
  ingestion as a separate process against the same SQLite file invites
  exactly the kind of "two processes fighting over one file" bug this
  project is meant to avoid creating. (Switch to Postgres — one
  environment variable, see below — and this stops being a constraint,
  but the colocated design is still simpler with no downside at this
  traffic level.)
- **Free-tier deploy constraints.** Render's free instance type only
  supports Web Service, Postgres, and Key Value — not Background
  Workers or Cron Jobs. Colocating ingestion inside the web service is
  the only way to get continuous polling on the free tier at all, not
  just the convenient one.

Split into a separate ingest worker once you have either concurrent
writers that need true parallelism, or polling load heavy enough to
want independent scaling from API traffic — at that point you've
outgrown the free tier anyway and the constraint above no longer
applies.

---

## Storage: SQLite by default, Postgres via one env var

`storage/db.py` defaults to a local SQLite file. Set `DATABASE_URL` to
any Postgres connection string and every other module picks it up with
no code changes — `storage/db.py` is the only file that knows which
backend is active.

SQLite is genuinely fine for this write pattern: ~6 routes × 1 row per
30-second poll is roughly 1,700 rows/hour, well within what SQLite's
single-writer model handles comfortably. It stops being fine the moment
you add concurrent writers (see above) — that's the actual threshold to
watch for, not "SQLite doesn't scale" as a vague rule of thumb.

---

## Running it locally

```bash
git clone <this repo>
cd gtfs-anomaly-detector
pip install -r requirements.txt --break-system-packages   # or use a venv

# starts the API + scheduler together; visit http://localhost:8000/api/health
uvicorn api.main:app --reload

# open frontend/index.html directly in a browser, or serve it:
cd frontend && python3 -m http.server 5500
# then visit http://localhost:5500/?api=http://localhost:8000
```

The first few minutes will show "collecting data…" on the chart — that's
expected; the rolling detector needs `MIN_OBSERVATIONS_BEFORE_SCORING`
(10, ~5 minutes at the default poll interval) readings per route before
it scores anything.

### Running the tests

```bash
pip install pytest --break-system-packages
pytest tests/ -v
```

`tests/test_rolling_zscore.py` and `tests/test_fetch_feed.py` only
depend on stdlib (the feed-parsing tests use lightweight fakes instead
of real protobuf objects), so they run even before you've installed
the full dependency list — useful as a first sanity check.

### Trying the Isolation Forest upgrade path

```bash
# after the live system has accumulated >= 50 readings for a route:
python -m detection.isolation_forest
```

This re-scores stored history per route and writes any newly-flagged
anomalies (`method="isolation_forest"`) alongside the live z-score
flags, so you can compare what each approach catches.

---

## Deploying

### Backend — Render (genuinely free)

```bash
# push to GitHub, then in Render: New > Blueprint, point at the repo
```

`deploy/render.yaml` provisions the web service and a free Postgres
instance together. **Read the comments in that file before deploying**
— free-tier constraints that materially affect this project (verified
as of 2026, not assumed):

- Free Postgres databases are capped at 1GB and **expire after 30
  days** unless upgraded. At this project's write volume that's not a
  storage problem, but it is a "the database disappears" problem if
  ignored.
- Free web services have an **ephemeral filesystem** — a local SQLite
  file is wiped on every restart. This is why the blueprint wires
  `DATABASE_URL` to the Postgres instance rather than leaving the app
  on its SQLite default.
- Free web services **spin down after 15 minutes of inactivity** and
  take 30-60s to cold-start on the next request — meaning polling
  pauses while spun down. An external uptime pinger (any free cron-style
  service hitting `/api/health` every 10 minutes) keeps it warm if you
  want truly continuous polling.

A Dockerfile is included at `deploy/Dockerfile` for any container
platform. **Note on Fly.io specifically**, since the brief mentions it:
Fly.io removed its free tier for new accounts in 2024. A small
always-on instance now runs roughly $2-5/month pay-as-you-go. If you
want a genuinely free deployment, use the Render path above.

### Frontend — Vercel (genuinely free, static-only)

The `frontend/` directory is plain HTML/CSS/JS with zero build step and
zero dependencies, so it deploys to Vercel's free Hobby tier as a static
site with no serverless-function limits to worry about:

```bash
cd frontend
vercel deploy --prod
```

Then visit the deployed URL with `?api=https://your-render-url.onrender.com`
appended, or edit `API_BASE` directly in `app.js` for a permanent
deployment, since query params are easy to forget to re-append.

---

## What's genuinely simplified here, stated plainly

- **Headway gap is a proxy**, not exact platform headway — see above.
- **6 routes, not all ~26** — a coverage choice for portfolio scale, not
  a limitation of the ingestion code (add more in `MONITORED_FEEDS`).
- **z ≥ 3.0 is a fixed threshold**, not tuned against labeled ground
  truth (there isn't any — "anomalous service" doesn't come pre-labeled
  in the wild). A real production deployment would validate this
  threshold against known disruption events from MTA's own service
  alerts feed.
- **The SVG chart's "expected range" band is computed client-side for
  display only** — it's a visual aid using the same rolling-window math,
  not the actual detector. The real anomaly flags come from the server.

None of these are hidden — they're the deliberate scope line between
"a working, honest demonstration of streaming ingestion + detection +
live serving" and "a production transit operations platform," and that
line is exactly where this project intends to sit.
