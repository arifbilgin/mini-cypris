"""The CORE throttle (app/throttle.py): unit tests, then through the API. Fake clock, fake CORE."""
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import create_app
from app.cache import DEFAULT_TTL
from app.throttle import PENALTY, WINDOW, Throttle

T0 = 1_794_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


# ----- the window ----------------------------------------------------------------

def test_allows_the_budget_then_says_how_long_to_wait(clock):
    t = Throttle(3, clock=clock)
    assert [t.acquire() for _ in range(3)] == [0, 0, 0]
    clock.t += 10
    assert t.acquire() == WINDOW - 10  # the oldest slot frees up 50 s from now
    assert t.acquire() == WINDOW - 10  # a refusal takes no slot


def test_window_slides(clock):
    t = Throttle(2, clock=clock)
    t.acquire()
    clock.t += 30
    t.acquire()
    clock.t += 30  # first slot is exactly a minute old
    assert t.acquire() == 0
    assert t.acquire() == 30  # second slot frees at T0 + 90


def test_zero_turns_it_off(clock):
    t = Throttle(0, clock=clock)
    assert all(t.acquire() == 0 for _ in range(100))


def test_penalty_after_a_real_429(clock):
    t = Throttle(9, clock=clock)
    assert t.penalize() == T0 + PENALTY
    assert t.acquire() == PENALTY
    clock.t += PENALTY
    assert t.acquire() == 0


def test_penalty_honours_a_later_reset(clock):
    t = Throttle(9, clock=clock)
    assert t.penalize(T0 + 45) == T0 + 45
    assert t.penalize(T0 + 5) == T0 + 45  # never shortens
    assert t.acquire() == 45


def test_threads_never_exceed_the_budget(clock):
    t = Throttle(9, clock=clock)
    with ThreadPoolExecutor(16) as pool:
        waits = list(pool.map(lambda _: t.acquire(), range(200)))
    assert waits.count(0) == 9


# ----- through the API -----------------------------------------------------------

class FakeCore:
    def __init__(self):
        self.calls = 0
        self.fail = None
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.calls += 1
        if self.fail:
            return self.fail
        if request.method == "POST":
            return httpx.Response(200, json={"aggregations": {"yearPublished": {"2024": 3}}})
        return httpx.Response(200, json={"totalHits": 1, "results": [{"id": 1, "title": "A"}]})


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def client(core, clock, tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "CORE_TRANSPORT": httpx.MockTransport(core),
                       "CLOCK": clock, "CORE_RATE_PER_MINUTE": 3}).test_client()


def search(client, q):
    return client.get("/api/search", query_string={"q": q})


def test_over_budget_is_a_friendly_429_and_core_is_not_called(client, core, clock):
    assert [search(client, q).status_code for q in ("a", "b", "c")] == [200, 200, 200]
    clock.t += 15
    res = search(client, "d")
    assert res.status_code == 429
    assert res.get_json() == {"error": "core_rate_limited", "retry_after": 45,
                              "message": "CORE allows about 10 requests a minute. Try again in 45 s."}
    assert res.headers["Retry-After"] == "45"
    assert core.calls == 3


def test_cached_queries_still_answer_while_throttled(client, core, clock):
    for q in ("a", "b", "c"):
        search(client, q)
    res = search(client, "a")
    assert (res.status_code, res.headers["X-Cache"]) == (200, "HIT")
    assert core.calls == 3


def test_stale_served_when_the_refresh_is_throttled(client, core, clock):
    search(client, "a")
    clock.t += DEFAULT_TTL
    search(client, "x"), search(client, "y"), search(client, "z")  # spend the budget
    res = search(client, "a")
    body = res.get_json()
    assert (res.status_code, res.headers["X-Cache"], body["cache"]["error"]) == (200, "STALE", "CoreRateLimited")


def test_budget_comes_back_after_a_minute(client, core, clock):
    for q in ("a", "b", "c"):
        search(client, q)
    assert search(client, "d").status_code == 429
    clock.t += WINDOW
    assert search(client, "d").status_code == 200


def test_a_real_429_from_core_silences_us_for_the_penalty(client, core, clock):
    core.fail = httpx.Response(429)  # CORE's 429 has an empty body and an optimistic reset header
    res = search(client, "a")
    assert res.status_code == 429
    assert res.get_json()["retry_after"] == PENALTY
    core.fail = None
    clock.t += PENALTY - 1
    assert search(client, "a").status_code == 429
    assert core.calls == 1  # held back, not sent
    clock.t += 1
    assert search(client, "a").status_code == 200


def test_aggregate_takes_slots_too(client, core):
    assert client.get("/api/aggregate", query_string={"q": "a"}).status_code == 200  # 1-row search + aggregate
    assert search(client, "b").status_code == 200
    assert search(client, "c").status_code == 429
    assert core.calls == 3


def test_invalid_queries_cost_no_slot(client, core):
    for _ in range(5):
        assert search(client, "(a AND").status_code == 400
    assert search(client, "a").status_code == 200


def test_budget_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_RATE_PER_MINUTE", "4")
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")})
    assert app.extensions["throttle"].per_minute == 4


def test_default_budget_is_under_cores(tmp_path, monkeypatch):
    monkeypatch.delenv("CORE_RATE_PER_MINUTE", raising=False)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")})
    assert app.extensions["throttle"].per_minute == 9
