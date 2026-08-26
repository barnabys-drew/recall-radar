"""Command line for recall-radar."""

import argparse
import json
import sys

from . import db, matcher, sync, watchlist
from .matcher import CERTAIN, LIKELY, POSSIBLE, Query

_C = {"certain": "\033[1;97;41m", "likely": "\033[1;31m", "possible": "\033[33m",
      "ok": "\033[32m", "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m"}


def _color(enabled):
    return _C if enabled and sys.stdout.isatty() else {k: "" for k in _C}


def _headline(verdict):
    return {CERTAIN: " RECALLED ", LIKELY: " LIKELY RECALLED ", POSSIBLE: " POSSIBLE MATCH "}[verdict]


def _print_match(m, c, verbose=False):
    r = m.recall
    print(f"{c[m.verdict]}{_headline(m.verdict)}{c['off']} "
          f"{c['bold']}{r.get('recalling_firm') or 'Unknown firm'}{c['off']}"
          f"  {c['dim']}({r.get('classification') or 'unclassified'}, "
          f"{r.get('source','').upper()} {r.get('recall_number')}, {r.get('report_date')}){c['off']}")
    desc = (r.get("product_description") or "").strip()
    print(f"    product : {desc[:160]}{'...' if len(desc) > 160 else ''}")
    print(f"    reason  : {(r.get('reason') or '-')[:160]}")
    if r.get("distribution_pattern"):
        print(f"    sold in : {r['distribution_pattern'][:110]}")
    for reason in m.reasons:
        print(f"    {c['dim']}why     : {reason} (score {m.score}){c['off']}")
    # FSIS has no separate code field, so the adapter maps product items into
    # it; printing both would just repeat the same long line back at the reader.
    codes = (r.get("code_info") or "").strip()
    if codes and codes != desc:
        print(f"    {c['dim']}codes   : {codes[:200]}{c['off']}")
    if r.get("url"):
        print(f"    {c['dim']}details : {r['url']}{c['off']}")
    print()


def cmd_sync(args, conn, c):
    print("Syncing recall sources...")
    report = sync.sync(conn, sources=args.source or None, since=args.since,
                       full=args.full, progress=lambda s: print(s, flush=True))
    for name, stat in report.items():
        if stat["error"]:
            print(f"  {name:5} FAILED: {stat['error']}")
        else:
            print(f"  {name:5} {stat['added']} new, {stat['updated']} updated")
    s = db.stats(conn)
    print(f"\n{s['recalls']} recalls stored ({s['ongoing']} ongoing, "
          f"{s['class_i']} Class I). Newest: {s['newest']}")
    return 0 if not any(v["error"] for v in report.values()) else 1


def cmd_check(args, conn, c):
    if args.upc:
        q = Query.from_barcode(args.upc, label=args.upc)
        if not q.gtin:
            print(f"'{args.upc}' is not a 12-14 digit barcode.", file=sys.stderr)
            return 2
    else:
        q = Query.from_text(" ".join(args.text))

    matches = matcher.check(conn, q, include_closed=args.all, state=args.state, limit=args.limit)
    if args.json:
        print(json.dumps([{"verdict": m.verdict, "score": m.score,
                           "reasons": m.reasons, "recall": m.recall} for m in matches],
                         indent=2, default=str))
        return 1 if matches else 0

    label = q.label
    if not matches:
        scope = "any recall" if args.all else "any open recall"
        print(f"{c['ok']}No match{c['off']} - '{label}' does not appear in {scope} on file.")
        print(f"{c['dim']}Checked {db.stats(conn)['recalls']} recalls, last synced "
              f"{db.get_meta(conn, 'last_sync', 'never')}.{c['off']}")
        return 0

    rank = {CERTAIN: 0, LIKELY: 1, POSSIBLE: 2}
    strongest = min((m.verdict for m in matches), key=rank.get)
    n = len(matches)
    noun = "match" if n == 1 else "matches"
    advice = {CERTAIN: "this barcode is on a recall",
              LIKELY: "the name matches a recall",
              POSSIBLE: "worth checking the label"}[strongest]
    print(f"\n{n} {noun} for {c['bold']}{label}{c['off']} - {advice}:\n")
    for m in matches:
        _print_match(m, c, args.verbose)
    return 1


