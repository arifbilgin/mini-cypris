"""Small, safe fixes for CORE's messier fields.

Measured on a 50-record sample (2026-11-13): 6 abstracts carry HTML entities,
sometimes double-encoded or cut off mid-entity (`&lt;/p&gt`), and some author
names end in a numeric id, e.g. "Smith, John (12345)". Years like 9999 or 2096
also appear.

Every function here is idempotent: cleaning clean text changes nothing.
"""
import html
import re
import time

# Only things that look like real tags (<p>, </jats:p>, <br/>), so "a < b > c" survives.
_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
_SPACE = re.compile(r"\s+")
_AUTHOR_ID = re.compile(r"\s*\(\d+\)")


def text(value: str) -> str:
    """Decode HTML entities (up to twice, for double-encoded text), drop tags, collapse whitespace."""
    for _ in range(2):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    value = _TAG.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def author(name: str) -> str:
    """'Smith, John (12345)' -> 'Smith, John'."""
    return _SPACE.sub(" ", _AUTHOR_ID.sub("", html.unescape(name))).strip()


def max_year(now: float) -> int:
    """The latest believable publication year: next year (preprints and journals post-date)."""
    return time.gmtime(now).tm_year + 1


def year(value: int | None, now: float) -> int | None:
    """None for impossible years such as 9999, 2096 or 0."""
    if value is None or not 1 <= value <= max_year(now):
        return None
    return value
