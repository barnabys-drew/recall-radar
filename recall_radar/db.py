"""SQLite storage plus the inverted index the matcher scores against."""

import json
import os
import sqlite3
import time

from . import normalize

DEFAULT_DB = os.path.expanduser("~/.local/share/recall-radar/recalls.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recalls (
    id                   TEXT PRIMARY KEY,   -- "<source>:<recall_number>"
    source               TEXT NOT NULL,      -- 'fda' | 'fsis'
    recall_number        TEXT,
    event_id             TEXT,
    status               TEXT,               -- Ongoing | Completed | Terminated
    classification       TEXT,               -- Class I | II | III
    recalling_firm       TEXT,
    product_description  TEXT,
    reason               TEXT,
    distribution_pattern TEXT,
    code_info            TEXT,
    report_date          TEXT,               -- ISO yyyy-mm-dd
    initiation_date      TEXT,
    termination_date     TEXT,
    url                  TEXT,
    raw                  TEXT,
    first_seen           REAL,
    last_seen            REAL
);
CREATE INDEX IF NOT EXISTS idx_recalls_report  ON recalls(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_recalls_status  ON recalls(status);

CREATE TABLE IF NOT EXISTS recall_upcs (
    recall_id  TEXT NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    gtin       TEXT NOT NULL,
    confidence TEXT NOT NULL,
    PRIMARY KEY (recall_id, gtin)
);
CREATE INDEX IF NOT EXISTS idx_upcs_gtin ON recall_upcs(gtin);

CREATE TABLE IF NOT EXISTS recall_states (
    recall_id TEXT NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    state     TEXT NOT NULL,   -- USPS code, or 'US' for nationwide
    PRIMARY KEY (recall_id, state)
);
CREATE INDEX IF NOT EXISTS idx_states_state ON recall_states(state);

-- Inverted index over firm + product description.
CREATE TABLE IF NOT EXISTS recall_tokens (
    token     TEXT NOT NULL,
    recall_id TEXT NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    field     TEXT NOT NULL,   -- 'firm' | 'product'
    PRIMARY KEY (token, recall_id, field)
);
CREATE INDEX IF NOT EXISTS idx_tokens_recall ON recall_tokens(recall_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    brand      TEXT,
    product    TEXT,
    gtin       TEXT,
    note       TEXT,
    created_at REAL
);

-- Dedupe ledger so a future alerting layer never notifies twice.
CREATE TABLE IF NOT EXISTS alerts (
    watch_id   INTEGER NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    recall_id  TEXT NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL,
    score      REAL,
    first_seen REAL,
    notified   INTEGER DEFAULT 0,
    PRIMARY KEY (watch_id, recall_id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path=DEFAULT_DB):
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_recall(conn, rec):
    """Insert or refresh one normalized recall and its derived index rows.

    `rec` is the dict produced by a source adapter (see sources/base.py).
    """
    now = time.time()
    rid = rec["id"]
    conn.execute(
        """INSERT INTO recalls (id, source, recall_number, event_id, status,
               classification, recalling_firm, product_description, reason,
               distribution_pattern, code_info, report_date, initiation_date,
               termination_date, url, raw, first_seen, last_seen)
           VALUES (:id,:source,:recall_number,:event_id,:status,:classification,
               :recalling_firm,:product_description,:reason,:distribution_pattern,
               :code_info,:report_date,:initiation_date,:termination_date,:url,
               :raw,:now,:now)
           ON CONFLICT(id) DO UPDATE SET
               status=excluded.status,
               classification=excluded.classification,
               termination_date=excluded.termination_date,
               raw=excluded.raw,
               last_seen=excluded.last_seen""",
        {**rec, "raw": json.dumps(rec.get("raw", {}), separators=(",", ":")), "now": now},
    )

    # Derived rows are cheap to rebuild and must never drift from the source text.
    conn.execute("DELETE FROM recall_upcs   WHERE recall_id=?", (rid,))
    conn.execute("DELETE FROM recall_states WHERE recall_id=?", (rid,))
    conn.execute("DELETE FROM recall_tokens WHERE recall_id=?", (rid,))

    haystack = " ".join(filter(None, [rec.get("code_info"), rec.get("product_description")]))
    conn.executemany(
        "INSERT OR IGNORE INTO recall_upcs (recall_id, gtin, confidence) VALUES (?,?,?)",
        [(rid, u["gtin"], u["confidence"]) for u in normalize.extract_upcs(haystack)],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO recall_states (recall_id, state) VALUES (?,?)",
        [(rid, s) for s in normalize.states_from_distribution(rec.get("distribution_pattern"))],
    )
    rows = set()
    for field, text in (("firm", rec.get("recalling_firm")), ("product", rec.get("product_description"))):
        for tok in normalize.tokenize(text):
            rows.add((tok, rid, field))
    conn.executemany(
        "INSERT OR IGNORE INTO recall_tokens (token, recall_id, field) VALUES (?,?,?)", rows
    )
    return rid


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def stats(conn):
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "recalls": q("SELECT COUNT(*) FROM recalls"),
        "ongoing": q("SELECT COUNT(*) FROM recalls WHERE status='Ongoing'"),
        "class_i": q("SELECT COUNT(*) FROM recalls WHERE classification='Class I'"),
        "with_upc": q("SELECT COUNT(DISTINCT recall_id) FROM recall_upcs"),
        "newest": q("SELECT COALESCE(MAX(report_date),'-') FROM recalls"),
        "last_sync": get_meta(conn, "last_sync", "never"),
    }
