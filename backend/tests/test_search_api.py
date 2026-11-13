"""GET /api/search through the SQLite cache, with a fake CORE and a fake clock. No network."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import create_app
from app.cache import DEFAULT_TTL, normalize_query

RATE = {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9", "x-ratelimit-retry-after": "2027-01-15T08:00:30+0000"}
START = 1_800_000_000  # 2027-01-15T08:00:00Z
DAY = DEFAULT_TTL


def rec(paper_id, title=None, **extra):
    return {
        "id": paper_id,
        "title": title or f"Paper {paper_id}",
        "authors": [{"name": f"Author {paper_id}"}, {"name": "Shared Author"}],
        "yearPublished": 2000 + paper_id % 25,
        "publisher": "Pub",
        "doi": f"10.1/{paper_id}",
        "downloadUrl": f"https://example.test/{paper_id}.pdf",
        "abstract": f"Abstract of {paper_id}",
        **extra,
    }


class FakeCore:
    """Behaves like CORE's search endpoint: q, limit, offset over a fixed ranking per query."""

    def __init__(self):
        self.corpus: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, int, int]] = []
        self.fail = None  # request -> Response, or raise
        self.lock = threading.Lock()

    def add(self, q, ids, **extra):
        self.corpus[q] = [rec(i, **extra) for i in ids]

    def __call__(self, request):
        p = request.url.params
        q, limit, offset = p["q"], int(p["limit"]), int(p["offset"])
        with self.lock:
            self.calls.append((q, limit, offset))
        if self.fail:
            return self.fail(request)
        records = self.corpus.get(q, [])
        body = {"totalHits": len(records), "limit": limit, "offset": offset, "results": records[offset:offset + limit]}
        return httpx.Response(200, json=body, headers=RATE)


class Clock:
    def __init__(self, t=START):
        self.t = t

    def __call__(self):
        return self.t


def status(code, headers=None):
    return lambda _req: httpx.Response(code, json={"message": "nope"}, headers=headers or {})


def boom(exc):
    def handler(request):
        raise exc("boom", request=request)

    return handler


@pytest.fixture
def core():
    fake = FakeCore()
    fake.add("drone AND delivery", range(1, 201))  # 200 matches
    fake.add("tiny", [901, 902, 903])
    return fake


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cache.db")


@pytest.fixture
def make_app(core, clock, db_path):
    def build(**config):
        return create_app({"TESTING": True, "DATABASE": db_path, "CORE_TRANSPORT": httpx.MockTransport(core),
                           "CLOCK": clock, **config})
    return build


@pytest.fixture
def client(make_app):
    return make_app().test_client()


def get(client, q="drone AND delivery", limit=None, **params):
    if limit is not None:
        params["limit"] = limit
    return client.get("/api/search", query_string={"q": q, **params})


def ids(res):
    return [p["external_id"] for p in res.get_json()["results"]]


def expected(start, stop):
    return [str(i) for i in range(start, stop)]


def db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ----- input checks (CORE never called) ---------------------------------------


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "   "}, {"q": "\n\t"}])
def test_missing_or_blank_q_is_400(client, core, params):
    res = client.get("/api/search", query_string=params)
    assert res.status_code == 400
    assert res.get_json() == {"error": "bad_request", "message": "q is required"}
    assert core.calls == []


def test_overlong_q_is_400(client, core):
    res = get(client, q="a" * 1001)
    assert res.status_code == 400 and "1000" in res.get_json()["message"]
    assert core.calls == []


def test_q_at_max_length_is_accepted(client):
    assert get(client, q="a" * 1000).status_code == 200


@pytest.mark.parametrize("limit", ["0", "101", "abc", "-1", "+5", "1_0", "５", "10.0", "", "1e2", "99999999999999999999"])
def test_bad_limit_is_400(client, core, limit):
    res = get(client, limit=limit)
    assert res.status_code == 400
    assert res.get_json()["error"] == "bad_request" and "limit" in res.get_json()["message"]
    assert core.calls == []


@pytest.mark.parametrize("limit, n", [("1", 1), ("100", 100), (" 7 ", 7), ("007", 7)])
def test_good_limits(client, limit, n):
    assert get(client, limit=limit).get_json()["count"] == n


def test_default_limit_is_10(client, core):
    body = get(client).get_json()
    assert body["limit"] == 10 and body["count"] == 10
    assert core.calls == [("drone AND delivery", 10, 0)]


# ----- miss and hit -----------------------------------------------------------


