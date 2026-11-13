# Mini-Cypris

Search open-access research papers from the [CORE API](https://core.ac.uk/services/api): an Angular app backed by a small Flask service with a SQLite cache.

**Status:** step 1. Hello-world wiring: Angular → Flask → SQLite.

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
cd backend && .venv/bin/python -m pytest      # API
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

## Layout

```
backend/
  app/
    __init__.py     create_app(); serves the built app when FRONTEND_DIST is set
    db.py           SQLite connection; creates the schema on first start
    routes.py       /api endpoints
    schema.sql      database schema
  tests/            pytest
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
