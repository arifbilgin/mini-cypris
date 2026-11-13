PRAGMA foreign_keys = ON;

-- One row per paper, stored once no matter how many searches find it
CREATE TABLE papers (
  id          INTEGER PRIMARY KEY,           -- ours
  source      TEXT    NOT NULL,              -- 'core' for now
  external_id TEXT    NOT NULL,              -- source's id (third party)
  title       TEXT    NOT NULL,
  authors     TEXT,                          -- JSON array of cleaned names
  year        INTEGER,
  publisher   TEXT,
  doi         TEXT,
  pdf_url     TEXT,
  abstract    TEXT,                          -- cleaned
  raw_json    TEXT,                          -- source's original record
  fetched_at  INTEGER NOT NULL,              -- Unix seconds, UTC
  UNIQUE (source, external_id)
);

-- One row per normalized query (the cache lookup)
CREATE TABLE searches (
  id           INTEGER PRIMARY KEY,          -- ours
  query_key    TEXT    NOT NULL UNIQUE,      -- normalized: "(DRONE AND DELIVERY)"
  query_text   TEXT    NOT NULL,             -- as typed
  total_hits   INTEGER,                      -- source's total matches
  rows_fetched INTEGER NOT NULL DEFAULT 0,   -- cached depth
  fetched_at   INTEGER NOT NULL              -- Unix seconds, UTC
);

-- Ordered papers returned by a search
CREATE TABLE search_results (
  search_id INTEGER NOT NULL REFERENCES searches(id),
  rank      INTEGER NOT NULL,                -- 1, 2, 3...
  paper_id  INTEGER NOT NULL REFERENCES papers(id),
  PRIMARY KEY (search_id, rank)
);

-- Chart data across ALL matches, one row per bucket per metric
CREATE TABLE aggregates (
  search_id  INTEGER NOT NULL REFERENCES searches(id),
  field      TEXT    NOT NULL,               -- 'yearPublished'
  bucket     TEXT    NOT NULL,               -- '2024'
  metric     TEXT    NOT NULL DEFAULT 'count', -- 'count', 'avg', 'sum'...
  value      REAL    NOT NULL,               -- 7808.0
  fetched_at INTEGER NOT NULL,               -- Unix seconds, UTC
  PRIMARY KEY (search_id, field, bucket, metric)
);