def test_first_search_is_a_miss_and_returns_cores_ranking(client, core):
    res = get(client, limit=5)
    body = res.get_json()
    assert res.status_code == 200
    assert res.headers["X-Cache"] == "MISS"
    assert body["cache"] == {"status": "miss", "fetched_at": START, "age_seconds": 0, "error": None}
    assert body["query"] == "drone AND delivery"
    assert body["total_hits"] == 200
    assert body["count"] == 5
    assert ids(res) == expected(1, 6)
    assert core.calls == [("drone AND delivery", 5, 0)]


def test_result_shape(client):
    paper = get(client, limit=1).get_json()["results"][0]
    assert set(paper) == {"id", "source", "external_id", "title", "authors", "year", "publisher", "doi", "pdf_url", "abstract"}
    assert paper["source"] == "core"
    assert paper["external_id"] == "1"
    assert paper["title"] == "Paper 1"
    assert paper["authors"] == ["Author 1", "Shared Author"]
    assert paper["year"] == 2001
    assert paper["doi"] == "10.1/1"
    assert paper["pdf_url"] == "https://example.test/1.pdf"
    assert isinstance(paper["id"], int)


def test_repeat_search_is_a_hit_and_skips_core(client, core, clock):
    get(client, limit=5)
    clock.t += 60
    res = get(client, limit=5)
    assert res.headers["X-Cache"] == "HIT"
    assert res.get_json()["cache"]["age_seconds"] == 60
    assert ids(res) == expected(1, 6)
    assert len(core.calls) == 1


def test_whitespace_variants_share_one_cache_entry(client, core):
    get(client, q="drone AND delivery", limit=5)
    res = get(client, q="  drone   AND\tdelivery \n", limit=5)
    assert res.headers["X-Cache"] == "HIT"
    assert res.get_json()["query"] == "drone AND delivery"
    assert len(core.calls) == 1


def test_case_variants_are_separate_entries(client, core):
    """On purpose: CORE treats field names case-sensitively (TITLE: is a 400)."""
    core.add("DRONE AND DELIVERY", range(1, 50))
    get(client, q="drone AND delivery", limit=5)
    assert get(client, q="DRONE AND DELIVERY", limit=5).headers["X-Cache"] == "MISS"
    assert len(core.calls) == 2


def test_smaller_limit_is_served_from_cache(client, core):
    get(client, limit=20)
    res = get(client, limit=3)
    assert res.headers["X-Cache"] == "HIT"
    assert ids(res) == expected(1, 4)
    assert len(core.calls) == 1


def test_query_sent_to_core_is_normalized(client, core):
    get(client, q="  drone \n AND   delivery  ", limit=2)
    assert core.calls[0][0] == "drone AND delivery"


# ----- top-up -----------------------------------------------------------------


def test_larger_limit_fetches_only_the_missing_rows(client, core):
    get(client, limit=10)
    res = get(client, limit=25)
    assert res.headers["X-Cache"] == "TOPUP"
    assert ids(res) == expected(1, 26)
    assert core.calls == [("drone AND delivery", 10, 0), ("drone AND delivery", 15, 10)]


def test_after_topup_the_deeper_list_is_a_hit(client, core):
    get(client, limit=10)
    get(client, limit=25)
    res = get(client, limit=25)
    assert res.headers["X-Cache"] == "HIT"
    assert ids(res) == expected(1, 26)
    assert len(core.calls) == 2


def test_topup_keeps_the_original_fetch_time(client, clock):
    get(client, limit=10)
    clock.t += 3600
    body = get(client, limit=20).get_json()
    assert body["cache"]["status"] == "topup"
    assert body["cache"]["fetched_at"] == START  # freshness is judged by the oldest rows
    assert body["cache"]["age_seconds"] == 3600


def test_repeated_topups_chain_correctly(client, core):
    for limit in (5, 12, 30, 31, 100):
        res = get(client, limit=limit)
        assert ids(res) == expected(1, limit + 1)
    assert [c[1:] for c in core.calls] == [(5, 0), (7, 5), (18, 12), (1, 30), (69, 31)]


def test_topup_skips_papers_core_already_gave_us(client, core, db_path):
    """CORE's ranking can shift between pages; a paper must not appear twice."""
    get(client, limit=5)  # ids 1..5
    core.corpus["drone AND delivery"] = [rec(i) for i in [1, 2, 3, 4, 5, 3, 4, 6, 7, 8]]
    res = get(client, limit=10)  # CORE's rows 6-10 are 3, 4, 6, 7, 8
    assert ids(res) == ["1", "2", "3", "4", "5", "6", "7", "8"]
    ranks = [r[0] for r in db(db_path).execute("SELECT rank FROM search_results ORDER BY rank")]
    assert ranks == list(range(1, 9))


