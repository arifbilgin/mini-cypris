"""CoreClient against a fake CORE (httpx.MockTransport). No network.

Fixture bodies in tests/fixtures/ are real CORE responses captured 2026-11-13.
"""
import json
import logging
import random
import string
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app import create_app
from app.core_client import (
    MAX_LIMIT,
    Aggregate,
    CoreBadQuery,
    CoreClient,
    CoreError,
    CoreRateLimited,
    CoreTimeout,
    CoreUnavailable,
    CoreUnexpected,
    RateLimit,
    SearchPage,
)

FIXTURES = Path(__file__).parent / "fixtures"
RATE_HEADERS = {
    "x-ratelimit-limit": "10",
    "x-ratelimit-remaining": "9",
    "x-ratelimit-retry-after": "2026-11-13T18:31:22+0000",  # CORE's exact format
}
RESET_TS = int(datetime(2026, 11, 13, 18, 31, 22, tzinfo=timezone.utc).timestamp())
SECRET = "sk-test-SECRET-key-123"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeCore:
    """Stands in for api.core.ac.uk: records each request, answers via handler."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def answer(status=200, body=None, *, text=None, headers=RATE_HEADERS):
    def handler(_request):
        if text is not None:
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return handler


def raises(exc_type):
    def handler(request):
        raise exc_type("boom", request=request)

    return handler


def make(handler, **kwargs):
    fake = FakeCore(handler)
    return CoreClient(transport=httpx.MockTransport(fake), **kwargs), fake


SEARCH_OK = answer(body=fixture("core_search_drone.json"))
AGG_OK = answer(body=fixture("core_aggregate_drone.json"))


# ----- requests we send -------------------------------------------------------


def test_search_sends_get_with_q_limit_offset():
    client, fake = make(SEARCH_OK)
    client.search("drone AND (package OR delivery)", limit=3, offset=20)
    (req,) = fake.requests
    assert req.method == "GET"
    assert req.url.host == "api.core.ac.uk"
    assert req.url.path == "/v3/search/works/"
    assert dict(req.url.params) == {"q": "drone AND (package OR delivery)", "limit": "3", "offset": "20"}


def test_search_offset_defaults_to_zero():
    client, fake = make(SEARCH_OK)
    client.search("drone", limit=3)
    assert fake.requests[0].url.params["offset"] == "0"


def test_search_strips_surrounding_whitespace_from_q():
    client, fake = make(SEARCH_OK)
    client.search("  drone AND delivery \n", limit=3)
    assert fake.requests[0].url.params["q"] == "drone AND delivery"


def test_aggregate_sends_post_with_json_body():
    client, fake = make(AGG_OK)
    client.aggregate("drone AND (package OR delivery)")
    (req,) = fake.requests
    assert req.method == "POST"
    assert req.url.path == "/v3/search/works/aggregate"
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"q": "drone AND (package OR delivery)", "aggregations": ["yearPublished"]}


def test_aggregate_passes_custom_fields():
    body = {"aggregations": {"yearPublished": {"2024": 1}, "documentType": {"research": 2}}}
    client, fake = make(answer(body=body))
    result = client.aggregate("drone", fields=["yearPublished", "documentType"])
    assert json.loads(fake.requests[0].content)["aggregations"] == ["yearPublished", "documentType"]
    assert result.buckets == {"yearPublished": {"2024": 1}, "documentType": {"research": 2}}


def test_aggregate_accepts_tuple_fields():
    client, fake = make(AGG_OK)
    client.aggregate("drone", fields=("yearPublished",))
    assert json.loads(fake.requests[0].content)["aggregations"] == ["yearPublished"]


def test_default_headers():
    client, fake = make(SEARCH_OK)
    client.search("drone", limit=1)
    headers = fake.requests[0].headers
    assert headers["user-agent"] == "mini-cypris"
    assert headers["accept"] == "application/json"
    assert "authorization" not in headers


def test_custom_user_agent():
    client, fake = make(SEARCH_OK, user_agent="mini-cypris/9.9")
    client.search("drone", limit=1)
    assert fake.requests[0].headers["user-agent"] == "mini-cypris/9.9"


def test_api_key_sent_as_bearer():
    client, fake = make(SEARCH_OK, api_key=SECRET)
    client.search("drone", limit=1)
    assert fake.requests[0].headers["authorization"] == f"Bearer {SECRET}"


def test_api_key_is_trimmed():
    client, fake = make(SEARCH_OK, api_key=f"  {SECRET}\n")
    client.search("drone", limit=1)
    assert fake.requests[0].headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize("key", [None, "", "   ", "\n"])
def test_blank_api_key_sends_no_auth_header(key):
    client, fake = make(SEARCH_OK, api_key=key)
    client.search("drone", limit=1)
    assert "authorization" not in fake.requests[0].headers


@pytest.mark.parametrize("base", ["https://mirror.test/v3", "https://mirror.test/v3/", "https://mirror.test/v3//"])
def test_custom_base_url_with_or_without_trailing_slash(base):
    client, fake = make(SEARCH_OK)
    client = CoreClient(base, transport=httpx.MockTransport(fake))
    client.search("drone", limit=1)
    assert str(fake.requests[0].url).startswith("https://mirror.test/v3/search/works/?")


QUERY_CHARS = string.ascii_letters + string.digits + " ()\"'&=?#%+/\\:*~^![]{}<>|,.;-_" + "çğışöüé中文😀\t"


def test_query_round_trips_exactly_fuzz():
    """Whatever the user types arrives at CORE byte-for-byte (after the outer strip)."""
    rng = random.Random(2026_11_13)
    client, fake = make(SEARCH_OK)
    sent = []
    for _ in range(300):
        q = "".join(rng.choice(QUERY_CHARS) for _ in range(rng.randint(1, 80)))
        if not q.strip():
            continue
        client.search(q, limit=1)
        sent.append(q.strip())
    assert [r.url.params["q"] for r in fake.requests] == sent
    assert len(sent) > 250


def test_aggregate_query_round_trips_exactly():
    q = 'title:"drone delivery" AND (çevre OR 中文) & a=b #x 100%'
    client, fake = make(AGG_OK)
    client.aggregate(q)
    assert json.loads(fake.requests[0].content)["q"] == q


# ----- our own input checks (nothing is sent) ---------------------------------


@pytest.mark.parametrize("q", ["", "   ", "\n\t", None, 42, b"drone", ["drone"]])
@pytest.mark.parametrize("call", ["search", "aggregate"])
def test_bad_query_rejected_before_sending(call, q):
    client, fake = make(SEARCH_OK)
    with pytest.raises(ValueError, match="q must be"):
        client.search(q, limit=1) if call == "search" else client.aggregate(q)
    assert fake.requests == []


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, 1000, True, False, 10.0, "10", None])
def test_bad_limit_rejected_before_sending(limit):
    client, fake = make(SEARCH_OK)
    with pytest.raises(ValueError, match="limit"):
        client.search("drone", limit=limit)
    assert fake.requests == []


def test_limit_cap_matches_what_core_enforces():
    """Literal numbers on purpose: CORE answered 200 at 100 and 400 at 101 (2026-11-13)."""
    client, fake = make(SEARCH_OK)
    client.search("drone", limit=100)
    with pytest.raises(ValueError, match="between 1 and 100"):
        client.search("drone", limit=101)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("limit", [1, 50, MAX_LIMIT])
def test_limit_bounds_accepted(limit):
    client, fake = make(SEARCH_OK)
    client.search("drone", limit=limit)
    assert fake.requests[0].url.params["limit"] == str(limit)


@pytest.mark.parametrize("offset", [-1, 1.5, "0", None, True])
def test_bad_offset_rejected_before_sending(offset):
    client, fake = make(SEARCH_OK)
    with pytest.raises(ValueError, match="offset"):
        client.search("drone", limit=1, offset=offset)
    assert fake.requests == []


def test_large_offset_is_allowed():
    client, fake = make(SEARCH_OK)
    client.search("drone", limit=1, offset=9_999)
    assert fake.requests[0].url.params["offset"] == "9999"


@pytest.mark.parametrize("fields", [[], (), "yearPublished", [""], ["  "], [5], ["yearPublished", None]])
def test_bad_aggregate_fields_rejected_before_sending(fields):
    client, fake = make(AGG_OK)
    with pytest.raises(ValueError, match="fields"):
        client.aggregate("drone", fields=fields)
    assert fake.requests == []


# ----- successful responses ---------------------------------------------------


def test_search_parses_real_core_page():
    raw = fixture("core_search_drone.json")
    client, _ = make(answer(body=raw))
    page = client.search("drone", limit=3)
    assert isinstance(page, SearchPage)
    assert page.total_hits == 70744
    assert page.offset == 0 and page.limit == 3
    assert page.records == raw["results"]  # passed through untouched
    assert page.rate == RateLimit(limit=10, remaining=9, resets_at=RESET_TS)


def test_search_leaves_records_raw_for_the_cleaner():
    """HTML entities etc. are fixed by the cleaning step, not here."""
    client, _ = make(SEARCH_OK)
    first = client.search("drone", limit=3).records[0]
    assert "&lt;p&gt;" in first["abstract"]


def test_search_zero_hits_real_response():
    client, _ = make(answer(body=fixture("core_search_zero.json")))
    page = client.search("xqzvwplkjh AND qqqzzzxx", limit=5)
    assert page.total_hits == 0
    assert page.records == []


def test_search_falls_back_to_requested_offset_and_limit():
    client, _ = make(answer(body={"totalHits": 5, "results": []}))
    page = client.search("drone", limit=7, offset=14)
    assert (page.offset, page.limit) == (14, 7)


def test_search_ignores_garbage_echoed_offset_and_limit():
    client, _ = make(answer(body={"totalHits": 5, "offset": "x", "limit": -3, "results": []}))
    page = client.search("drone", limit=7, offset=14)
    assert (page.offset, page.limit) == (14, 7)


def test_search_ignores_unknown_top_level_keys():
    body = {"totalHits": 1, "results": [{"id": 1}], "searchId": "abc", "somethingNew": {"x": 1}}
    client, _ = make(answer(body=body))
    assert client.search("drone", limit=1).records == [{"id": 1}]


def test_aggregate_parses_real_core_response():
    raw = fixture("core_aggregate_drone.json")["aggregations"]["yearPublished"]
    client, _ = make(AGG_OK)
    agg = client.aggregate("drone AND (package OR delivery)")
    assert isinstance(agg, Aggregate)
    assert agg.buckets["yearPublished"] == raw
    assert agg.buckets["yearPublished"]["2026"] == 638
    assert sum(agg.buckets["yearPublished"].values()) == sum(raw.values())
    assert agg.rate.remaining == 9


def test_aggregate_keeps_junk_years_for_the_cleaner():
    """CORE really returns years like 9999 and 2096; filtering is a later step."""
    client, _ = make(AGG_OK)
    years = client.aggregate("drone").buckets["yearPublished"]
    assert {"9999", "2096"} <= years.keys()


def test_aggregate_zero_matches_empty_list_becomes_empty_dict():
    """Real CORE quirk: no matches -> {"yearPublished": []}."""
    client, _ = make(answer(body=fixture("core_aggregate_zero.json")))
    assert client.aggregate("xqzvwplkjh").buckets == {"yearPublished": {}}


def test_aggregate_empty_dict_is_fine_too():
    client, _ = make(answer(body={"aggregations": {"yearPublished": {}}}))
    assert client.aggregate("drone").buckets == {"yearPublished": {}}


def test_aggregate_bucket_keys_are_strings():
    client, _ = make(answer(text='{"aggregations": {"yearPublished": {"2024": 3}}}'))
    assert list(client.aggregate("drone").buckets["yearPublished"]) == ["2024"]


def test_aggregate_returns_only_requested_fields():
    body = {"aggregations": {"yearPublished": {"2024": 1}, "extra": {"x": 2}}}
    client, _ = make(answer(body=body))
    assert client.aggregate("drone").buckets == {"yearPublished": {"2024": 1}}


# ----- malformed 200 responses ------------------------------------------------


CLOUDFLARE_PAGE = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>cf</body></html>"


@pytest.mark.parametrize(
    "text",
    [CLOUDFLARE_PAGE, "", "   ", '{"totalHits": 5, "results": [', "null", "[]", '"hello"', "42", "true"],
    ids=["cloudflare-html", "empty", "blank", "truncated", "null", "list", "string", "number", "bool"],
)
@pytest.mark.parametrize("call", ["search", "aggregate"])
def test_non_object_200_body_is_unexpected(call, text):
    client, _ = make(answer(text=text))
    with pytest.raises(CoreUnexpected) as err:
        client.search("drone", limit=1) if call == "search" else client.aggregate("drone")
    assert err.value.status == 200
    assert err.value.rate.limit == 10


@pytest.mark.parametrize(
    "body",
    [
        {"results": []},
        {"totalHits": None, "results": []},
        {"totalHits": "70744", "results": []},
        {"totalHits": True, "results": []},
        {"totalHits": -1, "results": []},
        {"totalHits": 1.5, "results": []},
        {"totalHits": 1},
        {"totalHits": 1, "results": None},
        {"totalHits": 1, "results": {"id": 1}},
        {"totalHits": 1, "results": [{"id": 1}, "oops"]},
        {"totalHits": 1, "results": [None]},
    ],
)
def test_search_bad_shape_is_unexpected(body):
    client, _ = make(answer(body=body))
    with pytest.raises(CoreUnexpected) as err:
        client.search("drone", limit=1)
    assert err.value.rate is not None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"aggregations": None},
        {"aggregations": []},
        {"aggregations": "yearPublished"},
        {"aggregations": {}},  # requested field missing
        {"aggregations": {"other": {"2024": 1}}},
        {"aggregations": {"yearPublished": None}},
        {"aggregations": {"yearPublished": [{"key": "2024", "count": 1}]}},  # non-empty list
        {"aggregations": {"yearPublished": {"2024": "7"}}},
        {"aggregations": {"yearPublished": {"2024": -1}}},
        {"aggregations": {"yearPublished": {"2024": 1.5}}},
        {"aggregations": {"yearPublished": {"2024": True}}},
        {"aggregations": {"yearPublished": {"2024": None}}},
        {"aggregations": {"yearPublished": "2024"}},
    ],
)
def test_aggregate_bad_shape_is_unexpected(body):
    client, _ = make(answer(body=body))
    with pytest.raises(CoreUnexpected):
        client.aggregate("drone")


def test_aggregate_missing_second_field_is_unexpected():
    client, _ = make(answer(body={"aggregations": {"yearPublished": {"2024": 1}}}))
    with pytest.raises(CoreUnexpected, match="documentType"):
        client.aggregate("drone", fields=["yearPublished", "documentType"])


# ----- HTTP error statuses ----------------------------------------------------


def test_400_real_core_message_is_bad_query():
    client, _ = make(answer(400, fixture("core_400_limit.json")))
    with pytest.raises(CoreBadQuery) as err:
        client.search("drone", limit=1)
    assert "limit is too large" in str(err.value)
    assert err.value.status == 400
    assert err.value.rate.remaining == 9


def test_400_plain_text_body():
    client, _ = make(answer(400, text="bad things"))
    with pytest.raises(CoreBadQuery, match="bad things"):
        client.search("drone", limit=1)


def test_400_empty_body():
    client, _ = make(answer(400, text=""))
    with pytest.raises(CoreBadQuery, match="empty body"):
        client.search("drone", limit=1)


def test_400_json_without_message_uses_body_text():
    client, _ = make(answer(400, {"error": "nope"}))
    with pytest.raises(CoreBadQuery, match="nope"):
        client.search("drone", limit=1)


@pytest.mark.parametrize("body", [{"text": "x" * 50_000}, {"body": {"message": "x" * 50_000}}], ids=["text", "json"])
def test_error_message_is_capped(body):
    client, _ = make(answer(400, **body))
    with pytest.raises(CoreBadQuery) as err:
        client.search("drone", limit=1)
    assert len(str(err.value)) < 400


def test_429_with_core_headers():
    client, _ = make(answer(429, {"message": "Too many requests"}))
    with pytest.raises(CoreRateLimited) as err:
        client.search("drone", limit=1)
    assert err.value.status == 429
    assert err.value.resets_at == RESET_TS
    assert err.value.rate.limit == 10


def test_429_with_only_standard_retry_after_seconds(monkeypatch):
    monkeypatch.setattr("app.core_client.time.time", lambda: 1_800_000_000.7)
    client, _ = make(answer(429, text="slow down", headers={"Retry-After": "30"}))
    with pytest.raises(CoreRateLimited) as err:
        client.search("drone", limit=1)
    assert err.value.resets_at == 1_800_000_030


def test_429_with_http_date_retry_after():
    client, _ = make(answer(429, text="", headers={"Retry-After": "Fri, 13 Nov 2026 18:31:22 GMT"}))
    with pytest.raises(CoreRateLimited) as err:
        client.aggregate("drone")
    assert err.value.resets_at == RESET_TS


def test_429_without_headers():
    client, _ = make(answer(429, text="", headers={}))
    with pytest.raises(CoreRateLimited) as err:
        client.search("drone", limit=1)
    assert err.value.resets_at is None
    assert err.value.rate == RateLimit(None, None, None)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 520, 599])
def test_5xx_is_unavailable(status):
    client, _ = make(answer(status, text="oops"))
    with pytest.raises(CoreUnavailable) as err:
        client.search("drone", limit=1)
    assert not isinstance(err.value, CoreTimeout)
    assert err.value.status == status


@pytest.mark.parametrize("status", [201, 204, 301, 302, 304, 401, 403, 404, 405, 409, 410, 413, 414, 418, 422, 451])
def test_other_statuses_are_unexpected(status):
    client, fake = make(answer(status, text="", headers={**RATE_HEADERS, "Location": "https://elsewhere.test/"}))
    with pytest.raises(CoreUnexpected) as err:
        client.search("drone", limit=1)
    assert err.value.status == status
    assert f"HTTP {status}" in str(err.value)
    assert len(fake.requests) == 1  # redirects are not followed


# ----- transport failures -----------------------------------------------------


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout])
def test_timeouts(exc):
    client, _ = make(raises(exc))
    with pytest.raises(CoreTimeout) as err:
        client.search("drone", limit=1)
    assert isinstance(err.value, CoreUnavailable)  # a timeout is a kind of "unavailable"
    assert isinstance(err.value.__cause__, exc)
    assert err.value.status is None and err.value.rate is None


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.LocalProtocolError,
            httpx.ProxyError, httpx.UnsupportedProtocol, httpx.NetworkError]
)
def test_connection_failures_are_unavailable(exc):
    client, _ = make(raises(exc))
    with pytest.raises(CoreUnavailable) as err:
        client.aggregate("drone")
    assert not isinstance(err.value, CoreTimeout)
    assert isinstance(err.value.__cause__, exc)


def test_undecodable_body_is_unexpected():
    client, _ = make(raises(httpx.DecodingError))
    with pytest.raises(CoreUnexpected):
        client.search("drone", limit=1)


def test_garbled_gzip_body_is_unexpected():
    """A body that claims gzip but isn't: httpx raises DecodingError while reading."""
    def handler(_request):
        return httpx.Response(200, content=b"not gzip at all", headers={"Content-Encoding": "gzip"})

    client, _ = make(handler)
    with pytest.raises(CoreUnexpected):
        client.search("drone", limit=1)


