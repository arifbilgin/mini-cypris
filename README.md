# Mini-Cypris

Search open-access research papers from the [CORE API](https://core.ac.uk/services/api): an Angular app backed by a small Flask service with a SQLite cache.

**Status:** step 3. `GET /api/search` works end to end through a SQLite read-through cache. No UI yet; the Angular app is still the hello-world page.

## Run it (Docker, one command)

```bash
docker compose up --build
```

Open http://localhost:8080. Flask serves both the API (`/api/*`) and the built Angular app from one port, so there is no CORS setup. The SQLite cache lives in a Docker volume and survives restarts.

## Run for development

Requirements: **Node 24 LTS** (Angular 22 needs Node `^22.22.3` or `^24.15`) and **Python 3.13**.

Terminal 1: API on port 8000

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/flask --app wsgi run --port 8000 --debug
```

Terminal 2: app on port 4200

```bash
cd frontend
npm install
npx ng serve
```

Open http://localhost:4200. The dev server proxies `/api` to port 8000 (`frontend/proxy.conf.json`).

Port 8000 rather than Flask's default 5000, because macOS uses 5000 for AirPlay.

## Tests

```bash
cd backend && .venv/bin/python -m pytest      # API (no network; CORE is faked)
cd backend && .venv/bin/python -m pytest -m live   # contract checks against the real CORE API (4 requests)
cd frontend && npx ng test --watch=false      # Angular
npx newman run postman/mini-cypris.postman_collection.json -e postman/docker.postman_environment.json   # API contract
```

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | Service + database status, lists the SQLite tables |
| GET | `/api/hello?name=` | Hello-world check (`name` optional) |
| GET | `/api/search?q=&limit=` | Top `limit` papers (1–100, default 10) for a boolean query, through the cache |

Unknown `/api/*` paths return a JSON `404`; unexpected errors return a JSON `500`.

### `GET /api/search`

```
GET /api/search?q=drone AND (package OR delivery)&limit=5

200  X-Cache: MISS
{
  "query": "drone AND (package OR delivery)",       // whitespace-normalized, as sent to CORE
  "limit": 5,
  "total_hits": 4691,                               // matches across all of CORE
  "count": 5,
  "results": [{"id": 1, "source": "core", "external_id": "158741339", "title": "...",
               "authors": ["..."], "year": 2021, "publisher": "...", "doi": "...",
               "pdf_url": "...", "abstract": "..."}],
  "cache": {"status": "miss", "fetched_at": 1794595200, "age_seconds": 0, "error": null}
}
```

| Error | Status | When |
|---|---|---|
| `bad_request` | 400 | `q` missing or blank or over 1000 characters; `limit` not a whole number from 1 to 100 |
| `core_rejected_query` | 400 | CORE refused the query, e.g. `TITLE:drone` (field names are case-sensitive) |
| `core_rate_limited` | 429 | CORE's rate limit, nothing cached; `Retry-After` says when to try again |
| `core_unavailable` | 502 | CORE down or unreachable, nothing cached |
| `core_bad_response` | 502 | CORE answered with something unreadable, nothing cached |
| `core_timeout` | 504 | CORE did not answer in time, nothing cached |

**Postman:** import `postman/mini-cypris.postman_collection.json` plus one environment (`local-dev`: port 8000, `docker`: port 8080). Every request has a saved example response and tests.

## Configuration

All optional; set as environment variables.

| Variable | Default | What it does |
|---|---|---|
| `CORE_API_KEY` | none | CORE API key, sent as `Authorization: Bearer`. Works without one. Never logged |
| `CORE_BASE_URL` | `https://api.core.ac.uk/v3` | CORE API root |
| `CORE_TIMEOUT` | `20` | Seconds to wait for CORE to answer (connecting has its own 5 s limit) |
| `CACHE_TTL_SECONDS` | `86400` | How long a cached search counts as fresh |
| `DATABASE_PATH` | `backend/data/cache.db` | SQLite cache file (`/data/cache.db` in the container) |

## Cache

`backend/app/cache.py`. Every search goes through SQLite first; `X-Cache` and `cache.status` say what happened:

| Status | Meaning | CORE calls |
|---|---|---|
| `miss` | Never seen: fetch `limit` rows, store, serve | 1 |
| `hit` | Cached, under 24 h old, deep enough (or CORE has no more rows) | 0 |
| `topup` | Cached and fresh but shallower than `limit`: fetch only the missing rows (`offset` = cached depth) and append | 1 |
| `refresh` | Older than 24 h: fetch `limit` rows again from rank 1 and replace the list | 1 |
| `stale` | Needed a refresh but CORE failed: serve the old list (`cache.error` says why) | 1 failed |
| `partial` | Needed a top-up but CORE failed: serve the rows we have | 1 failed |

- **Key:** the query with whitespace collapsed. Case is kept on purpose: CORE ignores the case of search terms but not of field names (`title:drone` works, `TITLE:drone` is an error), so folding case could merge two different queries.
- **Storage:** each paper is stored once (`papers`, unique per source + id) however many searches find it; `search_results` holds each search's ranking. The raw CORE record is kept in `raw_json`.
- **Freshness:** 24 h (`CACHE_TTL_SECONDS`). A top-up keeps the original fetch time, so age is always that of the oldest rows.
- **Failures:** if CORE fails and something is cached, the cached rows are served; if nothing is cached, the error is returned and nothing is stored. Zero-result searches are cached too.
- **Concurrency:** gunicorn runs 4 threads. SQLite is in WAL mode, every write is one `BEGIN IMMEDIATE` transaction, a fresh list only ever grows (a slow, shallower response never overwrites a deeper one), and a top-up only appends rows past the current depth. Stress-tested with 16 threads and random CORE delays.

## CORE client

`backend/app/core_client.py` is a thin wrapper over [httpx](https://www.python-httpx.org/): it sends the request, checks the response shape, reads CORE's rate-limit headers, and turns every failure into one error type. Records come back exactly as CORE sent them.

| CORE result | Raised as |
|---|---|
| 400 (e.g. limit above 100) | `CoreBadQuery` |
| 429 | `CoreRateLimited` (with `resets_at`) |
| 5xx, connection failure | `CoreUnavailable` |
| timeout | `CoreTimeout` (a kind of `CoreUnavailable`) |
| unreadable body, other status | `CoreUnexpected` |

All of them inherit from `CoreError`. There are no automatic retries: CORE allows about 10 requests per short window, so retries would spend that budget.

CORE quirks the client handles (measured 2026-11-13):

- `limit` above 100 is a 400.
- A query CORE's search backend rejects (unknown field like `TITLE:drone`, `yearPublished:abc`) comes back as **HTTP 500** with `Azure search failed with status code: 400 ...`. The client raises that as `CoreBadQuery`, not as an outage.
- An aggregate field with no matches comes back as `[]`, not `{}`.
- The rate-limit reset time is an ISO timestamp, not seconds. In practice the budget is about 10 requests per minute, tighter than the headers suggest. A 429 has an empty body.
- A malformed boolean query such as `(drone AND ` is *not* an error at CORE (it still returns results), so query checks belong in our API.
- With no operator, terms are OR'ed: `drone delivery` = `drone OR delivery` (1.6M hits) while `drone AND delivery` has 4.3K. Lowercase `and`/`or` work as operators.

## Layout

```
backend/
  app/
    __init__.py     create_app(); serves the built app when FRONTEND_DIST is set
    core_client.py  CORE API client (httpx): requests, errors, rate-limit headers
    cache.py        read-through SQLite cache for searches
    papers.py       CORE record -> papers row -> API shape
    db.py           SQLite connection; creates the schema on first start
    routes.py       /api endpoints
    schema.sql      database schema
  tests/            pytest; fixtures/ holds real CORE responses
  wsgi.py           entry point (flask run / gunicorn)
frontend/           Angular 22 app (standalone components, signals)
postman/            collection + environments
Dockerfile          builds Angular, then runs Flask + gunicorn on :8080
docker-compose.yml  one service + a volume for the cache
```

## Database

`backend/app/schema.sql`, created automatically on first start:

| Table | Holds |
|---|---|
| `papers` | One row per paper, stored once. Our own `id`; the source's id is `external_id`, unique per `source` |
| `searches` | One row per normalized query: total hits, how many rows are cached, when |
| `search_results` | Which papers a search returned, in rank order |
| `aggregates` | Chart data across all matches: one row per bucket and metric |

Timestamps are Unix seconds (UTC).
