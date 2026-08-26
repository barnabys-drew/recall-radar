"""openFDA food enforcement reports - FDA-regulated food.

Public, no API key required (a key only raises rate limits). Covers produce,
packaged food, dairy, seafood, supplements. Does NOT cover meat, poultry or
processed egg products - that is USDA FSIS territory (see fsis.py).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import SourceError, blank_record, iso_date

ENDPOINT = "https://api.fda.gov/food/enforcement.json"
PAGE = 1000        # openFDA hard maximum per request
SKIP_LIMIT = 25000  # openFDA refuses skip beyond this without a key


def _get(params, timeout=45):
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "recall-radar/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"results": []}  # openFDA's way of saying "no matches"
        raise SourceError(f"openFDA HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise SourceError(f"openFDA unreachable: {e}") from e


def _to_record(r):
    rec = blank_record("fda")
    num = r.get("recall_number") or r.get("event_id") or ""
    rec.update(
        id=f"fda:{num}",
        recall_number=num,
        event_id=r.get("event_id"),
        status=r.get("status"),
        classification=r.get("classification"),
        recalling_firm=r.get("recalling_firm"),
        product_description=r.get("product_description"),
        reason=r.get("reason_for_recall"),
        distribution_pattern=r.get("distribution_pattern"),
        code_info=r.get("code_info"),
        report_date=iso_date(r.get("report_date")),
        initiation_date=iso_date(r.get("recall_initiation_date")),
        termination_date=iso_date(r.get("termination_date")),
        url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
        raw=r,
    )
    return rec


def fetch(since=None, until=None, max_records=None):
    """Yield normalized records with report_date in [since, until].

    `since`/`until` are ISO dates. Records are re-yielded on every sync that
    covers their date, which is intentional: the FDA mutates status and
    termination_date in place after publication, and the upsert path relies on
    seeing those updates.
    """
    since = (since or "2020-01-01").replace("-", "")
    until = (until or "2099-12-31").replace("-", "")
    yielded, skip = 0, 0
    while True:
        data = _get({
            # urlencode turns the space into "+", which is exactly the separator
            # openFDA wants; writing "+TO+" here double-encodes it to %2B.
            "search": f"report_date:[{since} TO {until}]",
            "limit": PAGE,
            "skip": skip,
            "sort": "report_date:asc",
        })
        results = data.get("results") or []
        if not results:
            return
        for r in results:
            if not (r.get("recall_number") or r.get("event_id")):
                continue  # unidentifiable; nothing stable to key on
            yield _to_record(r)
            yielded += 1
            if max_records and yielded >= max_records:
                return
        skip += len(results)
        if len(results) < PAGE or skip >= SKIP_LIMIT:
            return
