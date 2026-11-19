"""Papers per publication year across ALL matches of a query, cached in `aggregates`.

CORE's aggregate endpoint counts every match, not just one page, but its
buckets have two problems (measured 2026-11-13):

- junk years: 9999, 2096, 2029 appear next to real ones. Buckets later than
  next year are dropped and counted as unknown.
- papers with no year are in no bucket, so the buckets sum to less than the
  total (4,526 vs 4,691 for `drone AND (package OR delivery)`).

So the answer carries unknown_year = total_hits - sum(kept buckets).

Storage, one snapshot per search (`aggregates.search_id` -> `searches.id`):
  field 'yearPublished', bucket '2024', value 523   CORE's buckets, as sent
  field 'totalHits',     bucket 'all',  value 4691  total at the same moment

Raw buckets are stored and filtered on read, so the junk rule can change
without refetching. The totalHits row also marks "cached" for a query with no
matches, whose year list is empty. Freshness follows the search cache's TTL.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import clean
from .cache import DEFAULT_TTL, SearchCache, normalize_query
from .core_client import CoreClient, CoreError

FIELD = "yearPublished"
TOTAL = ("totalHits", "all")


@dataclass(frozen=True)
class YearCounts:
    query: str
    total_hits: int
    years: list[dict[str, int]]  # [{"year": 2019, "count": 250}, ...], oldest first
    unknown_year: int  # no year, or an impossible one
    implausible: dict[str, int]  # dropped buckets, e.g. {"9999": 1}
    status: str  # hit | miss | refresh | stale
    fetched_at: int
    error: str | None = None


class AggregateCache:
    def __init__(self, conn: sqlite3.Connection, core: CoreClient, *, ttl: int = DEFAULT_TTL,
                 clock: Callable[[], float] = time.time):
        self.conn = conn
        self.core = core
        self.ttl = ttl
        self.clock = clock

    def years(self, q: str) -> YearCounts:
        key = normalize_query(q)
        if not key:
            raise ValueError("q must not be blank")
        now = int(self.clock())
        search = self.conn.execute("SELECT id FROM searches WHERE query_key = ?", (key,)).fetchone()
        cached = self._load(search["id"]) if search else None
        if cached and now - cached[2] < self.ttl:
            return self._answer(key, *cached, status="hit")

        # The total comes from a search. It is a cache hit when the app searched
        # first (it always does); otherwise one 1-row CORE call that also seeds the cache.
        total = SearchCache(self.conn, self.core, ttl=self.ttl, clock=self.clock).search(key, 1).total_hits
        search_id = self.conn.execute("SELECT id FROM searches WHERE query_key = ?", (key,)).fetchone()["id"]
        try:
            buckets = self.core.aggregate(key, [FIELD]).buckets[FIELD]
        except CoreError as exc:
            if cached:
                return self._answer(key, *cached, status="stale", error=type(exc).__name__)
            raise
        self._store(search_id, total, buckets, now)
        return self._answer(key, total, buckets, now, status="refresh" if cached else "miss")

    def _load(self, search_id: int) -> tuple[int, dict[str, int], int] | None:
        rows = self.conn.execute(
            "SELECT field, bucket, value, fetched_at FROM aggregates WHERE search_id = ? AND field IN (?, ?)",
            (search_id, FIELD, TOTAL[0]),
        ).fetchall()
        total = next((r for r in rows if (r["field"], r["bucket"]) == TOTAL), None)
        if total is None:
            return None
        buckets = {r["bucket"]: int(r["value"]) for r in rows if r["field"] == FIELD}
        return int(total["value"]), buckets, total["fetched_at"]

    def _store(self, search_id: int, total: int, buckets: dict[str, int], now: int) -> None:
        rows = [(search_id, FIELD, bucket, count, now) for bucket, count in buckets.items()]
        rows.append((search_id, *TOTAL, total, now))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM aggregates WHERE search_id = ? AND field IN (?, ?)",
                              (search_id, FIELD, TOTAL[0]))
            self.conn.executemany(
                "INSERT INTO aggregates (search_id, field, bucket, metric, value, fetched_at)"
                " VALUES (?, ?, ?, 'count', ?, ?)", rows)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    @staticmethod
    def _answer(key: str, total: int, buckets: dict[str, int], fetched_at: int, *, status: str,
                error: str | None = None) -> YearCounts:
        years, implausible = [], {}
        for bucket, count in buckets.items():
            year = int(bucket) if bucket.isascii() and bucket.isdigit() else None
            if clean.year(year, fetched_at) is None:
                implausible[bucket] = count
            else:
                years.append({"year": year, "count": count})
        years.sort(key=lambda y: y["year"])
        known = sum(y["count"] for y in years)
        return YearCounts(query=key, total_hits=total, years=years, unknown_year=max(0, total - known),
                          implausible=implausible, status=status, fetched_at=fetched_at, error=error)
