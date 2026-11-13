"""Read-through cache for CORE searches, stored in SQLite.

search(q, limit) returns the top `limit` papers for q and says how it got them:

  hit      cached, fresh and deep enough: CORE is not called
  miss     never seen: fetch `limit` rows from CORE, store, serve
  topup    cached and fresh but shallower than `limit`: fetch only the missing
           rows (offset = cached depth) and append them
  refresh  cached but older than the TTL: fetch `limit` rows again from rank 1
           and replace the list
  stale    a refresh was needed but CORE failed: serve the old list anyway
  partial  a top-up was needed but CORE failed: serve the rows we have

If CORE fails and nothing is cached, the CoreError propagates to the route.

Cache key: the query with whitespace collapsed. CORE ignores the case of search
terms but not of field names (`title:` works, `TITLE:` is a 400), so folding
case here could merge two different queries. Smarter keys come with the query
parser.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import papers
from .core_client import CoreClient, CoreError, SearchPage

DEFAULT_TTL = 24 * 3600  # seconds


def normalize_query(q: str) -> str:
    return " ".join(q.split())


@dataclass(frozen=True)
class CachedSearch:
    query: str  # normalized; what was sent to CORE
    total_hits: int  # matches across all of CORE
    papers: list[dict[str, Any]]
    status: str  # hit | miss | topup | refresh | stale | partial
    fetched_at: int  # when the oldest of these rows came from CORE (Unix seconds)
    error: str | None = None  # the CoreError behind a stale/partial answer


class SearchCache:
    def __init__(self, conn: sqlite3.Connection, core: CoreClient, *, ttl: int = DEFAULT_TTL,
                 clock: Callable[[], float] = time.time):
        self.conn = conn
        self.core = core
        self.ttl = ttl
        self.clock = clock

    def search(self, q: str, limit: int) -> CachedSearch:
        key = normalize_query(q)
        if not key:
            raise ValueError("q must not be blank")
        now = int(self.clock())
        cached = self._find(key)

        if cached is None:
            page = self.core.search(key, limit)  # nothing to fall back on: errors propagate
            search_id = self._replace(key, q, page, now)
            return self._answer(search_id, limit, "miss")

        fresh = now - cached["fetched_at"] < self.ttl
        depth = cached["rows_fetched"]
        exhausted = depth >= (cached["total_hits"] or 0)  # CORE has nothing beyond what we hold
        if fresh and (depth >= limit or exhausted):
            return self._answer(cached["id"], limit, "hit")

        try:
            if fresh:
                page = self.core.search(key, limit - depth, offset=depth)
                status = "topup" if self._append(cached["id"], depth, page) else "partial"
            else:
                page = self.core.search(key, limit)
                self._replace(key, q, page, now)
                status = "refresh"
        except CoreError as exc:
            return self._answer(cached["id"], limit, "partial" if fresh else "stale", error=type(exc).__name__)
        return self._answer(cached["id"], limit, status)

    # ----- reads --------------------------------------------------------------

    def _find(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM searches WHERE query_key = ?", (key,)).fetchone()

    def _answer(self, search_id: int, limit: int, status: str, error: str | None = None) -> CachedSearch:
        search = self.conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        rows = self.conn.execute(
            """SELECT p.* FROM search_results r JOIN papers p ON p.id = r.paper_id
               WHERE r.search_id = ? ORDER BY r.rank LIMIT ?""",
            (search_id, limit),
        ).fetchall()
        return CachedSearch(
            query=search["query_key"],
            total_hits=search["total_hits"] or 0,
            papers=[papers.to_api(r) for r in rows],
            status=status,
            fetched_at=search["fetched_at"],
            error=error,
        )

    # ----- writes (each one transaction) --------------------------------------

    @contextmanager
    def _write(self):
        # IMMEDIATE takes the write lock up front, so two threads never both
        # read-then-try-to-write and deadlock; the other waits (busy timeout).
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _replace(self, key: str, typed: str, page: SearchPage, now: int) -> int:
        """Store a page as the whole result list for key (miss or refresh).

        If a parallel request stored this query while we waited for CORE and
        its list is fresh and at least as deep, keep it: a fresh list only ever
        grows, so a top-up running alongside never loses its place.
        """
        depth = page.offset + len(page.records)
        with self._write():
            other = self.conn.execute(
                "SELECT id, rows_fetched, fetched_at FROM searches WHERE query_key = ?", (key,)
            ).fetchone()
            if other and now - other["fetched_at"] < self.ttl and other["rows_fetched"] >= depth:
                return other["id"]
            search_id = self.conn.execute(
                """INSERT INTO searches (query_key, query_text, total_hits, rows_fetched, fetched_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (query_key) DO UPDATE SET
                     query_text = excluded.query_text, total_hits = excluded.total_hits,
                     rows_fetched = excluded.rows_fetched, fetched_at = excluded.fetched_at
                   RETURNING id""",
                (key, typed, page.total_hits, depth, now),
            ).fetchone()[0]
            self.conn.execute("DELETE FROM search_results WHERE search_id = ?", (search_id,))
            self._insert_results(search_id, page.records, now, start_rank=1, seen=set())
        return search_id

    def _append(self, search_id: int, offset: int, page: SearchPage) -> bool:
        """Add a top-up page (CORE rows offset..offset+n) after the rows we hold.

        Another request may have topped up the same search while we waited for
        CORE, so the depth is re-read inside the transaction and only rows past
        it are added. Returns False if our page no longer joins on (a parallel
        refresh shrank the list), in which case nothing is written.
        """
        now = int(self.clock())
        with self._write():
            depth = self.conn.execute("SELECT rows_fetched FROM searches WHERE id = ?", (search_id,)).fetchone()[0]
            if depth < offset:
                return False  # a gap would break CORE's ranking
            self.conn.execute(
                "UPDATE searches SET total_hits = ?, rows_fetched = ? WHERE id = ?",
                (page.total_hits, max(depth, offset + len(page.records)), search_id),
            )
            held = self.conn.execute(
                "SELECT paper_id, rank FROM search_results WHERE search_id = ?", (search_id,)
            ).fetchall()
            next_rank = max((r["rank"] for r in held), default=0) + 1
            self._insert_results(search_id, page.records[depth - offset:], now, start_rank=next_rank,
                                 seen={r["paper_id"] for r in held})
        return True

    def _insert_results(self, search_id: int, records: list[dict], now: int, *, start_rank: int, seen: set[int]):
        rank = start_rank
        for record in records:
            row = papers.from_core(record, now)
            if row is None:
                continue  # no usable id
            paper_id = self._upsert_paper(row)
            if paper_id in seen:
                continue  # CORE's ranking shifted between pages and repeated a paper
            seen.add(paper_id)
            self.conn.execute(
                "INSERT INTO search_results (search_id, rank, paper_id) VALUES (?, ?, ?)",
                (search_id, rank, paper_id),
            )
            rank += 1

    def _upsert_paper(self, row: dict[str, Any]) -> int:
        cols = list(row)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("source", "external_id"))
        return self.conn.execute(
            f"""INSERT INTO papers ({", ".join(cols)}) VALUES ({", ".join("?" for _ in cols)})
                ON CONFLICT (source, external_id) DO UPDATE SET {updates}
                RETURNING id""",
            [row[c] for c in cols],
        ).fetchone()[0]
