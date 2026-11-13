"""Thin client for the CORE v3 API (https://api.core.ac.uk/v3).

It only talks HTTP: it builds requests, checks the response shape, reads the
rate-limit headers and turns every failure into one of the CoreError types.
Records come back exactly as CORE sent them; cleaning, caching and throttling
live in other modules.

CORE behaviour this relies on (measured 2026-11-13):
- search is GET /search/works/?q=&limit=&offset=; limit above 100 is a 400.
- aggregate is POST /search/works/aggregate with {"q", "aggregations": [...]};
  a field with no matches comes back as [] instead of {}.
- every response carries x-ratelimit-limit / -remaining / -retry-after, where
  retry-after is an ISO timestamp such as 2026-11-13T18:31:22+0000.
- a malformed boolean query is NOT an error: CORE answers 200 with results.
- a query its search backend rejects (unknown field `TITLE:x`, `yearPublished:abc`)
  comes back as HTTP 500 with "Azure search failed with status code: 400. ...";
  that is our caller's mistake, not an outage, so it is raised as CoreBadQuery.
- a 429 has an empty body.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.core.ac.uk/v3"
DEFAULT_TIMEOUT = 20.0  # seconds to wait for a response
DEFAULT_CONNECT_TIMEOUT = 5.0
MAX_LIMIT = 100  # CORE rejects anything larger with a 400
USER_AGENT = "mini-cypris"

# CORE's wrapper around its search backend's "bad request" (see module docstring)
_BACKEND_REJECTED = re.compile(r"status code: 400\b")
_BACKEND_DETAIL = re.compile(r"message=(.+?)(?:, details=|, request-id=|$)")

log = logging.getLogger(__name__)


# ----- errors -----------------------------------------------------------------


class CoreError(Exception):
    """Base class: catching this catches every failure talking to CORE."""

    def __init__(self, message: str, *, status: int | None = None, rate: RateLimit | None = None):
        super().__init__(message)
        self.status = status  # CORE's HTTP status, None if no response came back
        self.rate = rate  # rate-limit headers, when there was a response


class CoreBadQuery(CoreError):
    """CORE answered 400: it rejected the request (e.g. limit too large)."""


class CoreRateLimited(CoreError):
    """CORE answered 429. resets_at is when to try again (Unix seconds, UTC) if known."""

    def __init__(self, message: str, *, resets_at: int | None, **kwargs):
        super().__init__(message, **kwargs)
        self.resets_at = resets_at


class CoreUnavailable(CoreError):
    """CORE is down or unreachable: a 5xx, a connection failure or a timeout."""


class CoreTimeout(CoreUnavailable):
    """CORE did not answer in time."""


class CoreUnexpected(CoreError):
    """CORE answered with something we don't understand: a bad body or an odd status."""


# ----- results ----------------------------------------------------------------


@dataclass(frozen=True)
class RateLimit:
    """CORE's x-ratelimit-* headers. Any value can be None if the header is missing or garbage."""

    limit: int | None
    remaining: int | None
    resets_at: int | None  # Unix seconds, UTC

    @classmethod
    def from_headers(cls, headers: httpx.Headers, now: float | None = None) -> RateLimit:
        reset = headers.get("x-ratelimit-retry-after") or headers.get("retry-after")
        return cls(
            limit=_int_or_none(headers.get("x-ratelimit-limit")),
            remaining=_int_or_none(headers.get("x-ratelimit-remaining")),
            resets_at=_parse_reset(reset, time.time() if now is None else now),
        )


@dataclass(frozen=True)
class SearchPage:
    total_hits: int  # matches across all of CORE, not just this page
    offset: int
    limit: int
    records: list[dict[str, Any]]  # CORE's records, untouched
    rate: RateLimit


@dataclass(frozen=True)
class Aggregate:
    buckets: dict[str, dict[str, int]]  # field -> bucket -> count, e.g. {"yearPublished": {"2024": 523}}
    rate: RateLimit


# ----- client -----------------------------------------------------------------