# ----- small and empty result sets --------------------------------------------


def test_exhausted_results_are_a_hit_for_any_limit(client, core):
    first = get(client, q="tiny", limit=10)
    assert first.headers["X-Cache"] == "MISS" and first.get_json()["count"] == 3
    for limit in (10, 50, 100, 2):
        assert get(client, q="tiny", limit=limit).headers["X-Cache"] == "HIT"
    assert len(core.calls) == 1


def test_zero_hits_are_cached(client, core):
    first = get(client, q="nothing matches this", limit=10)
    assert first.status_code == 200 and first.get_json()["count"] == 0 and first.get_json()["total_hits"] == 0
    again = get(client, q="nothing matches this", limit=50)
    assert again.headers["X-Cache"] == "HIT"
    assert len(core.calls) == 1


# ----- freshness --------------------------------------------------------------


def test_just_under_24h_is_still_a_hit(client, core, clock):
    get(client, limit=5)
    clock.t += DAY - 1
    res = get(client, limit=5)
    assert res.headers["X-Cache"] == "HIT"
    assert res.get_json()["cache"]["age_seconds"] == DAY - 1


def test_at_24h_the_search_is_refreshed(client, core, clock):
    get(client, limit=5)
    core.add("drone AND delivery", range(500, 600))  # CORE's results changed overnight
    clock.t += DAY
    res = get(client, limit=5)
    assert res.headers["X-Cache"] == "REFRESH"
    assert ids(res) == expected(500, 505)
    assert res.get_json()["total_hits"] == 100
    assert res.get_json()["cache"] == {"status": "refresh", "fetched_at": START + DAY, "age_seconds": 0, "error": None}
    assert core.calls[-1] == ("drone AND delivery", 5, 0)


def test_refresh_replaces_the_whole_list(client, core, clock, db_path):
    get(client, limit=50)
    clock.t += DAY
    get(client, limit=10)  # refresh fetches 10 from rank 1
    assert db(db_path).execute("SELECT count(*) FROM search_results").fetchone()[0] == 10
    res = get(client, limit=50)
    assert res.headers["X-Cache"] == "TOPUP"
    assert core.calls[-1] == ("drone AND delivery", 40, 10)
    assert ids(res) == expected(1, 51)


def test_refresh_updates_stored_paper_details(client, core, clock):
    get(client, limit=1)
    core.add("drone AND delivery", [1], title="Paper 1 (corrected)")
    clock.t += DAY
    assert get(client, limit=1).get_json()["results"][0]["title"] == "Paper 1 (corrected)"


def test_ttl_is_configurable(make_app, core, clock):
    client = make_app(CACHE_TTL=60).test_client()
    get(client, limit=5)
    clock.t += 59
    assert get(client, limit=5).headers["X-Cache"] == "HIT"
    clock.t += 1
    assert get(client, limit=5).headers["X-Cache"] == "REFRESH"


def test_ttl_env_var(core, clock, db_path, monkeypatch):
    monkeypatch.setenv("CACHE_TTL_SECONDS", "5")
    app = create_app({"TESTING": True, "DATABASE": db_path, "CORE_TRANSPORT": httpx.MockTransport(core), "CLOCK": clock})
    assert app.config["CACHE_TTL"] == 5


# ----- CORE failures with something cached: degrade, don't fail ---------------


@pytest.mark.parametrize(
    "failure, error",
    [
        (status(500), "CoreUnavailable"),
        (status(429), "CoreRateLimited"),
        (boom(httpx.ReadTimeout), "CoreTimeout"),
        (boom(httpx.ConnectError), "CoreUnavailable"),
        (lambda _r: httpx.Response(200, text="<html>"), "CoreUnexpected"),
        (status(400), "CoreBadQuery"),
    ],
)
def test_expired_entry_is_served_stale_when_core_fails(client, core, clock, failure, error):
    get(client, limit=5)
    clock.t += DAY + 300
    core.fail = failure
    res = get(client, limit=5)
    assert res.status_code == 200
    assert res.headers["X-Cache"] == "STALE"
    assert res.get_json()["cache"] == {"status": "stale", "fetched_at": START, "age_seconds": DAY + 300, "error": error}
    assert ids(res) == expected(1, 6)


