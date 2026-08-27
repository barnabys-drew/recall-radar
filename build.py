#!/usr/bin/env python3
"""Build the aisle app: one self-contained page carrying the whole matcher.

The dashboard in `app.py` asks a local Flask server, which asks SQLite. Neither
exists on a phone in a grocery aisle, and neither the artifact sandbox nor a
GitHub Pages origin may call FDA or USDA directly. So this bakes the corpus and
the index into the page itself and ships a JavaScript port of `matcher.check`
alongside it.

Two outputs from one `template.html`:

  recall-radar.html   a body fragment for a Claude Artifact (the host supplies
                      its own <html>/<head>)
  docs/index.html     a standalone, installable page for GitHub Pages, plus a
                      content-stamped service worker, manifest and icons

Only *open* recalls are baked. Terminated ones are five sevenths of the corpus
and answer a different question (what is already in the pantry) than the one
this page exists for (what is about to go in the cart). Their tokens still
count toward document frequency, so scoring is identical to the CLI's.

    python3 build.py                 sync both agencies, then build
    python3 build.py --no-sync       build from whatever is already in the DB
    python3 build.py --allow-partial build even if an agency did not answer
"""

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import pathlib
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from recall_radar import db, sync as sync_mod  # noqa: E402

HERE = pathlib.Path(__file__).parent
DOCS = HERE / "docs"
ICONS = DOCS / "icons"
TEMPLATE = HERE / "template.html"
MATCHER_JS = HERE / "web" / "matcher.js"
SW_TEMPLATE = HERE / "sw-template.js"
ARTIFACT_OUT = HERE / "recall-radar.html"
PAGE_OUT = DOCS / "index.html"
SW_OUT = DOCS / "sw.js"

# A build that quietly ships half the food supply is worse than no build: FDA
# does not cover the chicken and USDA does not cover the spinach, so a page
# missing one agency answers "no recall" to questions it never looked at.
#
# The test is whether the *fetch* worked, not how many recalls happen to be
# open. USDA closes recalls fast and normally has only one or two open at a
# time, so an open-count floor would fail every build. Total records caught a
# 403 or a truncated response; the newest report date catches a source that
# answered but is frozen. Both are set well outside normal variation - USDA
# publishes on the order of twenty recalls a year, so quiet months are routine.
MIN_RECORDS = {"fda": 1000, "fsis": 100}
MAX_AGE_DAYS = {"fda": 45, "fsis": 180}

# Fields carried into the page, in this order. `raw` is deliberately dropped:
# it is two thirds of the payload and nothing on the page reads it.
FIELDS = ["id", "source", "recall_number", "classification", "recalling_firm",
          "product_description", "reason", "distribution_pattern", "code_info",
          "report_date", "url"]