class CoreClient:
    """One instance per app. Thread-safe (httpx.Client is), reuses connections."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ):
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        api_key = (api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CoreClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- public calls ---

    def search(self, q: str, limit: int, offset: int = 0) -> SearchPage:
        """One page of works matching q. total_hits counts every match in CORE."""
        q = _check_query(q)
        _check_int("limit", limit, low=1, high=MAX_LIMIT)
        _check_int("offset", offset, low=0)
        body, rate = self._call("GET", "/search/works/", q, params={"q": q, "limit": limit, "offset": offset})
        return SearchPage(
            total_hits=_require_int(body, "totalHits", rate),
            offset=_optional_int(body, "offset", default=offset),
            limit=_optional_int(body, "limit", default=limit),
            records=_require_records(body, rate),
            rate=rate,
        )

    def aggregate(self, q: str, fields: list[str] | tuple[str, ...] = ("yearPublished",)) -> Aggregate:
        """Counts per bucket across ALL works matching q (not just one page)."""
        q = _check_query(q)
        if isinstance(fields, str) or not fields or not all(isinstance(f, str) and f.strip() for f in fields):
            raise ValueError("fields must be a non-empty list of field names")
        fields = list(fields)
        body, rate = self._call("POST", "/search/works/aggregate", q, json={"q": q, "aggregations": fields})
        aggs = body.get("aggregations")
        if not isinstance(aggs, dict):
            raise CoreUnexpected("aggregate response has no 'aggregations' object", status=200, rate=rate)
        return Aggregate(buckets={f: _buckets(aggs, f, rate) for f in fields}, rate=rate)

    # --- plumbing ---

    def _call(self, method: str, path: str, q: str, **kwargs) -> tuple[dict[str, Any], RateLimit]:
        started = time.monotonic()
        try:
            res = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            self._log(logging.WARNING, method, path, q, "timeout", started)
            raise CoreTimeout(f"CORE did not answer in time ({type(exc).__name__})") from exc
        except httpx.DecodingError as exc:  # body could not be decompressed/decoded
            self._log(logging.WARNING, method, path, q, "undecodable", started)
            raise CoreUnexpected(f"CORE sent a body we could not decode ({type(exc).__name__})") from exc
        except httpx.RequestError as exc:  # connection refused, reset, DNS, protocol errors
            self._log(logging.WARNING, method, path, q, "unreachable", started)
            raise CoreUnavailable(f"could not reach CORE ({type(exc).__name__})") from exc

        rate = RateLimit.from_headers(res.headers)
        level = logging.INFO if res.status_code == 200 else logging.WARNING
        self._log(level, method, path, q, res.status_code, started, rate)

        status = res.status_code
        if status == 200:
            try:
                body = res.json()
            except ValueError as exc:  # HTML error page, truncated JSON, empty body
                raise CoreUnexpected("CORE answered 200 but the body is not JSON", status=200, rate=rate) from exc
            if not isinstance(body, dict):
                raise CoreUnexpected("CORE answered 200 but the body is not a JSON object", status=200, rate=rate)
            return body, rate
        if status == 400:
            raise CoreBadQuery(f"CORE rejected the request: {_message(res)}", status=400, rate=rate)
        if status == 429:
            raise CoreRateLimited("CORE rate limit reached", resets_at=rate.resets_at, status=429, rate=rate)
        if 500 <= status <= 599:
            text = _message(res)
            if _BACKEND_REJECTED.search(text):
                detail = _BACKEND_DETAIL.search(text)
                reason = detail.group(1).strip() if detail else text
                raise CoreBadQuery(f"CORE rejected the query: {reason}", status=status, rate=rate)
            raise CoreUnavailable(f"CORE is having trouble (HTTP {status})", status=status, rate=rate)
        raise CoreUnexpected(f"CORE answered HTTP {status}: {_message(res)}", status=status, rate=rate)

    @staticmethod
    def _log(level, method, path, q, outcome, started, rate: RateLimit | None = None):
        remaining = rate.remaining if rate else None
        log.log(level, "core %s %s q=%r -> %s in %.2fs (rate remaining=%s)",
                method, path, q[:120], outcome, time.monotonic() - started, remaining)


# ----- helpers ----------------------------------------------------------------


def _check_query(q: Any) -> str:
    if not isinstance(q, str) or not q.strip():
        raise ValueError("q must be a non-empty string")
    return q.strip()


def _check_int(name: str, value: Any, *, low: int, high: int | None = None) -> None:
    # bool is an int subclass in Python; True must not count as limit=1
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if value < low or (high is not None and value > high):
        bounds = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ValueError(f"{name} must be {bounds}, got {value}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(body: dict, key: str, rate: RateLimit) -> int:
    value = body.get(key)
    if not _is_int(value) or value < 0:
        raise CoreUnexpected(f"search response has no valid '{key}' (got {value!r:.50})", status=200, rate=rate)
    return value


def _optional_int(body: dict, key: str, *, default: int) -> int:
    value = body.get(key)
    return value if _is_int(value) and value >= 0 else default


def _require_records(body: dict, rate: RateLimit) -> list[dict[str, Any]]:
    results = body.get("results")
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise CoreUnexpected("search response 'results' is not a list of records", status=200, rate=rate)
    return results


def _buckets(aggs: dict, field: str, rate: RateLimit) -> dict[str, int]:
    raw = aggs.get(field)
    if raw is None:
        raise CoreUnexpected(f"aggregate response is missing field '{field}'", status=200, rate=rate)
    if isinstance(raw, list):
        if raw:  # CORE sends [] for "no matches"; a non-empty list is a shape we don't know
            raise CoreUnexpected(f"aggregate field '{field}' is a non-empty list", status=200, rate=rate)
        return {}
    if not isinstance(raw, dict) or not all(_is_int(v) and v >= 0 for v in raw.values()):
        raise CoreUnexpected(f"aggregate field '{field}' is not a map of counts", status=200, rate=rate)
    return {str(k): v for k, v in raw.items()}


def _message(res: httpx.Response) -> str:
    """CORE's error text: the JSON 'message' if there is one, else the start of the body."""
    try:
        body = res.json()
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            return body["message"][:300]
    except ValueError:
        pass
    text = res.text.strip()
    return text[:300] if text else "(empty body)"


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value.strip()) if value is not None else None
    except ValueError:
        return None


def _parse_reset(value: str | None, now: float) -> int | None:
    """When the rate limit resets, as Unix seconds. Accepts seconds-from-now, ISO 8601 or an HTTP date."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():  # seconds from now; isascii rules out "²" and friends
        return int(now) + int(value) if len(value) <= 10 else None
    for parse in (_parse_iso, parsedate_to_datetime):
        try:
            moment = parse(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    return None


def _parse_iso(value: str) -> datetime:
    # Python 3.13 fromisoformat handles "Z" and "+00:00"; CORE sends "+0000"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