def test_stale_answer_does_not_touch_the_cache_and_recovers(client, core, clock):
    get(client, limit=5)
    clock.t += DAY
    core.fail = status(503)
    assert get(client, limit=5).headers["X-Cache"] == "STALE"
    assert get(client, limit=5).headers["X-Cache"] == "STALE"  # tries CORE again each time
    core.fail = None
    assert get(client, limit=5).headers["X-Cache"] == "REFRESH"
    assert get(client, limit=5).headers["X-Cache"] == "HIT"


def test_failed_topup_serves_what_we_have(client, core):
    get(client, limit=10)
    core.fail = boom(httpx.ReadTimeout)
    res = get(client, limit=30)
    body = res.get_json()
    assert res.status_code == 200
    assert res.headers["X-Cache"] == "PARTIAL"
    assert body["count"] == 10 and body["limit"] == 30
    assert body["cache"]["error"] == "CoreTimeout"
    core.fail = None
    assert get(client, limit=30).headers["X-Cache"] == "TOPUP"


# ----- CORE failures with nothing cached: clean JSON errors -------------------


@pytest.mark.parametrize(
    "failure, code, error",
    [
        (status(500), 502, "core_unavailable"),
        (status(503), 502, "core_unavailable"),
        (boom(httpx.ConnectError), 502, "core_unavailable"),
        (boom(httpx.ReadTimeout), 504, "core_timeout"),
        (boom(httpx.ConnectTimeout), 504, "core_timeout"),
        (lambda _r: httpx.Response(200, text="<!DOCTYPE html>"), 502, "core_bad_response"),
        (status(401), 502, "core_bad_response"),
        (status(400), 400, "core_rejected_query"),
        (status(429), 429, "core_rate_limited"),
    ],
)
def test_miss_with_core_failure(client, core, db_path, failure, code, error):
    core.fail = failure
    res = get(client, limit=5)
    assert res.status_code == code
    assert res.get_json()["error"] == error
    assert "X-Cache" not in res.headers
    assert db(db_path).execute("SELECT count(*) FROM searches").fetchone()[0] == 0  # nothing half-stored
    core.fail = None
    assert get(client, limit=5).headers["X-Cache"] == "MISS"


def test_core_rejection_message_reaches_the_user(client, core):
    core.fail = lambda _r: httpx.Response(400, json={"message": "TITLE is not a searchable field."})
    res = get(client, q="TITLE:drone")
    assert res.status_code == 400
    assert "TITLE is not a searchable field" in res.get_json()["message"]


def test_rate_limited_miss_says_when_to_retry(client, core):
    core.fail = status(429, {"x-ratelimit-retry-after": "2027-01-15T08:00:42+0000"})  # START + 42 s
    res = get(client)
    assert res.status_code == 429
    assert res.headers["Retry-After"] == "42"


@pytest.mark.parametrize("headers", [{}, {"x-ratelimit-retry-after": "2020-01-01T00:00:00+0000"}])
def test_rate_limited_miss_retry_after_is_at_least_1(client, core, headers):
    core.fail = status(429, headers)
    assert get(client).headers["Retry-After"] == "1"


# ----- what lands in SQLite ---------------------------------------------------


def test_tables_after_a_miss(client, db_path):
    get(client, q="  drone  AND delivery", limit=3)
    conn = db(db_path)
    (search,) = conn.execute("SELECT * FROM searches").fetchall()
    assert dict(search) | {"id": 0} == {"id": 0, "query_key": "drone AND delivery", "query_text": "  drone  AND delivery",
                                        "total_hits": 200, "rows_fetched": 3, "fetched_at": START}
    results = conn.execute("SELECT rank, external_id FROM search_results JOIN papers ON papers.id = paper_id ORDER BY rank").fetchall()
    assert [tuple(r) for r in results] == [(1, "1"), (2, "2"), (3, "3")]
    paper = conn.execute("SELECT * FROM papers WHERE external_id = '1'").fetchone()
    assert paper["source"] == "core" and paper["fetched_at"] == START
    assert '"id": 1' in paper["raw_json"]


def test_a_paper_found_by_two_searches_is_stored_once(client, core, db_path):
    core.add("drone", [1, 2, 3])  # same ids as "drone AND delivery"
    get(client, limit=3)
    get(client, q="drone", limit=3)
    conn = db(db_path)
    assert conn.execute("SELECT count(*) FROM papers").fetchone()[0] == 3
    assert conn.execute("SELECT count(DISTINCT paper_id) FROM search_results").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM search_results").fetchone()[0] == 6


