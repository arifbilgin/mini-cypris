"""CORE record -> papers row -> API shape."""
import json
from pathlib import Path

import pytest

from app.papers import UNTITLED, from_core, to_api

REAL = json.loads((Path(__file__).parent / "fixtures" / "core_search_drone.json").read_text())["results"]


def row(record):
    r = from_core(record, 123)
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


@pytest.mark.parametrize("value, expected", [(2024, 2024), (9999, 9999), ("2024", None), (2024.0, None), (True, None), (None, None)])
def test_year(value, expected):
    """Type check only; impossible years like 9999 are the cleaning step's job."""
    assert from_core({"id": 1, "yearPublished": value}, 0)["year"] == expected


@pytest.mark.parametrize("field, column", [("doi", "doi"), ("downloadUrl", "pdf_url"), ("publisher", "publisher"), ("abstract", "abstract")])
@pytest.mark.parametrize("value, expected", [("x", "x"), ("  x ", "x"), ("", None), ("  ", None), (None, None), (5, None)])
def test_optional_text_fields(field, column, value, expected):
    assert from_core({"id": 1, field: value}, 0)[column] == expected


def test_abstract_is_not_cleaned_yet():
    assert from_core({"id": 1, "abstract": "&lt;p&gt;Hi&lt;/p&gt;"}, 0)["abstract"] == "&lt;p&gt;Hi&lt;/p&gt;"


def test_to_api_round_trip():
    api = to_api(row({"id": 7, "title": "T", "authors": [{"name": "Ada"}], "yearPublished": 2020, "doi": "10.1/x"}))
    assert api == {"id": 1, "source": "core", "external_id": "7", "title": "T", "authors": ["Ada"], "year": 2020,
                   "publisher": None, "doi": "10.1/x", "pdf_url": None, "abstract": None}


def test_to_api_handles_empty_authors_column():
    r = row({"id": 7})
    r["authors"] = None
    assert to_api(r)["authors"] == []
