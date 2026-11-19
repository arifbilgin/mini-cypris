"""A small syntax check for boolean queries, run before anything reaches CORE.

CORE does not reject a malformed boolean query: `(drone AND ` comes back 200
with results, quietly searching for something other than what was typed. So
we check the parts people get wrong:

- parentheses balance, and none are empty
- quotes close, and none are empty
- AND / OR have a term on both sides; NOT has a term after it

Operators count in any case, because CORE treats `and` like `AND`. Terms next
to each other with no operator are fine (CORE ORs them). Everything else, such
as field names, wildcards and ranges, is left to CORE, whose rejections come
back as core_rejected_query.
"""
from __future__ import annotations

from dataclasses import dataclass

OPERATORS = {"AND", "OR", "NOT"}
BINARY = {"AND", "OR"}


class QueryError(ValueError):
    def __init__(self, message: str, position: int):
        super().__init__(message)
        self.message = message
        self.position = position  # 0-based index into the query as sent


@dataclass(frozen=True)
class Token:
    kind: str  # "(" | ")" | "AND" | "OR" | "NOT" | "TERM"
    text: str
    pos: int


def check(q: str) -> None:
    """Raise QueryError with a message a person can act on, or return None."""
    prev: Token | None = None
    opened: list[Token] = []
    for tok in tokenize(q):
        if tok.kind in BINARY and (prev is None or prev.kind == "("):
            raise _error(f"'{tok.text}' needs a search term before it", tok)
        if tok.kind in BINARY | {")"} and prev is not None and prev.kind in OPERATORS:
            raise _error(f"'{prev.text}' needs a search term after it", prev)
        if tok.kind == "(":
            opened.append(tok)
        elif tok.kind == ")":
            if not opened:
                raise _error("')' has no matching '('", tok)
            if prev is not None and prev.kind == "(":
                raise _error("empty parentheses", prev)
            opened.pop()
        prev = tok
    if prev is not None and prev.kind in OPERATORS:
        raise _error(f"'{prev.text}' needs a search term after it", prev)
    if opened:
        raise _error("'(' is never closed; add a ')'", opened[-1])


def tokenize(q: str) -> list[Token]:
    tokens, i, n = [], 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            i += 1
        elif c in "()":
            tokens.append(Token(c, c, i))
            i += 1
        elif c == '"':
            end = q.find('"', i + 1)
            if end == -1:
                raise _error('the quote is never closed; add a closing "', Token("TERM", c, i))
            if not q[i + 1:end].strip():
                raise _error("empty quotes", Token("TERM", c, i))
            tokens.append(Token("TERM", q[i:end + 1], i))
            i = end + 1
        else:
            start = i
            while i < n and not q[i].isspace() and q[i] not in '()"':
                i += 1
            word = q[start:i]
            kind = word.upper() if word.upper() in OPERATORS else "TERM"
            tokens.append(Token(kind, word, start))
    return tokens


def _error(problem: str, tok: Token) -> QueryError:
    return QueryError(f"{problem[0].upper()}{problem[1:]} (character {tok.pos + 1})", tok.pos)
