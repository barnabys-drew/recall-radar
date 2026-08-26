"""USDA FSIS recalls - meat, poultry, and processed egg products.

Two quirks drive the shape of this module:

1. The endpoint sits behind Akamai bot protection that rejects a bare
   User-Agent with 403. A complete, browser-shaped header set is required -
   see BROWSER_HEADERS. This is a documented fragility, not a trick: if USDA
   tightens the rules the adapter degrades to SourceError and sync continues
   with FDA data only.
2. Every list-valued field arrives as a *stringified Python list*
   ("['Nationwide']"), so values need unwrapping before use.
"""

import ast
import gzip
import html
import io
import json
import re
import urllib.error
import urllib.request

from .base import SourceError, blank_record

ENDPOINT = "https://www.fsis.usda.gov/fsis/api/recall/v/1"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "sec-ch-ua": '"Chromium";v="124", "Not:A-Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_TAGS = re.compile(r"<[^>]+>")


def _unwrap(value):
    """"['a', 'b']" -> "a; b".  Plain strings pass through untouched."""
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, (list, tuple)):
            parts = [re.sub(r"\s+", " ", str(p)).strip() for p in parsed]
            return "; ".join(p for p in parts if p) or None
    return text or None


def _strip_html(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", str(value)))).strip() or None


def _firm_from_title(title):
    """FSIS titles read 'Acme Foods, Inc. Recalls Chicken Products Due To...'."""
    if not title:
        return None
    # Public health alerts are issued by FSIS itself and name no recalling firm;
    # their titles have no "Recalls" verb to split on.
    if not re.search(r"\bRecalls?\b", str(title)):
        return None
    return re.split(r"\bRecalls?\b", str(title), maxsplit=1)[0].strip(" ,.-") or None


def _fetch_raw(timeout=90):
    req = urllib.request.Request(ENDPOINT, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                payload = gzip.GzipFile(fileobj=io.BytesIO(payload)).read()
            return json.loads(payload.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        hint = " (bot protection - header set may need refreshing)" if e.code == 403 else ""
        raise SourceError(f"FSIS HTTP {e.code}{hint}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise SourceError(f"FSIS unreachable: {e}") from e


def _to_record(r):
    number = (r.get("field_recall_number") or "").strip()
    if not number:
        return None
    rec = blank_record("fsis")
    products = _unwrap(r.get("field_product_items"))
    active = str(r.get("field_active_notice", "")).lower() == "true"
    archived = str(r.get("field_archive_recall", "")).lower() == "true"
    rec.update(
        id=f"fsis:{number}",
        recall_number=number,
        event_id=number,
        # Mapped onto the FDA vocabulary so downstream filters stay source-agnostic.
        status="Ongoing" if active and not archived else "Terminated",
        classification=(r.get("field_recall_classification") or "").strip() or None,
        recalling_firm=_firm_from_title(r.get("field_title")),
        product_description=products or _strip_html(r.get("field_title")),
        reason=_unwrap(r.get("field_recall_reason")),
        distribution_pattern=_unwrap(r.get("field_states")),
        # FSIS has no code_info field; pack dates and establishment numbers live
        # inside the product item text, which is what the extractors want anyway.
        code_info=products,
        report_date=(r.get("field_recall_date") or "").strip() or None,
        initiation_date=(r.get("field_recall_date") or "").strip() or None,
        termination_date=(r.get("field_closed_year") or "").strip() or None,
        url=(r.get("field_recall_url") or "").strip() or None,
        raw={k: v for k, v in r.items() if k != "field_summary"},
    )
    return rec


def fetch(since=None, until=None, max_records=None):
    """Yield normalized FSIS records. The endpoint has no date filter, so the
    full list is fetched and filtered locally - it is ~13MB and ~2k rows."""
    count = 0
    for r in _fetch_raw():
        rec = _to_record(r)
        if not rec:
            continue
        d = rec["report_date"] or ""
        if since and d and d < since:
            continue
        if until and d and d > until:
            continue
        yield rec
        count += 1
        if max_records and count >= max_records:
            return