FIELD_FIRM, FIELD_PRODUCT = 1, 2


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def bake(conn):
    """Everything the page needs to answer a query, and nothing else."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM recalls WHERE status='Ongoing' ORDER BY report_date DESC")]
    index_of = {r["id"]: i for i, r in enumerate(rows)}

    # Document frequency over the *whole* corpus, terminated recalls included.
    # The matcher weights a token by how rare it is; measuring that against only
    # the open subset would make common words look distinctive and turn "farms"
    # into evidence.
    corpus = conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] or 1
    df = {r["token"]: r["df"] for r in conn.execute(
        "SELECT token, COUNT(DISTINCT recall_id) AS df FROM recall_tokens GROUP BY token")}

    # Postings, packed as (recall index << 2) | field bitmask. One integer per
    # (token, recall) pair instead of an object per row.
    postings = {}
    for row in conn.execute(
            "SELECT t.token, t.recall_id, t.field FROM recall_tokens t "
            "JOIN recalls r ON r.id = t.recall_id WHERE r.status='Ongoing'"):
        idx = index_of.get(row["recall_id"])
        if idx is None:
            continue
        bit = FIELD_FIRM if row["field"] == "firm" else FIELD_PRODUCT
        bucket = postings.setdefault(row["token"], {})
        bucket[idx] = bucket.get(idx, 0) | bit
    postings = {t: [(i << 2) | m for i, m in sorted(b.items())]
                for t, b in postings.items()}

    # Barcodes, packed as (recall index << 1) | 1 when the check digit validated.
    upcs = {}
    for row in conn.execute(
            "SELECT u.gtin, u.confidence, u.recall_id FROM recall_upcs u "
            "JOIN recalls r ON r.id = u.recall_id WHERE r.status='Ongoing'"):
        idx = index_of.get(row["recall_id"])
        if idx is not None:
            upcs.setdefault(row["gtin"], []).append(
                (idx << 1) | (1 if row["confidence"] == "high" else 0))
    # `rows` is already newest-first, so ascending index reproduces the CLI's
    # "ORDER BY report_date DESC" without carrying the dates into the page.
    upcs = {g: sorted(v) for g, v in upcs.items()}

    states = [[] for _ in rows]
    for row in conn.execute(
            "SELECT s.state, s.recall_id FROM recall_states s "
            "JOIN recalls r ON r.id = s.recall_id WHERE r.status='Ongoing'"):
        idx = index_of.get(row["recall_id"])
        if idx is not None:
            states[idx].append(row["state"])

    # Over the whole corpus, not just the open subset: USDA usually has one open
    # recall, so an open-only date would read as months stale on a healthy build.
    as_of = {r["source"]: r["newest"] for r in conn.execute(
        "SELECT source, MAX(report_date) AS newest FROM recalls GROUP BY source")}
    totals = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM recalls GROUP BY source")}
    open_by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM recalls WHERE status='Ongoing' GROUP BY source")}

    class_i = sum(1 for r in rows if (r["classification"] or "") == "Class I")

    return {
        "built": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of,
        "totals": totals,
        "corpus": corpus,
        "counts": {
            "open": len(rows),
            "class_i": class_i,
            "with_upc": len({p >> 1 for v in upcs.values() for p in v}),
            "by_source": open_by_source,
        },
        "fields": FIELDS,
        "r": [[r[f] or "" for f in FIELDS] for r in rows],
        "df": df,
        "px": postings,
        "u": upcs,
        "s": states,
    }


def check_coverage(data, allow_partial):
    """Refuse to ship a page that silently lost an agency."""
    today = _dt.date.today()
    problems = []
    for source, floor in MIN_RECORDS.items():
        total = data["totals"].get(source, 0)
        if total < floor:
            problems.append(f"{source.upper()}: {total} records in the corpus "
                            f"(expected at least {floor}) - the fetch probably failed")
            continue
        newest = data["as_of"].get(source)
        if not newest:
            problems.append(f"{source.upper()}: no dated records at all")
            continue
        age = (today - _dt.date.fromisoformat(newest)).days
        if age > MAX_AGE_DAYS[source]:
            problems.append(f"{source.upper()}: newest recall is {newest}, {age} days old "
                            f"(expected within {MAX_AGE_DAYS[source]})")
    if not problems:
        return
    for p in problems:
        print(f"  !! {p}", file=sys.stderr)
    if not allow_partial:
        sys.exit(
            "\nRefusing to build. A page missing an agency answers 'no recall' to\n"
            "questions it never looked at. Fix the sync, or pass --allow-partial\n"
            "if you genuinely want a partial page.")
    print("  (building anyway: --allow-partial)", file=sys.stderr)


# --------------------------------------------------------------------------
# icons - a pure-stdlib PNG writer, so the build has no image dependency
# --------------------------------------------------------------------------
BG = (0x0f, 0x11, 0x17)
ACCENT = (0x58, 0xa6, 0xff)
BLIP = (0xf8, 0x51, 0x49)


def _png_bytes(size, pixels):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels)

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _icon(size):
    """Concentric rings with one red contact on them - a radar with a hit.

    Drawn from signed distance fields so a single sample per pixel still
    antialiases, which keeps this to a few lines of arithmetic.
    """
    cx = cy = (size - 1) / 2
    rings = [(0.215, 1.0), (0.325, 0.62), (0.435, 0.34)]
    half = 0.020 * size
    blip_r = 0.075 * size
    ang = math.radians(-38)
    bx, by = cx + math.cos(ang) * 0.325 * size, cy + math.sin(ang) * 0.325 * size

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            r, g, b = BG
            d = math.hypot(x - cx, y - cy)
            for frac, alpha in rings:
                cover = max(0.0, min(1.0, 0.5 - (abs(d - frac * size) - half))) * alpha
                if cover:
                    r = r + (ACCENT[0] - r) * cover
                    g = g + (ACCENT[1] - g) * cover
                    b = b + (ACCENT[2] - b) * cover
            centre = max(0.0, min(1.0, 0.5 - (d - 0.052 * size)))
            if centre:
                r = r + (ACCENT[0] - r) * centre
                g = g + (ACCENT[1] - g) * centre
                b = b + (ACCENT[2] - b) * centre
            hit = max(0.0, min(1.0, 0.5 - (math.hypot(x - bx, y - by) - blip_r)))
            if hit:
                r = r + (BLIP[0] - r) * hit
                g = g + (BLIP[1] - g) * hit
                b = b + (BLIP[2] - b) * hit
            row.append((int(r), int(g), int(b)))
        rows.append(row)
    return _png_bytes(size, rows)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
MANIFEST = {
    "name": "Recall Radar",
    "short_name": "Recalls",
    "description": "Check food against open FDA and USDA recalls before it goes in the cart.",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#0f1117",
    "theme_color": "#0f1117",
    "orientation": "portrait",
    "icons": [
        {"src": "./icons/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "./icons/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-sync", action="store_true",
                    help="build from the existing database instead of fetching")
    ap.add_argument("--allow-partial", action="store_true",
                    help="build even when an agency returned too little data")
    ap.add_argument("--db", default=db.DEFAULT_DB, help="database path")
    args = ap.parse_args()

    conn = db.connect(args.db)

    if not args.no_sync:
        print("Syncing...")
        report = sync_mod.sync(conn, progress=lambda m: print(m, flush=True))
        for name, stat in report.items():
            if stat["error"]:
                print(f"  {name}: FAILED - {stat['error']}", file=sys.stderr)
            else:
                print(f"  {name}: +{stat['added']} new, {stat['updated']} refreshed")

    data = bake(conn)
    check_coverage(data, args.allow_partial)

    payload = json.dumps(data, separators=(",", ":"))
    template = TEMPLATE.read_text()
    for marker in ("__RECALL_DATA__", "__MATCHER_JS__"):
        if marker not in template:
            sys.exit(f"ERROR: template.html is missing the {marker} placeholder")
    # The matcher is inlined rather than linked, so the page stays one file in
    # both outputs and the artifact sandbox has nothing external to fetch.
    body = (template
            .replace("__MATCHER_JS__", MATCHER_JS.read_text())
            .replace("__RECALL_DATA__", payload))

    # 1. artifact fragment
    ARTIFACT_OUT.write_text(body)

    # 2. standalone installable page
    DOCS.mkdir(exist_ok=True)
    ICONS.mkdir(exist_ok=True)

    # Registered only here; the artifact sandbox has no service workers.
    sw_register = """<script>
