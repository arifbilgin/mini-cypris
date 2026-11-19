"""Turn a raw CORE record into a `papers` row, and a row into the API's paper shape.

It picks and type-checks fields, then cleans them (app/clean.py): HTML
entities and tags in titles and abstracts, "(12345)" ids in author names,
impossible years. The original record is kept in raw_json, so cached rows can
be re-cleaned if the rules change.
"""
import json
from typing import Any

from . import clean

SOURCE = "core"
UNTITLED = "(untitled)"
CORE_WORK_URL = "https://core.ac.uk/works/{}"


def from_core(record: dict[str, Any], fetched_at: int) -> dict[str, Any] | None:
    """A papers row, or None if the record has no usable id (it can't be cached)."""
    external_id = record.get("id")
    if isinstance(external_id, bool) or not isinstance(external_id, (int, str)) or not str(external_id).strip():
        return None
    return {
        "source": SOURCE,
        "external_id": str(external_id).strip(),
        "title": _clean(record.get("title")) or UNTITLED,
        "authors": json.dumps(_authors(record.get("authors")), ensure_ascii=False),
        "year": clean.year(_year(record.get("yearPublished")), fetched_at),
        "publisher": _clean(record.get("publisher")),
        "doi": _text(record.get("doi")),
        "pdf_url": _text(record.get("downloadUrl")),
        "abstract": _clean(record.get("abstract")),
        "raw_json": json.dumps(record, ensure_ascii=False),
        "fetched_at": fetched_at,
    }


def to_api(row) -> dict[str, Any]:
    """The paper as /api/search returns it."""
    return {
        "id": row["id"],
        "source": row["source"],
        "external_id": row["external_id"],
        "title": row["title"],
        "authors": json.loads(row["authors"]) if row["authors"] else [],
        "year": row["year"],
        "publisher": row["publisher"],
        "doi": row["doi"],
        "pdf_url": row["pdf_url"],
        "abstract": row["abstract"],
        "core_url": CORE_WORK_URL.format(row["external_id"]) if row["source"] == SOURCE else None,
    }


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _clean(value: Any) -> str | None:
    value = _text(value)
    return clean.text(value) or None if value else None


def _authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = []
    for author in value:
        name = author.get("name") if isinstance(author, dict) else author
        name = _text(name)
        name = clean.author(name) if name else None
        if name:
            names.append(name)
    return names


def _year(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
