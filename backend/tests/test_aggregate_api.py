"""GET /api/aggregate: year counts across all matches, cached, with a fake CORE. No network."""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app import create_app
from app.cache import DEFAULT_TTL

FIXTURES = Path(__file__).parent / "fixtures"
DRONE_YEARS = json.loads((FIXTURES / "core_aggregate_drone.json").read_text())
ZERO_YEARS = json.loads((FIXTURES / "core_aggregate_zero.json").read_text())
DRONE = "drone AND (package OR delivery)"
DRONE_TOTAL = 4691  # CORE's totalHits for DRONE when the fixture was captured
NOV_2026 = 1_794_000_000  # 2026-11-06Z: the latest believable year is 2027
RATE = {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "9"}


class FakeCore:
    """CORE's search (GET) and aggregate (POST) endpoints."""

    def __init__(self):
        self.calls = []  # ("search", q, limit) | ("aggregate", q)
        self.totals = {DRONE: DRONE_TOTAL, "nothing at all": 0}
        self.years = {DRONE: DRONE_YEARS, "nothing at all": ZERO_YEARS}
        self.fail = None  # Response to send instead, for aggregate calls only

    def __call__(self, request):
        if request.method == "POST":
            q = json.loads(request.content)["q"]
            self.calls.append(("aggregate", q))
            if self.fail:
                return self.fail
            return httpx.Response(200, json=self.years.get(q, ZERO_YEARS), headers=RATE)
        q, limit = request.url.params["q"], int(request.url.params["limit"])
        self.calls.append(("search", q, limit))
        total = self.totals.get(q, 0)
        results = [{"id": i, "title": f"Paper {i}"} for i in range(1, min(limit, total) + 1)]
        return httpx.Response(200, json={"totalHits": total, "limit": limit, "offset": 0, "results": results},
                              headers=RATE)


class Clock:
    def __init__(self, t=NOV_2026):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cache.db")


@pytest.fixture
def client(core, clock, db_path):
    app = create_app({"TESTING": True, "DATABASE": db_path, "CORE_TRANSPORT": httpx.MockTransport(core),
                      "CLOCK": clock})
    return app.test_client()


def agg(client, q=DRONE):
    return client.get("/api/aggregate", query_string={"q": q})


def kept(fixture, max_year=2027):
    return {int(k): v for k, v in fixture["aggregations"]["yearPublished"].items() if int(k) <= max_year}


def test_years_across_all_matches(client):
    res = agg(client)
    assert res.status_code == 200
    body = res.get_json()
    years = kept(DRONE_YEARS)
    assert body["query"] == DRONE
    assert body["field"] == "yearPublished"
    assert body["total_hits"] == DRONE_TOTAL
    assert body["years"] == [{"year": y, "count": years[y]} for y in sorted(years)]  # oldest first
    assert body["years"][0] == {"year": 1963, "count": 1}
    assert body["years"][-1] == {"year": 2027, "count": 17}  # next year is believable
    assert body["implausible_years"] == {"2029": 1, "2096": 2, "9999": 1}
    assert body["unknown_year"] == DRONE_TOTAL - sum(years.values())
    assert body["unknown_year"] == 4691 - 4526 + 4  # no year at all (165) + impossible years (4)
    assert body["cache"] == {"status": "miss", "fetched_at": NOV_2026, "age_seconds": 0, "error": None}
    assert res.headers["X-Cache"] == "MISS"


def test_cold_aggregate_costs_one_search_row_and_one_aggregate(client, core):
    agg(client)
    assert core.calls == [("search", DRONE, 1), ("aggregate", DRONE)]


def test_after_a_search_only_the_aggregate_is_fetched(client, core):
    client.get("/api/search", query_string={"q": DRONE, "limit": 5})
    core.calls.clear()
    agg(client)
    assert core.calls == [("aggregate", DRONE)]


