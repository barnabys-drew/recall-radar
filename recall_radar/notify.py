"""Announce newly published recalls to a Discord webhook.

The app itself is a pull tool: you open it in the aisle and ask about the thing
in your hand. This is the push half - the part that tells you about a recall you
would never have thought to ask about, on a product already in your kitchen.

What counts as "new" is deliberately *not* the recall's report date. FDA
publishes in weekly batches and backfills older events, so a recall can arrive
today carrying a date from three weeks ago. The honest question is "did this
appear in the page since the last time we looked", which makes the previously
published page the ledger: parse the ids it carried, diff against the ids the
fresh build carries, and announce the difference. That needs no database, no
committed state file and no clock, and it stays correct when a build is skipped
or a day is missed.

Nothing here reads the household watchlist. The watchlist lives in the browser's
local storage, on the phone, which is the only place a public repository can
keep a list of what one family eats.
"""

import json
import time
import urllib.error
import urllib.request

# Discord's documented ceilings. Exceeding any of them fails the whole request,
# so everything below clips rather than trusting the source text to be short:
# a recall's product_description is routinely a thousand characters of pack
# sizes and lot codes.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS = 6000
MAX_CONTENT = 2000
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048

# What one card is actually allowed to use, which is far less than what Discord
# permits. Two reasons: a card near Discord's own limits would blow the 6000
# character per-message budget by itself, and nobody reads eight hundred words
# of lot codes on a phone. The link goes to the agency for the full text.
TITLE_BUDGET = 200
DESCRIPTION_BUDGET = 800
REASON_BUDGET = 600
SOLD_BUDGET = 200

# Matches the palette the page uses for the same badges, so a notice and the
# app agree at a glance about how bad a thing is.
COLORS = {"Class I": 0xF85149, "Class II": 0xF0883E, "Class III": 0x8B949E}
DEFAULT_COLOR = 0x58A6FF

# Class I is "reasonable probability of serious health consequences or death",
# which is the only tier worth waking someone up for. The rest sort under it.
CLASS_RANK = {"Class I": 0, "Class II": 1, "Class III": 2}

AGENCY = {"fda": "FDA", "fsis": "USDA FSIS"}

MARKER = "const DATA = "


# --------------------------------------------------------------------------
# reading a built page
# --------------------------------------------------------------------------
def payload_from_page(html):
    """Pull the baked corpus back out of a page build.py wrote.

    build.py emits the payload as one line of compact JSON, so this is a slice
    and a parse rather than anything that pretends to understand HTML.
    """
    start = html.find(MARKER)
    if start < 0:
        raise ValueError(f"no {MARKER!r} in this page - is it a recall-radar build?")
    start += len(MARKER)
    end = html.find("\n", start)
    if end < 0:
        end = len(html)
    line = html[start:end].strip().rstrip(";")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the baked payload is not valid JSON: {exc}") from None


def recalls(data):
    """The baked rows as dicts, each carrying its distribution states."""
    fields = data["fields"]
    states = data.get("s") or []
    out = []
    for i, row in enumerate(data["r"]):
        rec = dict(zip(fields, row))
        rec["states"] = states[i] if i < len(states) else []
        out.append(rec)
    return out


def ids(data):
    idx = data["fields"].index("id")
    return [row[idx] for row in data["r"]]


def new_recalls(before_ids, data):
    """Recalls in this build that the previous build did not carry.

    Worst first: Class I, then newest, then by id so two runs over the same
    data always produce the same notice. Built as three stable passes because
    a date is a string and there is no way to negate one inside a sort key.
    """
    seen = set(before_ids)
    fresh = [r for r in recalls(data) if r["id"] not in seen]
    fresh.sort(key=lambda r: r["id"])
    fresh.sort(key=lambda r: r.get("report_date") or "", reverse=True)
    fresh.sort(key=lambda r: CLASS_RANK.get(r.get("classification"), 3))
    return fresh


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def clip(text, limit):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def where(states):
    """A distribution line short enough to read on a phone."""
    if not states:
        return ""
    if "US" in states:
        return "Nationwide"
    rest = sorted(s for s in states if s != "US")
    if len(rest) > 8:
        return ", ".join(rest[:8]) + f" +{len(rest) - 8} more"
    return ", ".join(rest)


