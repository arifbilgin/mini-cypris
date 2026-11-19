"""The boolean query check (app/query.py) and its 400 on both endpoints."""
import httpx
import pytest

from app import create_app
from app.query import QueryError, check, tokenize


@pytest.mark.parametrize("q", [
    "drone",
    "drone AND (package OR delivery)",
    "(drone AND (package OR delivery))",
    "drone delivery",  # no operator: CORE ORs the terms
    "drone and (package or delivery)",  # lowercase operators
    "NOT drone",
    "drone AND NOT military",
    "drone NOT military",
    "(NOT drone) OR uav",
    '"drone delivery" AND package',
    'title:"drone delivery"',
    "title:(drone OR uav) AND yearPublished>2020",
    "dron* AND deliver?",
    "a < b",
    "((((drone))))",
    "AND-gate OR ORCID",  # operators only as whole words
    '"(not a paren" OR x',  # parentheses inside quotes are text
])
def test_valid_queries_pass(q):
    assert check(q) is None


@pytest.mark.parametrize("q, message, position", [
    ("(drone AND ", "'AND' needs a search term after it (character 8)", 7),
    ("drone AND", "'AND' needs a search term after it", 6),
    ("drone OR", "'OR' needs a search term after it", 6),
    ("drone NOT", "'NOT' needs a search term after it", 6),
    ("AND drone", "'AND' needs a search term before it (character 1)", 0),
    ("or drone", "'or' needs a search term before it", 0),
    ("drone AND OR uav", "'AND' needs a search term after it", 6),
    ("drone OR AND uav", "'OR' needs a search term after it", 6),
    ("drone NOT AND uav", "'NOT' needs a search term after it", 6),
    ("drone AND (OR uav)", "'OR' needs a search term before it", 11),
    ("(drone AND) uav", "'AND' needs a search term after it", 7),
    ("drone AND NOT", "'NOT' needs a search term after it", 10),
    ("(drone", "'(' is never closed; add a ')' (character 1)", 0),
    ("drone AND (package OR delivery", "'(' is never closed", 10),
    ("((drone)", "'(' is never closed", 0),
    ("drone)", "')' has no matching '(' (character 6)", 5),
    ("drone) AND (uav", "')' has no matching '('", 5),
    ("drone ()", "Empty parentheses (character 7)", 6),
    ('"drone delivery', 'The quote is never closed; add a closing " (character 1)', 0),
    ('drone AND ""', "Empty quotes (character 11)", 10),
    ("AND", "'AND' needs a search term before it", 0),
    ("NOT", "'NOT' needs a search term after it", 0),
])
def test_invalid_queries_say_what_is_wrong(q, message, position):
    with pytest.raises(QueryError) as err:
        check(q)
    assert message in err.value.message
    assert err.value.position == position


def test_tokens():
    assert [(t.kind, t.text) for t in tokenize('(drone and "x y")')] == [
        ("(", "("), ("TERM", "drone"), ("AND", "and"), ("TERM", '"x y"'), (")", ")")]


# ----- through the API: rejected before CORE is called -------------------------

class CountingCore:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        if request.method == "POST":
            return httpx.Response(200, json={"aggregations": {"yearPublished": []}})
        return httpx.Response(200, json={"totalHits": 0, "results": []})


@pytest.fixture
def core():
    return CountingCore()


@pytest.fixture
def client(tmp_path, core):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "c.db"),
                       "CORE_TRANSPORT": httpx.MockTransport(core)}).test_client()


@pytest.mark.parametrize("path", ["/api/search", "/api/aggregate"])
def test_invalid_query_is_a_400_with_the_reason(client, core, path):
    res = client.get(path, query_string={"q": "(drone AND "})
    assert res.status_code == 400
    assert res.get_json() == {"error": "invalid_query",
                              "message": "'AND' needs a search term after it (character 8)", "position": 7}
    assert core.calls == 0


@pytest.mark.parametrize("path", ["/api/search", "/api/aggregate"])
def test_valid_query_reaches_core(client, core, path):
    assert client.get(path, query_string={"q": "drone AND (package OR delivery)"}).status_code == 200
    assert core.calls >= 1