def cmd_watch(args, conn, c):
    if args.watch_cmd == "add":
        wid = watchlist.add(conn, label=args.label, brand=args.brand,
                            product=args.product, gtin=args.upc, note=args.note)
        print(f"Added #{wid}: {args.label}")
    elif args.watch_cmd == "rm":
        print("Removed." if watchlist.remove(conn, args.id) else "No such entry.")
    else:
        items = watchlist.all_items(conn)
        if not items:
            print("Watchlist is empty. Add one:  recall-radar watch add 'Taylor Farms salad'")
            return 0
        for i in items:
            extra = f"  upc={i['gtin']}" if i["gtin"] else ""
            brand = f"  brand={i['brand']}" if i["brand"] else ""
            print(f"  #{i['id']:<4} {i['label']}{brand}{extra}")
    return 0


def cmd_scan(args, conn, c):
    """Run the whole watchlist - the 'is anything we buy recalled?' sweep."""
    queries = watchlist.as_queries(conn)
    if not queries:
        print("Watchlist is empty. Add items with:  recall-radar watch add 'Brand product'")
        return 0

    total, new_only = 0, args.new
    for q in queries:
        matches = matcher.check(conn, q, include_closed=args.all, state=args.state, limit=args.limit)
        shown = []
        for m in matches:
            is_new = watchlist.record_alert(conn, q.watch_id, m)
            if new_only and not is_new:
                continue
            shown.append(m)
        if shown:
            print(f"\n{c['bold']}=== {q.label} ==={c['off']}\n")
            for m in shown:
                _print_match(m, c, args.verbose)
            total += len(shown)

    if total == 0:
        scope = "new matches" if new_only else "matches"
        print(f"{c['ok']}All clear{c['off']} - no {scope} across "
              f"{len(queries)} watchlist item(s).")
        return 0
    print(f"{total} match(es) across {len(queries)} watchlist item(s).")
    return 1


def cmd_stats(args, conn, c):
    for k, v in db.stats(conn).items():
        print(f"  {k:<10} {v}")
    by_source = conn.execute(
        "SELECT source, COUNT(*) n, MAX(report_date) newest FROM recalls GROUP BY source")
    for r in by_source:
        print(f"  {r['source']:<10} {r['n']} recalls, newest {r['newest']}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="recall-radar",
        description="Check food against live FDA and USDA recall data.")
    p.add_argument("--db", default=db.DEFAULT_DB, help="database path")
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="pull the latest recalls")
    s.add_argument("--source", action="append", choices=list(sync.SOURCES))
    s.add_argument("--since", help="ISO date lower bound")
    s.add_argument("--full", action="store_true", help="re-pull all history")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("check", help="check one item (use this in the store)")
    s.add_argument("text", nargs="*", help="brand and product, e.g. 'Taylor Farms salad'")
    s.add_argument("--upc", help="scanned barcode instead of text")
    s.add_argument("--state", help="only recalls distributed to this state, e.g. IL")
    s.add_argument("--all", action="store_true", help="include closed/terminated recalls")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("watch", help="manage the household watchlist")
    ws = s.add_subparsers(dest="watch_cmd", required=True)
    a = ws.add_parser("add")
    a.add_argument("label")
    a.add_argument("--brand")
    a.add_argument("--product")
    a.add_argument("--upc")
    a.add_argument("--note")
    ws.add_parser("list")
    rm = ws.add_parser("rm")
    rm.add_argument("id", type=int)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("scan", help="check the whole watchlist")
    s.add_argument("--new", action="store_true", help="only matches not seen before")
    s.add_argument("--state")
    s.add_argument("--all", action="store_true")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("stats", help="what is in the local database")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        return args.func(args, conn, _color(not args.no_color))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
