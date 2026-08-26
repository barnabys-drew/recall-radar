"""The household list: the things this family actually buys."""

import time

from .matcher import Query


def add(conn, label, brand=None, product=None, gtin=None, note=None):
    cur = conn.execute(
        "INSERT INTO watchlist (label, brand, product, gtin, note, created_at) VALUES (?,?,?,?,?,?)",
        (label, brand, product or label, gtin, note, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def remove(conn, watch_id):
    cur = conn.execute("DELETE FROM watchlist WHERE id=?", (watch_id,))
    conn.commit()
    return cur.rowcount


def all_items(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY id")]


def as_queries(conn):
    return [
        Query(label=i["label"], gtin=i["gtin"], brand=i["brand"],
              product=i["product"], watch_id=i["id"])
        for i in all_items(conn)
    ]


def record_alert(conn, watch_id, match):
    """Log a hit and report whether it is new, so an alerting layer built on
    top of this never notifies the same family twice about the same recall."""
    cur = conn.execute(
        """INSERT INTO alerts (watch_id, recall_id, verdict, score, first_seen)
           VALUES (?,?,?,?,?) ON CONFLICT(watch_id, recall_id) DO NOTHING""",
        (watch_id, match.recall["id"], match.verdict, match.score, time.time()),
    )
    conn.commit()
    return cur.rowcount > 0
