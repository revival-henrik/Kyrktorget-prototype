-- SQLite schema for the prototype.
-- Raw layer: everything the crawler sees, stored untouched.
CREATE TABLE IF NOT EXISTS raw_pages (
    url         TEXT PRIMARY KEY,
    status      INTEGER,
    title       TEXT,
    text        TEXT,
    html        TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url     TEXT,
    url          TEXT NOT NULL,
    method       TEXT,
    status       INTEGER,
    content_type TEXT,
    body         TEXT,
    fetched_at   TEXT NOT NULL,
    UNIQUE (url, method, body)
);

-- Normalised layer: what the website is built from.
CREATE TABLE IF NOT EXISTS organizations (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT,          -- 'församling', 'gård/camping', 'vårdverksamhet', ...
    denomination TEXT,
    address      TEXT,
    postal_code  TEXT,
    city         TEXT,
    municipality TEXT,
    county       TEXT,
    website      TEXT,
    email        TEXT,
    phone        TEXT,
    description  TEXT,
    lat          REAL,
    lng          REAL,
    source_url   TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    org_id   TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (org_id, category)
);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    org_id      TEXT REFERENCES organizations(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    starts_at   TEXT,           -- ISO 8601
    ends_at     TEXT,
    location    TEXT,
    city        TEXT,
    category    TEXT,
    description TEXT,
    source_url  TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    employer     TEXT,
    title        TEXT NOT NULL,
    location     TEXT,
    published_at TEXT,
    deadline     TEXT,
    description  TEXT,
    source_url   TEXT
);

CREATE TABLE IF NOT EXISTS info_pages (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body_html  TEXT,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_org_county ON organizations(county, municipality);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(starts_at);
