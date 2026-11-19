"""CORE record -> papers row -> API shape."""
import json
from pathlib import Path

import pytest

from app.papers import UNTITLED, from_core, to_api

NOW = 1_794_000_000  # 2026-11-06T...Z, so the latest believable year is 2027
REAL = json.loads((Path(__file__).parent / "fixtures" / "core_search_drone.json").read_text())["results"]


def row(record):
    r = from_core(record, NOW)
    return r if r is None else {"id": 1, **r}


@pytest.mark.parametrize("record", REAL, ids=[str(r["id"]) for r in REAL])
def test_real_core_records_map_cleanly(record):
    r = from_core(record, 123)
    assert r["source"] == "core"
    assert r["external_id"] == str(record["id"])
    assert r["title"] == record["title"].strip()
    assert isinstance(json.loads(r["authors"]), list)
    assert r["year"] is None or isinstance(r["year"], int)
    assert json.loads(r["raw_json"]) == record  # nothing lost; can be re-cleaned later
    assert r["fetched_at"] == 123


@pytest.mark.parametrize("value, expected", [(147353571, "147353571"), ("abc", "abc"), (" 42 ", "42"), (0, "0")])
def test_ids(value, expected):
    assert from_core({"id": value}, 0)["external_id"] == expected


@pytest.mark.parametrize("value", [None, "", "  ", True, False, 1.5, {"x": 1}, [1]])
def test_unusable_ids(value):
    assert from_core({"id": value, "title": "t"}, 0) is None


def test_missing_id():
    assert from_core({"title": "t"}, 0) is None


@pytest.mark.parametrize("value", [None, "", "   ", 5, ["t"]])
def test_untitled(value):
    assert from_core({"id": 1, "title": value}, 0)["title"] == UNTITLED


def test_title_trimmed():
    assert from_core({"id": 1, "title": "  Drones \n"}, 0)["title"] == "Drones"


@pytest.mark.parametrize(
    "value, expected",
    [
        ([{"name": "Ada"}, {"name": "  Bob "}], ["Ada", "Bob"]),
        (["Ada", "Bob"], ["Ada", "Bob"]),
        ([{"name": ""}, {"name": None}, {}, None, 5, {"name": "Ada"}], ["Ada"]),
        ([{"name": "Çağrı Öz"}], ["Çağrı Öz"]),
        (None, []),
        ("Ada", []),
        ({"name": "Ada"}, []),
    ],
)
def test_authors(value, expected):
    assert json.loads(from_core({"id": 1, "authors": value}, 0)["authors"]) == expected


def test_non_ascii_authors_stored_readably():
    assert "Çağrı" in from_core({"id": 1, "authors": [{"name": "Çağrı"}]}, 0)["authors"]


@pytest.mark.parametrize("value, expected", [(2024, 2024), (1946, 1946), (2027, 2027), (2028, None), (2096, None),
                                             (9999, None), (0, None), (-5, None), ("2024", None), (2024.0, None),
                                             (True, None), (None, None)])
def test_year(value, expected):
    """Next year is believable (journals post-date issues); anything later is junk."""
    assert from_core({"id": 1, "yearPublished": value}, NOW)["year"] == expected


@pytest.mark.parametrize("field, column", [("doi", "doi"), ("downloadUrl", "pdf_url"), ("publisher", "publisher"), ("abstract", "abstract")])
@pytest.mark.parametrize("value, expected", [("x", "x"), ("  x ", "x"), ("", None), ("  ", None), (None, None), (5, None)])
def test_optional_text_fields(field, column, value, expected):
    assert from_core({"id": 1, field: value}, 0)[column] == expected


def test_real_abstract_with_entities_is_cleaned():
    record = next(r for r in REAL if "&lt;" in (r.get("abstract") or ""))
    assert from_core(record, NOW)["abstract"] == "This is the first release for DRONE."  # was &lt;p&gt;...&lt;/p&gt


@pytest.mark.parametrize("value, expected", [
    ("&lt;p&gt;Hi&lt;/p&gt;", "Hi"),
    ("Caf&eacute; &amp; bar", "Café & bar"),
    ("&amp;lt;b&amp;gt;bold&amp;lt;/b&amp;gt;", "bold"),  # double-encoded
    ("<jats:p>Line one</jats:p>\n<jats:p>Line  two</jats:p>", "Line one Line two"),
    ("if a < b and c > d", "if a < b and c > d"),  # not tags
    ("x &lt; 5", "x < 5"),
    ("&lt;p&gt;&lt;/p&gt;", None),  # nothing left
    ("Plain text.", "Plain text."),
])
def test_abstract_cleaning(value, expected):
    assert from_core({"id": 1, "abstract": value}, NOW)["abstract"] == expected


def test_title_entities_and_tags_are_cleaned():
    assert from_core({"id": 1, "title": "<i>In vivo</i> drones &amp; delivery"}, NOW)["title"] == "In vivo drones & delivery"


@pytest.mark.parametrize("value, expected", [
    ("Smith, John (12345)", "Smith, John"),
    ("Smith, John(12345)", "Smith, John"),
    ("Ada (1) Lovelace", "Ada Lovelace"),
    ("M&uuml;ller, Jan", "Müller, Jan"),
    ("Agent (007) Bond (42)", "Agent Bond"),
    ("Team (Alpha)", "Team (Alpha)"),  # not an id
    ("(12345)", None),  # nothing left: dropped
])
def test_author_ids_are_removed(value, expected):
    names = json.loads(from_core({"id": 1, "authors": [{"name": value}]}, NOW)["authors"])
    assert names == ([expected] if expected else [])


@pytest.mark.parametrize("value", ["Hi", "a < b", "Café & bar", "&lt;p&gt;Hi&lt;/p&gt;"])
def test_cleaning_is_idempotent(value):
    once = from_core({"id": 1, "abstract": value, "authors": ["Smith (1)"]}, NOW)
    twice = from_core({"id": 1, "abstract": once["abstract"], "authors": json.loads(once["authors"])}, NOW)
    assert (twice["abstract"], twice["authors"]) == (once["abstract"], once["authors"])


def test_to_api_round_trip():
    api = to_api(row({"id": 7, "title": "T", "authors": [{"name": "Ada"}], "yearPublished": 2020, "doi": "10.1/x"}))
    assert api == {"id": 1, "source": "core", "external_id": "7", "title": "T", "authors": ["Ada"], "year": 2020,
                   "publisher": None, "doi": "10.1/x", "pdf_url": None, "abstract": None,
                   "core_url": "https://core.ac.uk/works/7"}


def test_to_api_handles_empty_authors_column():
    r = row({"id": 7})
    r["authors"] = None
    assert to_api(r)["authors"] == []
