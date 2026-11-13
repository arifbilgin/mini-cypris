# Mini-Cypris

Search open-access research papers from the [CORE API](https://core.ac.uk/services/api): an Angular app backed by a small Flask service with a SQLite cache.

**Status:** step 2a. Hello-world wiring (Angular → Flask → SQLite) plus a tested client for the CORE API. No search endpoint yet.

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

Unknown `/api/*` paths return a JSON `404`.

**Postman:** import `postman/mini-cypris.postman_collection.json` plus one environment (`local-dev`: port 8000, `docker`: port 8080). Every request has a saved example response and tests.

## Configuration

All optional; set as environment variables.

| Variable | Default | What it does |
|---|---|---|
| `CORE_API_KEY` | none | CORE API key, sent as `Authorization: Bearer`. Works without one. Never logged |
| `CORE_BASE_URL` | `https://api.core.ac.uk/v3` | CORE API root |
| `CORE_TIMEOUT` | `20` | Seconds to wait for CORE to answer (connecting has its own 5 s limit) |
| `DATABASE_PATH` | `backend/data/cache.db` | SQLite cache file (`/data/cache.db` in the container) |

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

CORE quirks the client handles (measured 2026-11-13): `limit` above 100 is a 400; an aggregate field with no matches comes back as `[]`, not `{}`; the rate-limit reset time is an ISO timestamp, not seconds. A malformed boolean query is *not* an error at CORE (it still returns results), so query checks belong in our API.

## Layout

```
backend/
  app/
    __init__.py     create_app(); serves the built app when FRONTEND_DIST is set
    core_client.py  CORE API client (httpx): requests, errors, rate-limit headers
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
