"""Contract checks against the real CORE API. Skipped by default; run with: pytest -m live

Five small requests. They guard the facts core_client.py relies on, so we
notice if CORE changes behaviour.
"""
import pytest

from app.core_client import MAX_LIMIT, CoreBadQuery, CoreClient

pytestmark = pytest.mark.live

QUERY = "drone AND (package OR delivery)"


@pytest.fixture(scope="module")
def core():
    with CoreClient(user_agent="mini-cypris-tests") as client:
        yield client


def test_search_returns_records_and_rate_headers(core):
    page = core.search(QUERY, limit=5)
    assert page.total_hits > 1000
    assert 1 <= len(page.records) <= 5
    assert all("id" in r and "title" in r for r in page.records)
    assert page.rate.limit == 10
    assert page.rate.remaining is not None and page.rate.resets_at is not None


def test_max_limit_is_still_accepted(core):
    assert len(core.search("drone", limit=MAX_LIMIT).records) == MAX_LIMIT


def test_one_over_max_limit_is_still_a_400(core):
    # bypass our own check on purpose, to prove CORE still enforces the same cap
    with pytest.raises(CoreBadQuery, match="limit"):
        core._call("GET", "/search/works/", "drone", params={"q": "drone", "limit": MAX_LIMIT + 1})


def test_aggregate_counts_by_year(core):
    years = core.aggregate(QUERY).buckets["yearPublished"]
    assert len(years) > 10
    assert sum(years.values()) > 1000


def test_unknown_field_is_still_a_500_wrapped_400(core):
    with pytest.raises(CoreBadQuery, match="not a searchable field"):
        core.search("TITLE:drone", limit=1)