def test_unknown_exceptions_are_not_swallowed():
    """Programming errors must surface, not turn into 'CORE is down'."""
    def handler(_request):
        raise RuntimeError("bug in our code")

    client, _ = make(handler)
    with pytest.raises(RuntimeError):
        client.search("drone", limit=1)


# ----- behaviour across the board ---------------------------------------------


FAILURES = [
    answer(400, text="no"),
    answer(429, text=""),
    answer(500, text=""),
    answer(404, text=""),
    answer(text="<html>"),
    raises(httpx.ReadTimeout),
    raises(httpx.ConnectError),
]


@pytest.mark.parametrize("handler", FAILURES)
@pytest.mark.parametrize("call", ["search", "aggregate"])
def test_every_failure_is_a_core_error_and_is_not_retried(call, handler):
    client, fake = make(handler)
    with pytest.raises(CoreError):
        client.search("drone", limit=1) if call == "search" else client.aggregate("drone")
    assert len(fake.requests) == 1


@pytest.mark.parametrize("handler", FAILURES)
def test_api_key_never_leaks_into_errors_or_logs(handler, caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = make(handler, api_key=SECRET)
    with pytest.raises(CoreError) as err:
        client.search("drone", limit=1)
    assert SECRET not in str(err.value)
    assert SECRET not in repr(err.value.__dict__)
    assert SECRET not in caplog.text


def test_logs_one_line_per_call(caplog):
    caplog.set_level(logging.INFO, logger="app.core_client")
    client, _ = make(SEARCH_OK)
    client.search("drone AND delivery", limit=3)
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    msg = record.getMessage()
    assert "GET /search/works/" in msg and "-> 200" in msg and "remaining=9" in msg
    assert "'drone AND delivery'" in msg


def test_failures_log_a_warning(caplog):
    caplog.set_level(logging.INFO, logger="app.core_client")
    client, _ = make(raises(httpx.ReadTimeout))
    with pytest.raises(CoreTimeout):
        client.search("drone", limit=1)
    (record,) = caplog.records
    assert record.levelno == logging.WARNING and "timeout" in record.getMessage()


def test_long_query_is_truncated_in_logs(caplog):
    caplog.set_level(logging.INFO, logger="app.core_client")
    client, _ = make(SEARCH_OK)
    client.search("drone " * 500, limit=1)
    assert len(caplog.records[0].getMessage()) < 300


def test_client_is_thread_safe():
    """gunicorn runs 4 threads per worker; they all share one client."""
    client, fake = make(SEARCH_OK)
    with ThreadPoolExecutor(max_workers=10) as pool:
        pages = list(pool.map(lambda i: client.search(f"drone {i}", limit=3), range(100)))
    assert all(p.total_hits == 70744 for p in pages)
    assert sorted(r.url.params["q"] for r in fake.requests) == sorted(f"drone {i}" for i in range(100))


def test_one_client_survives_errors_between_calls():
    responses = iter([answer(500, text="")(None), SEARCH_OK(None), answer(429, text="")(None), SEARCH_OK(None)])
    client, _ = make(lambda _req: next(responses))
    with pytest.raises(CoreUnavailable):
        client.search("drone", limit=1)
    assert client.search("drone", limit=1).total_hits == 70744
    with pytest.raises(CoreRateLimited):
        client.search("drone", limit=1)
    assert client.search("drone", limit=1).total_hits == 70744


def test_default_timeouts():
    client = CoreClient()
    timeout = client._http.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5.0, 20.0, 20.0, 20.0)
    client.close()


