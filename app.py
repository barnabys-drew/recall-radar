"""Local web UI for recall-radar.

The browser layer is a thin shell over the same core the CLI uses: every
answer here comes from matcher.check, so the two front-ends can never drift
into disagreeing about whether something is recalled.

    python3 app.py            # http://localhost:5055
"""

import datetime as _dt
import os

from flask import Flask, g, jsonify, render_template, request

from recall_radar import db, matcher, sync, watchlist
from recall_radar.matcher import CERTAIN, LIKELY, POSSIBLE, Query

app = Flask(__name__)
DB_PATH = os.environ.get("RECALL_RADAR_DB", db.DEFAULT_DB)


def conn():
    if "db" not in g:
        g.db = db.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def _close(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def _match_json(m):
    r = m.recall
    desc = (r.get("product_description") or "").strip()
    codes = (r.get("code_info") or "").strip()
    return {
        "verdict": m.verdict,
        "score": m.score,
        "reasons": m.reasons,
        "id": r.get("id"),
        "source": (r.get("source") or "").upper(),
        "recall_number": r.get("recall_number"),
        "classification": r.get("classification") or "Unclassified",
        "firm": r.get("recalling_firm") or "Unknown firm",
        "product": desc,
        "reason": r.get("reason") or "",
        "distribution": r.get("distribution_pattern") or "",
        # The FSIS adapter maps product items into code_info; showing both
        # would repeat the same text under two labels.
        "codes": codes if codes and codes != desc else "",
        "report_date": r.get("report_date"),
        "status": r.get("status"),
        "url": r.get("url"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    c = conn()
    s = db.stats(c)
    by_source = [
        {"source": r["source"].upper(), "count": r["n"], "newest": r["newest"]}
        for r in c.execute(
            "SELECT source, COUNT(*) n, MAX(report_date) newest FROM recalls GROUP BY source")
    ]
    open_class_i = c.execute(
        "SELECT COUNT(*) FROM recalls WHERE status='Ongoing' AND classification LIKE 'Class I'"
    ).fetchone()[0]
    return jsonify({**s, "open_class_i": open_class_i, "by_source": by_source})


@app.route("/api/check")
def api_check():
    q_text = (request.args.get("q") or "").strip()
    upc = (request.args.get("upc") or "").strip()
    state = (request.args.get("state") or "").strip() or None
    include_closed = request.args.get("all") == "1"

    if upc:
        query = Query.from_barcode(upc, label=upc)
        if not query.gtin:
            return jsonify({"error": f"'{upc}' is not a 12-14 digit barcode.",
                            "matches": []}), 400
    elif q_text:
        query = Query.from_text(q_text)
    else:
        return jsonify({"matches": [], "query": ""})

    matches = matcher.check(conn(), query, include_closed=include_closed,
                            state=state, limit=25)
    return jsonify({
        "query": query.label,
        "searched": db.stats(conn())["recalls"],
        "matches": [_match_json(m) for m in matches],
    })


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    c = conn()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        label = (data.get("label") or "").strip()
        if not label:
            return jsonify({"error": "label is required"}), 400
        wid = watchlist.add(c, label=label,
                            brand=(data.get("brand") or "").strip() or None,
                            gtin=(data.get("upc") or "").strip() or None)
        return jsonify({"id": wid}), 201
    return jsonify({"items": watchlist.all_items(c)})


@app.route("/api/watchlist/<int:wid>", methods=["DELETE"])
def api_watchlist_delete(wid):
    return jsonify({"removed": watchlist.remove(conn(), wid)})


@app.route("/api/scan")
def api_scan():
    """Every watchlist item with its current matches - powers the main grid."""
    c = conn()
    state = (request.args.get("state") or "").strip() or None
    include_closed = request.args.get("all") == "1"
    out = []
    for q in watchlist.as_queries(c):
        matches = matcher.check(c, q, include_closed=include_closed,
                                state=state, limit=5)
        out.append({
            "id": q.watch_id,
            "label": q.label,
            "brand": q.brand,
            "gtin": q.gtin,
            "matches": [_match_json(m) for m in matches],
            "worst": min((m.verdict for m in matches),
                         key={CERTAIN: 0, LIKELY: 1, POSSIBLE: 2}.get, default=None),
        })
    return jsonify({"items": out})


@app.route("/api/recent")
def api_recent():
    """Open recalls, newest first - the 'what is happening right now' feed."""
    c = conn()
    days = int(request.args.get("days") or 90)
    state = (request.args.get("state") or "").strip() or None
    only_class_i = request.args.get("class_i") == "1"
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()

    sql = ("SELECT * FROM recalls WHERE status='Ongoing' AND report_date >= ?")
    params = [since]
    if only_class_i:
        sql += " AND classification LIKE 'Class I'"
    sql += " ORDER BY report_date DESC LIMIT 400"

    rows = [dict(r) for r in c.execute(sql, params)]
    if state:
        rows = [r for r in rows if matcher._distributed_to(c, r["id"], state)]

    items = [_match_json(matcher.Match(recall=r, verdict=POSSIBLE, score=0)) for r in rows[:60]]
    for it in items:
        it.pop("verdict", None)
        it.pop("score", None)
        it.pop("reasons", None)
    return jsonify({"items": items, "days": days, "total": len(rows)})


@app.route("/api/volume")
def api_volume():
    """Recall counts per month for the last 12 months."""
    c = conn()
    today = _dt.date.today()
    # 12 months ending with the current one. Eleven steps back, not 365 days,
    # which would spill a 13th bar and put two same-named months on one axis.
    start = today.replace(day=1)
    for _ in range(11):
        start = (start - _dt.timedelta(days=1)).replace(day=1)
    rows = c.execute(
        """SELECT substr(report_date,1,7) AS month, COUNT(*) AS n
           FROM recalls WHERE report_date >= ? GROUP BY month ORDER BY month""",
        (start.isoformat(),),
    ).fetchall()
    counts = {r["month"]: r["n"] for r in rows}

    this_month = today.strftime("%Y-%m")
    months, cursor = [], start
    for _ in range(12):
        key = cursor.strftime("%Y-%m")
        months.append({"month": key,
                       "label": cursor.strftime("%b"),
                       "year": cursor.strftime("%Y"),
                       "count": counts.get(key, 0),
                       # The current month is still filling up. Flagged so the
                       # chart can draw it as incomplete instead of letting it
                       # read as a real drop in recalls.
                       "partial": key == this_month})
        cursor = (cursor.replace(day=28) + _dt.timedelta(days=8)).replace(day=1)
    return jsonify({"months": months, "as_of": today.isoformat()})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    report = sync.sync(conn())
    return jsonify({"report": report, "stats": db.stats(conn())})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="127.0.0.1", port=port, debug=False)