def test_search_id_survives_refresh_so_aggregates_stay_linked(client, clock, db_path):
    get(client, limit=3)
    conn = db(db_path)
    search_id = conn.execute("SELECT id FROM searches").fetchone()[0]
    conn.execute("INSERT INTO aggregates VALUES (?, 'yearPublished', '2024', 'count', 5, ?)", (search_id, START))
    conn.commit()
    clock.t += DAY
    assert get(client, limit=3).headers["X-Cache"] == "REFRESH"
    assert conn.execute("SELECT id FROM searches").fetchone()[0] == search_id
    assert conn.execute("SELECT count(*) FROM aggregates").fetchone()[0] == 1


def test_latest_typed_text_is_kept(client, clock, db_path):
    get(client, q="drone AND delivery", limit=3)
    clock.t += DAY
    get(client, q=" drone AND  delivery", limit=3)
    assert db(db_path).execute("SELECT query_text FROM searches").fetchone()[0] == " drone AND  delivery"


def test_records_without_an_id_are_skipped(client, core):
    core.corpus["odd"] = [rec(1), {"title": "no id"}, {"id": None}, {"id": ""}, {"id": True}, rec(2)]
    body = get(client, q="odd", limit=6).get_json()
    assert [p["external_id"] for p in body["results"]] == ["1", "2"]
    assert body["total_hits"] == 6


def test_duplicate_records_in_one_page_are_kept_once(client, core):
    core.corpus["dupes"] = [rec(1), rec(2), rec(1), rec(3)]
    assert ids(get(client, q="dupes", limit=4)) == ["1", "2", "3"]


def test_cache_survives_an_app_restart(make_app, core):
    get(make_app().test_client(), limit=5)
    res = get(make_app().test_client(), limit=5)  # new app, same SQLite file
    assert res.headers["X-Cache"] == "HIT"
    assert len(core.calls) == 1


def test_database_uses_wal(client, db_path):
    get(client, limit=1)
    assert db(db_path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_normalize_query():
    assert normalize_query("  a \t AND\n(b  OR c) ") == "a AND (b OR c)"
    assert normalize_query("Title:Drone") == "Title:Drone"  # case kept
    assert normalize_query("   ") == ""


# ----- concurrency (gunicorn runs 4 threads) ----------------------------------


def run_parallel(make_app, calls):
    app = make_app()

    def one(args):
        q, limit = args
        with app.test_client() as c:
            return get(c, q=q, limit=limit)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, calls))


def assert_ranks_consistent(db_path):
    conn = db(db_path)
    for (search_id,) in conn.execute("SELECT id FROM searches"):
        rows = conn.execute("SELECT rank, paper_id FROM search_results WHERE search_id = ? ORDER BY rank", (search_id,)).fetchall()
        assert [r[0] for r in rows] == list(range(1, len(rows) + 1)), "ranks have gaps"
        assert len({r[1] for r in rows}) == len(rows), "a paper appears twice"


def test_parallel_identical_misses(make_app, db_path):
    results = run_parallel(make_app, [("drone AND delivery", 20)] * 16)
    assert all(r.status_code == 200 for r in results)
    assert all(ids(r) == expected(1, 21) for r in results)
    assert db(db_path).execute("SELECT count(*) FROM searches").fetchone()[0] == 1
    assert_ranks_consistent(db_path)


def test_parallel_mixed_limits_and_queries(make_app, core, db_path):
    for i in range(5):
        core.add(f"q{i}", range(i * 1000, i * 1000 + 150))
    calls = [(f"q{i % 5}", 1 + (i * 7) % 100) for i in range(60)] + [("drone AND delivery", n) for n in range(5, 100, 9)]
    results = run_parallel(make_app, calls)
    assert all(r.status_code == 200 for r in results), [r.get_json() for r in results if r.status_code != 200]
    for (q, limit), r in zip(calls, results):
        body = r.get_json()
        start = int(q[1:]) * 1000 if q.startswith("q") else 1
        # every answer is exactly the top `limit` of CORE's ranking, however the requests interleaved
        assert ids(r) == expected(start, start + limit), (q, limit, body["cache"])
    assert_ranks_consistent(db_path)


def test_parallel_hits_never_call_core(make_app, core):
    get(make_app().test_client(), limit=50)
    results = run_parallel(make_app, [("drone AND delivery", n) for n in range(1, 51)] * 2)
    assert all(r.headers["X-Cache"] == "HIT" for r in results)
    assert len(core.calls) == 1