def test_custom_timeouts():
    client = CoreClient(timeout=3.0, connect_timeout=1.0)
    assert (client._http.timeout.connect, client._http.timeout.read) == (1.0, 3.0)
    client.close()


def test_context_manager_closes_connections():
    with CoreClient() as client:
        assert not client._http.is_closed
    assert client._http.is_closed


# ----- rate-limit header parsing ----------------------------------------------


def rate(headers, now=1_800_000_000.0):
    # real headers arrive as bytes off the wire; that also lets us send non-ASCII junk
    raw = [(k.encode(), v.encode("utf-8")) for k, v in headers.items()]
    return RateLimit.from_headers(httpx.Headers(raw), now=now)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-13T18:31:22+0000", RESET_TS),  # CORE's format
        ("2026-11-13T18:31:22Z", RESET_TS),
        ("2026-11-13T18:31:22+00:00", RESET_TS),
        ("2026-11-13T20:31:22+02:00", RESET_TS),  # converted to UTC
        ("2026-11-13T20:31:22+0200", RESET_TS),
        ("2026-11-13T18:31:22", RESET_TS),  # no zone: assume UTC
        ("2026-11-13T18:31:22.900+0000", RESET_TS),  # fractions dropped
        ("Fri, 13 Nov 2026 18:31:22 GMT", RESET_TS),
        ("  2026-11-13T18:31:22+0000  ", RESET_TS),
        ("30", 1_800_000_030),  # seconds from now
        ("0", 1_800_000_000),
    ],
)
def test_reset_formats(value, expected):
    assert rate({"x-ratelimit-retry-after": value}).resets_at == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "soon", "-5", "1e3", "3.5", "²", "٣", "9" * 5000, "2026-13-45T99:99:99+0000", "null", "NaN", "\x00"],
)
def test_reset_garbage_is_none(value):
    assert rate({"x-ratelimit-retry-after": value}).resets_at is None


