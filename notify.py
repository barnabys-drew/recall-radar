#!/usr/bin/env python3
"""Tell a Discord channel about recalls that just showed up in the app.

Two commands, meant to bracket a build:

    python3 notify.py snapshot --out before.json   # before build.py runs
    python3 notify.py post --before before.json    # after it has been published

The snapshot records which recalls the currently published page carries. The
post step reads the freshly built page, announces the difference, and says
nothing at all when there is no difference - which is most days, because FDA
publishes in weekly batches.

Bracketing the build this way, rather than diffing report dates or keeping a
database, means a missed day is not a missed notice: whatever appeared while
nobody was looking is still new relative to the last page that shipped.

The webhook URL is a credential and is read from the environment, never from
an argument, so it stays out of shell history and out of a CI process list:

    export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'

Add --dry-run to see exactly what would be sent without sending it.
"""

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from recall_radar import notify  # noqa: E402

HERE = pathlib.Path(__file__).parent
DEFAULT_PAGE = HERE / "docs" / "index.html"
DEFAULT_APP_URL = "https://barnabys-drew.github.io/recall-radar/"
WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def load_page(path):
    path = pathlib.Path(path)
    if not path.exists():
        sys.exit(f"No page at {path}. Run build.py first.")
    return notify.payload_from_page(path.read_text())


def cmd_snapshot(args):
    # A repository with no published page yet has nothing to compare against.
    # Writing no snapshot is what tells `post` to stay quiet, which is right:
    # the first build is not news, it is the baseline.
    if not pathlib.Path(args.page).exists():
        print(f"No page at {args.page} yet - the next build is the baseline, not news.")
        return 0
    data = load_page(args.page)
    recall_ids = notify.ids(data)
    pathlib.Path(args.out).write_text(json.dumps(
        {"built": data.get("built"), "ids": recall_ids}, separators=(",", ":")))
    print(f"Noted {len(recall_ids)} recalls already published (built {data.get('built')}).")
    return 0


def cmd_post(args):
    before = pathlib.Path(args.before)
    if not before.exists():
        # First run, or a snapshot that never happened. Announcing every open
        # recall in the corpus would be nine hundred messages of noise, so the
        # only safe reading of "no ledger" is "nothing is new".
        print(f"No snapshot at {before} - nothing to compare against, so nothing to announce.")
        return 0

    try:
        previous = json.loads(before.read_text())["ids"]
    except (json.JSONDecodeError, KeyError, TypeError):
        sys.exit(f"{before} is not a snapshot written by `notify.py snapshot`.")
    if not previous:
        print("The snapshot is empty, which cannot be right. Announcing nothing.")
        return 0

    data = load_page(args.page)
    fresh = notify.new_recalls(previous, data)
    if args.class_i_only:
        fresh = [r for r in fresh if r.get("classification") == "Class I"]

    if not fresh:
        print("No new recalls since the last build.")
        return 0

    class_i = sum(1 for r in fresh if r.get("classification") == "Class I")
    print(f"{len(fresh)} new recall(s), {class_i} of them Class I.")

    payloads = notify.messages(fresh, app_url=args.app_url, limit=args.limit)

    if args.dry_run:
        print(json.dumps(payloads, indent=2)[:20000])
        print(f"\n(dry run: {len(payloads)} message(s) not sent)")
        return 0

    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        print(f"{WEBHOOK_ENV} is not set, so nothing was sent. "
              f"Use --dry-run to see the notice.", file=sys.stderr)
        return 0

    sent = notify.send(url, payloads, log=print)
    print(f"Sent {sent} message(s) to Discord.")

    step_output = os.environ.get("GITHUB_OUTPUT")
    if step_output:
        with open(step_output, "a") as fh:
            fh.write(f"new={len(fresh)}\nclass_i={class_i}\n")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="record which recalls are published now")
    s.add_argument("--page", default=str(DEFAULT_PAGE))
    s.add_argument("--out", required=True, help="where to write the id list")
    s.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("post", help="announce what the newest build added")
    p.add_argument("--before", required=True, help="a file written by `snapshot`")
    p.add_argument("--page", default=str(DEFAULT_PAGE))
    p.add_argument("--app-url", default=DEFAULT_APP_URL,
                   help="linked in the notice; pass '' to leave it out")
    p.add_argument("--limit", type=int, default=40,
                   help="how many recalls get their own card (default: 40)")
    p.add_argument("--class-i-only", action="store_true",
                   help="announce only the most serious tier")
    p.add_argument("--dry-run", action="store_true",
                   help="print the payloads instead of sending them")
    p.set_defaults(fn=cmd_post)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except notify.DiscordError as exc:
        sys.exit(f"ERROR: {exc}")
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")


if __name__ == "__main__":
    sys.exit(main())