def test_stress_random_timing(make_app, core, db_path):
    """CORE answers after a random 0-5 ms, so requests overlap in every order."""
    import random
    import time as _time

    rng = random.Random(7)
    plain = core.__call__

    def jittery(request):
        _time.sleep(rng.random() * 0.005)
        return plain(request)

    for i in range(3):
        core.add(f"s{i}", range(i * 1000, i * 1000 + 300))
    app = make_app(CORE_TRANSPORT=httpx.MockTransport(jittery))
    calls = [(f"s{rng.randrange(3)}", rng.randint(1, 100)) for _ in range(200)]

    def one(args):
        q, limit = args
        with app.test_client() as c:
            return q, limit, get(c, q=q, limit=limit)

    with ThreadPoolExecutor(max_workers=16) as pool:
        out = list(pool.map(one, calls))
    for q, limit, r in out:
        assert r.status_code == 200
        start = int(q[1:]) * 1000
        assert ids(r) == expected(start, start + limit), (q, limit, r.get_json()["cache"])
    assert_ranks_consistent(db_path)


def test_topup_that_no_longer_joins_on_writes_nothing(make_app, core, db_path):
    """Guard for a refresh that shrank the list while a top-up waited on CORE."""
    from app.cache import SearchCache
    from app.core_client import SearchPage, RateLimit

    app = make_app()
    get(app.test_client(), limit=5)
    with app.app_context():
        from app.db import get_db
        cache = SearchCache(get_db(), app.extensions["core"])
        search_id = get_db().execute("SELECT id FROM searches").fetchone()[0]
        page = SearchPage(total_hits=200, offset=20, limit=5, records=[rec(i) for i in range(21, 26)],
                          rate=RateLimit(None, None, None))
        assert cache._append(search_id, 20, page) is False
    assert db(db_path).execute("SELECT count(*) FROM search_results").fetchone()[0] == 5


def test_failed_write_rolls_back_and_the_connection_still_works(make_app, core, db_path, monkeypatch):
    """A write that dies halfway leaves no half-stored search, and the next write works."""
    from app import papers
    from app.cache import SearchCache
    from app.db import get_db

    real = papers.from_core
    calls = {"n": 0}

    def flaky(record, now):
        calls["n"] += 1
        if calls["n"] == 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real(record, now)

    app = make_app()
    with app.app_context():
        cache = SearchCache(get_db(), app.extensions["core"])
        monkeypatch.setattr(papers, "from_core", flaky)
        with pytest.raises(sqlite3.OperationalError):
            cache.search("drone AND delivery", 5)
        monkeypatch.setattr(papers, "from_core", real)
        assert cache.search("drone AND delivery", 5).status == "miss"  # same connection, not stuck in a transaction
    conn = db(db_path)
    assert conn.execute("SELECT count(*) FROM searches").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM search_results").fetchone()[0] == 5


def test_unexpected_error_is_a_json_500(make_app, monkeypatch):
    from app import cache as cache_module

    def broken(self, q, limit):
        raise RuntimeError("bug")

    monkeypatch.setattr(cache_module.SearchCache, "search", broken)
    client = make_app(PROPAGATE_EXCEPTIONS=False).test_client()
    res = get(client)
    assert res.status_code == 500
    assert res.get_json() == {"error": "internal_error", "message": "something went wrong on our side"}


def test_cache_rejects_blank_query_itself(make_app):
    from app.cache import SearchCache
    from app.db import get_db

    app = make_app()
    with app.app_context(), pytest.raises(ValueError):
        SearchCache(get_db(), app.extensions["core"]).search(" \n ", 5)


def test_non_api_404_in_dev_is_plain_text(client):
    res = client.get("/nope")
    assert res.status_code == 404 and res.data == b"Not found"


def test_query_core_rejects_with_a_500_is_a_400_for_the_user(client, core, db_path):
    """CORE answers HTTP 500 for an unknown field; our user gets a 400 that says why."""
    import json
    from pathlib import Path
    body = json.loads((Path(__file__).parent / "fixtures" / "core_500_bad_field.json").read_text())
    core.fail = lambda _r: httpx.Response(500, json=body)
    res = get(client, q="TITLE:drone")
    assert res.status_code == 400
    assert res.get_json() == {"error": "core_rejected_query",
                              "message": "CORE rejected the query: Illegal arguments in query request: TITLE is not a searchable field."}
    assert db(db_path).execute("SELECT count(*) FROM searches").fetchone()[0] == 0