def test_core_header_wins_over_standard_retry_after():
    r = rate({"x-ratelimit-retry-after": "2026-11-13T18:31:22+0000", "retry-after": "30"})
    assert r.resets_at == RESET_TS


def test_header_names_are_case_insensitive():
    r = rate({"X-RateLimit-Limit": "10", "X-RATELIMIT-REMAINING": "3"})
    assert (r.limit, r.remaining) == (10, 3)


@pytest.mark.parametrize("value, expected", [("10", 10), (" 7 ", 7), ("0", 0), ("ten", None), ("1.5", None), ("", None)])
def test_limit_and_remaining_parsing(value, expected):
    r = rate({"x-ratelimit-limit": value, "x-ratelimit-remaining": value})
    assert r.limit == expected and r.remaining == expected


def test_missing_headers_are_all_none():
    assert rate({}) == RateLimit(None, None, None)


def test_rate_header_fuzz_never_raises():
    rng = random.Random(42)
    alphabet = string.printable + "²٣中😀+-:TZ"
    for _ in range(2000):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        raw = value.encode("utf-8")
        headers = httpx.Headers([(b"x-ratelimit-retry-after", raw), (b"x-ratelimit-limit", raw), (b"x-ratelimit-remaining", raw)])
        r = RateLimit.from_headers(headers, now=1_800_000_000.0)
        for v in (r.limit, r.remaining, r.resets_at):
            assert v is None or isinstance(v, int)