if ("serviceWorker" in navigator) {
  addEventListener("load", function () {
    navigator.serviceWorker.register("./sw.js").catch(function () {});
  });
}
</script>"""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Check a product against every open FDA and USDA food recall, in the aisle, with no signal.">
<meta name="theme-color" content="#0f1117">
<link rel="manifest" href="./manifest.webmanifest">
<link rel="icon" href="./icons/icon-192.png">
<link rel="apple-touch-icon" href="./icons/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Recalls">
<style>
  /* The artifact host supplies a reset; standalone has to bring its own. */
  *, *::before, *::after {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{ margin: 0; overflow-x: hidden; }}
  /* Widen the gutter to clear the notch once installed. Everything that needs
     to sit flush derives its offset from --pad, so it tracks this. */
  :root {{ --pad: max(16px, env(safe-area-inset-left), env(safe-area-inset-right)); }}
  footer.legal {{ padding-bottom: max(24px, env(safe-area-inset-bottom)); }}
</style>
</head>
<body>
{body}
{sw_register}
</body>
</html>
"""
    PAGE_OUT.write_text(page)

    # 3. service worker, stamped with a hash of the page so a rebuild
    #    invalidates yesterday's recalls sitting in someone's phone cache
    version = hashlib.sha256(page.encode()).hexdigest()[:12]
    SW_OUT.write_text(SW_TEMPLATE.read_text().replace("__CACHE_VERSION__", version))

    (DOCS / "manifest.webmanifest").write_text(json.dumps(MANIFEST, indent=2))
    for px in (180, 192, 512):
        (ICONS / f"icon-{px}.png").write_bytes(_icon(px))
    (DOCS / ".nojekyll").write_text("")

    kb = lambda p: p.stat().st_size / 1024
    c = data["counts"]

    # The daily workflow puts this in the commit message, so a `git log` reads
    # as a record of what the data actually looked like each day.
    summary = (f"{c['open']} open recalls "
               f"({', '.join(f'{k.upper()} {v}' for k, v in sorted(c['by_source'].items()))}), "
               f"newest {max(data['as_of'].values())}")
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        with open(out_file, "a") as fh:
            fh.write(f"summary={summary}\n")

    print(f"\nopen recalls   : {c['open']} "
          f"({', '.join(f'{k.upper()} {v}' for k, v in sorted(c['by_source'].items()))})")
    print(f"corpus for idf : {data['corpus']} recalls, {len(data['df'])} tokens")
    print(f"newest         : {', '.join(f'{k.upper()} {v}' for k, v in sorted(data['as_of'].items()))}")
    print(f"payload        : {len(payload) / 1024:.0f} KB")
    print(f"artifact       : {ARTIFACT_OUT.name} ({kb(ARTIFACT_OUT):.0f} KB)")
    print(f"page           : docs/index.html ({kb(PAGE_OUT):.0f} KB)")
    print(f"service worker : docs/sw.js (cache recall-radar-{version})")


if __name__ == "__main__":
    main()