def embed(rec):
    """One recall as a Discord embed."""
    firm = clip(rec.get("recalling_firm"), TITLE_BUDGET) or "Unknown firm"
    body = clip(rec.get("product_description"), DESCRIPTION_BUDGET)

    fields = []
    reason = clip(rec.get("reason"), REASON_BUDGET)
    if reason:
        fields.append({"name": "Why", "value": reason, "inline": False})
    sold = clip(where(rec.get("states")), SOLD_BUDGET)
    if sold:
        fields.append({"name": "Sold in", "value": sold, "inline": True})
    cls = rec.get("classification")
    if cls:
        fields.append({"name": "Risk", "value": cls, "inline": True})

    source = AGENCY.get(rec.get("source"), (rec.get("source") or "").upper())
    footer = " · ".join(x for x in (source, rec.get("recall_number"),
                                         rec.get("report_date")) if x)

    out = {
        "title": firm,
        "color": COLORS.get(cls, DEFAULT_COLOR),
        "fields": fields,
        "footer": {"text": clip(footer, MAX_FOOTER)},
    }
    if body:
        out["description"] = body
    # A recall with no landing page still notifies; it just is not clickable.
    if rec.get("url"):
        out["url"] = rec["url"]
    return out


def _embed_chars(e):
    """Discord counts title, description, footer and every field together."""
    n = len(e.get("title", "")) + len(e.get("description", ""))
    n += len(e.get("footer", {}).get("text", ""))
    for f in e.get("fields", []):
        n += len(f["name"]) + len(f["value"])
    return n


def headline(fresh, shown, app_url):
    """The line above the cards.

    Every count is over the whole batch, not the part that fit. Cards are
    ordered worst first, so counting Class I only among the visible ones would
    under-report exactly when the batch is big enough to matter.
    """
    total, hidden = len(fresh), len(fresh) - shown
    class_i = sum(1 for r in fresh if r.get("classification") == "Class I")
    line = f"\U0001f4e2 **{total} new {'recall' if total == 1 else 'recalls'}**"
    if class_i:
        line += f" — {class_i} Class I (most serious)"
    if hidden:
        line += f"\nThe {shown} most serious are below; {hidden} more are not."
    if app_url:
        line += f"\nCheck something you have: {app_url}"
    return clip(line, MAX_CONTENT) if len(line) > MAX_CONTENT else line


def messages(fresh, app_url=None, limit=40):
    """Batch recalls into webhook payloads that respect every Discord ceiling.

    FDA publishes weekly, so a quiet run sends nothing and a Wednesday sends
    thirty at once. `limit` caps how many get their own embed: past that the
    notice says how many were left out and points at the app, which is more
    useful than forty more cards nobody scrolls through.
    """
    if not fresh:
        return []

    shown = fresh[:limit]

    batches, current, chars = [], [], 0
    for rec in shown:
        e = embed(rec)
        size = _embed_chars(e)
        if current and (len(current) >= MAX_EMBEDS_PER_MESSAGE
                        or chars + size > MAX_EMBED_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(e)
        chars += size
    if current:
        batches.append(current)

    payloads = []
    for i, batch in enumerate(batches):
        payload = {"embeds": batch, "allowed_mentions": {"parse": []}}
        if i == 0:
            payload["content"] = headline(fresh, len(shown), app_url)
        payloads.append(payload)
    return payloads


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------
class DiscordError(RuntimeError):
    pass


def _post_once(url, payload, timeout):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "recall-radar (+https://github.com/barnabys-drew/recall-radar)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def send(url, payloads, timeout=20, pause=1.0, attempts=4, sleep=time.sleep,
         post=_post_once, log=lambda m: None):
    """POST each payload, honouring rate limits and retrying transient failures.

    Webhook URLs are credentials: nothing here ever puts `url` in a message, an
    exception or a log line, because this runs in CI where output is public.
    """
    for i, payload in enumerate(payloads):
        if i:
            sleep(pause)
        for attempt in range(1, attempts + 1):
            try:
                status, _ = post(url, payload, timeout)
                if status < 300:
                    break
                raise DiscordError(f"Discord replied {status}")
            except urllib.error.HTTPError as exc:
                retry_after = _retry_after(exc)
                fatal = exc.code not in (429,) and exc.code < 500
                if fatal or attempt == attempts:
                    raise DiscordError(
                        f"Discord rejected the notice with HTTP {exc.code}"
                        f"{' (check the webhook secret)' if exc.code in (401, 403, 404) else ''}"
                    ) from None
                log(f"  Discord returned {exc.code}; retrying in {retry_after:.1f}s")
                sleep(retry_after)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == attempts:
                    raise DiscordError(f"could not reach Discord: {exc}") from None
                log(f"  network error talking to Discord; retrying")
                sleep(2.0 * attempt)
    return len(payloads)


def _retry_after(exc):
    """Seconds Discord asked us to wait, from the body or the header."""
    try:
        body = json.loads(exc.read().decode())
        if isinstance(body, dict) and "retry_after" in body:
            return max(0.0, min(60.0, float(body["retry_after"])))
    except Exception:
        pass
    try:
        return max(0.0, min(60.0, float(exc.headers.get("Retry-After", 0))))
    except (TypeError, ValueError):
        pass
    return 5.0