# ----- app wiring -------------------------------------------------------------


def test_app_builds_one_client_with_our_version(tmp_path):
    fake = FakeCore(SEARCH_OK)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "CORE_TRANSPORT": httpx.MockTransport(fake)})
    core = app.extensions["core"]
    assert isinstance(core, CoreClient)
    core.search("drone", limit=1)
    assert fake.requests[0].headers["user-agent"] == "mini-cypris/0.1.0"


def test_app_reads_core_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_BASE_URL", "https://mirror.test/v3")
    monkeypatch.setenv("CORE_API_KEY", SECRET)
    monkeypatch.setenv("CORE_TIMEOUT", "7.5")
    http = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")}).extensions["core"]._http
    assert str(http.base_url) == "https://mirror.test/v3/"
    assert http.headers["authorization"] == f"Bearer {SECRET}"
    assert http.timeout.read == 7.5


def test_app_defaults_without_env(tmp_path, monkeypatch):
    for name in ("CORE_BASE_URL", "CORE_API_KEY", "CORE_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    http = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")}).extensions["core"]._http
    assert str(http.base_url) == "https://api.core.ac.uk/v3/"
    assert "authorization" not in http.headers
    assert http.timeout.read == 20.0


def test_empty_api_key_env_means_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "")
    http = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")}).extensions["core"]._http
    assert "authorization" not in http.headers


def test_bad_timeout_env_fails_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_TIMEOUT", "soon")
    with pytest.raises(ValueError):
        create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")})
