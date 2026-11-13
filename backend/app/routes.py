import time

from flask import Blueprint, current_app, jsonify, request

from .cache import SearchCache
from .core_client import (
    MAX_LIMIT,
    CoreBadQuery,
    CoreError,
    CoreRateLimited,
    CoreTimeout,
    CoreUnavailable,
)
from .db import get_db

api = Blueprint("api", __name__)

DEFAULT_LIMIT = 10
MAX_QUERY_LENGTH = 1000  # CORE takes q in the URL; full syntax checks come later


@api.get("/health")
def health():
    tables = [
        row["name"]
        for row in get_db().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {"status": "ok", "version": current_app.config["VERSION"], "db": "ok", "tables": tables}


@api.get("/hello")
def hello():
    name = (request.args.get("name") or "").strip()[:50] or "world"
    return {"message": f"Hello, {name}! This is Flask.", "name": name, "served_at": int(time.time())}


@api.get("/search")
def search():
    """Top `limit` papers for `q`, served through the SQLite cache."""
    q = request.args.get("q", "")
    if not q.strip():
        return _error(400, "bad_request", "q is required")
    if len(q) > MAX_QUERY_LENGTH:
        return _error(400, "bad_request", f"q is longer than {MAX_QUERY_LENGTH} characters")
    raw_limit = request.args.get("limit", str(DEFAULT_LIMIT)).strip()
    # ASCII digits only: int() would also accept "+5", "1_0" and "５"
    if not (raw_limit.isascii() and raw_limit.isdigit()) or not 1 <= int(raw_limit) <= MAX_LIMIT:
        return _error(400, "bad_request", f"limit must be a whole number from 1 to {MAX_LIMIT}")
    limit = int(raw_limit)

    clock = current_app.config["CLOCK"]
    cache = SearchCache(get_db(), current_app.extensions["core"], ttl=current_app.config["CACHE_TTL"], clock=clock)
    try:
        result = cache.search(q, limit)
    except CoreError as exc:
        return _core_error(exc, clock())

    body = {
        "query": result.query,
        "limit": limit,
        "total_hits": result.total_hits,
        "count": len(result.papers),
        "results": result.papers,
        "cache": {
            "status": result.status,
            "fetched_at": result.fetched_at,
            "age_seconds": max(0, int(clock()) - result.fetched_at),
            "error": result.error,
        },
    }
    response = jsonify(body)
    response.headers["X-Cache"] = result.status.upper()
    return response


def _error(status: int, code: str, message: str, headers: dict | None = None):
    return jsonify(error=code, message=message), status, headers or {}


def _core_error(exc: CoreError, now: float):
    """CORE failed and nothing usable was cached."""
    if isinstance(exc, CoreBadQuery):
        return _error(400, "core_rejected_query", str(exc))
    if isinstance(exc, CoreRateLimited):
        wait = max(1, exc.resets_at - int(now)) if exc.resets_at else 1
        return _error(429, "core_rate_limited", "CORE's rate limit was reached; try again shortly",
                      {"Retry-After": str(wait)})
    if isinstance(exc, CoreTimeout):
        return _error(504, "core_timeout", "CORE did not answer in time")
    if isinstance(exc, CoreUnavailable):
        return _error(502, "core_unavailable", "CORE is unavailable right now")
    return _error(502, "core_bad_response", "CORE sent a response we could not read")
