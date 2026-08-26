"""Pull every source into the local database.

A source that fails is reported and skipped, never fatal: partial data is
useful, and FSIS in particular sits behind bot protection that may break.
"""

import datetime as _dt
import time

from . import db
from .sources import fsis, openfda
from .sources.base import SourceError

SOURCES = {"fda": openfda, "fsis": fsis}

# Agencies edit recalls in place after publication (status, termination date),
# so every sync re-reads a trailing window rather than only new dates.
OVERLAP_DAYS = 30


def _default_since(conn, source):
    row = conn.execute(
        "SELECT MAX(report_date) AS d FROM recalls WHERE source=?", (source,)
    ).fetchone()
    if not row or not row["d"]:
        return None
    newest = _dt.date.fromisoformat(row["d"])
    return (newest - _dt.timedelta(days=OVERLAP_DAYS)).isoformat()


def sync(conn, sources=None, since=None, full=False, progress=None):
    """Returns {source: {'added':int,'updated':int,'error':str|None}}."""
    report = {}
    for name in (sources or SOURCES):
        module = SOURCES[name]
        start = since if (since or full) else _default_since(conn, name)
        if full:
            start = None
        stat = {"added": 0, "updated": 0, "error": None}
        before = {r["id"] for r in conn.execute("SELECT id FROM recalls WHERE source=?", (name,))}
        try:
            for i, rec in enumerate(module.fetch(since=start), 1):
                is_new = rec["id"] not in before
                db.upsert_recall(conn, rec)
                stat["added" if is_new else "updated"] += 1
                if progress and i % 250 == 0:
                    progress(f"  {name}: {i} records...")
            conn.commit()
        except SourceError as e:
            conn.rollback()
            stat["error"] = str(e)
        report[name] = stat

    db.set_meta(conn, "last_sync", _dt.datetime.now().replace(microsecond=0).isoformat())
    conn.commit()
    return report