def test_second_call_is_a_hit(client, core, clock):
    first = agg(client).get_json()
    core.calls.clear()
    clock.t += 60
    res = agg(client)
    body = res.get_json()
    assert core.calls == []
    assert res.headers["X-Cache"] == "HIT"
    assert body["cache"]["age_seconds"] == 60
    assert {k: body[k] for k in ("years", "unknown_year", "total_hits")} == \
           {k: first[k] for k in ("years", "unknown_year", "total_hits")}


def test_query_is_whitespace_normalized(client, core):
    agg(client)
    core.calls.clear()
    assert agg(client, "  drone   AND (package OR delivery) ").headers["X-Cache"] == "HIT"
    assert core.calls == []


def test_zero_matches_is_cached_too(client, core):
    body = agg(client, "nothing at all").get_json()
    assert (body["total_hits"], body["years"], body["unknown_year"], body["implausible_years"]) == (0, [], 0, {})
    core.calls.clear()
    assert agg(client, "nothing at all").headers["X-Cache"] == "HIT"
    assert core.calls == []


def test_refresh_after_ttl(client, core, clock):
    agg(client)
    clock.t += DEFAULT_TTL
    core.calls.clear()
    res = agg(client)
    assert res.headers["X-Cache"] == "REFRESH"
    assert ("aggregate", DRONE) in core.calls


def test_stale_when_refresh_fails(client, core, clock):
    first = agg(client).get_json()
    clock.t += DEFAULT_TTL + 5
    core.fail = httpx.Response(503)
    res = agg(client)
    body = res.get_json()
    assert res.status_code == 200
    assert res.headers["X-Cache"] == "STALE"
    assert body["cache"]["error"] == "CoreUnavailable"
    assert body["cache"]["age_seconds"] == DEFAULT_TTL + 5
    assert body["years"] == first["years"]


@pytest.mark.parametrize("response, status, code", [
    (httpx.Response(503), 502, "core_unavailable"),
    (httpx.Response(429, headers={"x-ratelimit-retry-after": "2026-11-06T00:00:30+0000"}), 429, "core_rate_limited"),
    (httpx.Response(200, json={"nope": 1}), 502, "core_bad_response"),
    (httpx.Response(400, json={"message": "bad"}), 400, "core_rejected_query"),
])
def test_core_failure_with_nothing_cached(client, core, response, status, code, db_path):
    core.fail = response
    res = agg(client)
    assert res.status_code == status
    assert res.get_json()["error"] == code
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM aggregates").fetchone()[0] == 0


def test_unknown_year_is_never_negative(client, core):
    core.totals[DRONE] = 10  # total moved below the bucket sum between the two calls
    assert agg(client).get_json()["unknown_year"] == 0


def test_non_numeric_buckets_count_as_unknown(client, core):
    core.totals["odd"] = 10
    core.years["odd"] = {"aggregations": {"yearPublished": {"2020": 6, "": 1, "n/a": 2, "0": 1}}}
    body = agg(client, "odd").get_json()
    assert body["years"] == [{"year": 2020, "count": 6}]
    assert body["unknown_year"] == 4
    assert body["implausible_years"] == {"": 1, "n/a": 2, "0": 1}


def test_junk_rule_follows_the_clock(client, clock):
    clock.t = 1_830_000_000  # 2027-12-28Z: 2028 is now believable, 2029 is not yet
    body = agg(client).get_json()
    assert "2029" in body["implausible_years"]


def test_raw_buckets_are_stored_as_sent(client, db_path):
    agg(client)
    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT bucket, value FROM aggregates WHERE field = 'yearPublished'").fetchall())
    assert rows["9999"] == 1 and rows["2026"] == 638  # junk kept in storage, dropped on read
    assert conn.execute("SELECT value FROM aggregates WHERE field = 'totalHits' AND bucket = 'all'").fetchone() == (4691,)


@pytest.mark.parametrize("params, message", [({}, "q is required"), ({"q": "  "}, "q is required"),
                                             ({"q": "x" * 1001}, "longer than 1000")])
def test_bad_input(client, core, params, message):
    res = client.get("/api/aggregate", query_string=params)
    assert res.status_code == 400
    assert res.get_json()["error"] == "bad_request"
    assert message in res.get_json()["message"]
    assert core.calls == []
