"""Score a thing-you-might-buy against the recall corpus.

Everything the user can point at a shelf - a scanned barcode, a photo of a
package, a typed watchlist entry - collapses into one Query. Front-ends are
adapters; this module is the whole decision.

Two rules shape the scoring:

* A barcode is proof. A name is a guess. They must never be reported the same
  way, so a UPC hit and a text hit produce different verdicts.
* A match on a common word is not evidence. Tokens are weighted by inverse
  document frequency over the corpus, so "farms" or "chocolate" contribute
  almost nothing while "bazzini" or "taylor" carries the match.
"""

import math
from dataclasses import dataclass, field

from . import normalize

# Verdicts, strongest first.
CERTAIN = "certain"    # validated barcode match - this is the recalled item
LIKELY = "likely"      # strong distinctive name overlap
POSSIBLE = "possible"  # worth a look at the label

# LIKELY means "nearly everything you typed matched". Anything less is
# POSSIBLE, because IDF ranks tokens by corpus rarity, not by which word
# names the food: in this corpus "baby" (27 recalls) is rarer than
# "spinach" (45), so a partial match cannot be trusted to be the right one.
_MIN_LIKELY = 0.85
# Share of the query's total token weight that must actually match. Set
# from real queries: 'Cheerios cereal' matching only 'cereal' scores 0.40
# and is noise; 'Taylor Farms salad' matching 'taylor farms' scores 0.76.
_MIN_POSSIBLE = 0.50
# A token is "distinctive" - able to carry a match on its own - when it appears
# in at most 0.5% of recalls. This is expressed against corpus size rather than
# as a fixed IDF cutoff so that a small database (a fresh install that has only
# synced a month) behaves the same as a full one, instead of silently
# degrading every result to POSSIBLE because no token can clear the bar.
_DISTINCTIVE_DF_FRACTION = 0.005
_DISTINCTIVE_MIN_DF = 3
# Coverage a query of only-generic words must reach before it is shown.
_GENERIC_FLOOR = 0.85


@dataclass
class Query:
    """One item to check. Populate whatever the front-end managed to capture."""
    label: str = ""
    gtin: str = None
    brand: str = None
    product: str = None
    watch_id: int = None

    @classmethod
    def from_text(cls, text):
        """Free text from a typed search or OCR'd package photo."""
        return cls(label=text, product=text)

    @classmethod
    def from_barcode(cls, code, label=None):
        gtin = normalize.to_gtin14(code)
        return cls(label=label or code, gtin=gtin)


@dataclass
class Match:
    recall: dict
    verdict: str
    score: float
    reasons: list = field(default_factory=list)

    @property
    def is_class_i(self):
        return (self.recall.get("classification") or "").startswith("Class I") \
            and not (self.recall.get("classification") or "").startswith("Class II")


def _corpus_size(conn):
    return conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] or 1


def _idf(conn, tokens):
    """Return (idf, document-frequency) per token, over the whole corpus."""
    if not tokens:
        return {}, {}
    n = _corpus_size(conn)
    marks = ",".join("?" * len(tokens))
    rows = conn.execute(
        f"SELECT token, COUNT(DISTINCT recall_id) AS df FROM recall_tokens "
        f"WHERE token IN ({marks}) GROUP BY token", list(tokens)
    ).fetchall()
    df = {r["token"]: r["df"] for r in rows}
    # An unseen token gets maximum weight: it is maximally distinctive, and it
    # simply will not match anything, so it can only lower a score honestly.
    idf = {t: math.log(n / (df.get(t, 0) + 1)) for t in tokens}
    return idf, {t: df.get(t, 0) for t in tokens}


def _status_clause(include_closed):
    return "" if include_closed else " AND r.status = 'Ongoing'"


def _by_barcode(conn, gtin, include_closed):
    rows = conn.execute(
        f"""SELECT r.*, u.confidence AS upc_confidence
            FROM recall_upcs u JOIN recalls r ON r.id = u.recall_id
            WHERE u.gtin = ?{_status_clause(include_closed)}
            ORDER BY r.report_date DESC, r.id""",
        (gtin,),
    ).fetchall()
    out = []
    for row in rows:
        rec = dict(row)
        strong = rec.pop("upc_confidence", "low") == "high"
        out.append(Match(
            recall=rec,
            verdict=CERTAIN if strong else LIKELY,
            score=1.0 if strong else 0.8,
            reasons=[f"barcode {gtin} appears in this recall's code information"
                     + ("" if strong else " (unverified check digit)")],
        ))
    return out


def _by_text(conn, query, include_closed, limit):
    brand_tokens = set(normalize.tokenize(query.brand))
    product_tokens = set(normalize.tokenize(query.product))
    tokens = brand_tokens | product_tokens
    if not tokens:
        return []

    idf, df = _idf(conn, tokens)
    total_weight = sum(idf.values()) or 1.0

    # The most distinctive token that exists in the corpus carries the query's
    # identity - the brand, or the food itself. If it did not match, the overlap
    # is probably coincidence ("organic baby spinach" hitting "Organic BABY
    # bedtime drops"), so the result is capped at POSSIBLE rather than dropped:
    # still worth a look at the label, never announced as a recall.
    # Tokens absent from the corpus are ignored here - they can never match, so
    # letting one become the key token would cap every result (a misspelled
    # brand would silently downgrade an otherwise exact hit).
    present = [t for t in tokens if df.get(t, 0) > 0]
    key_token = max(present, key=lambda t: idf[t]) if present else None
    distinctive_cutoff = max(_DISTINCTIVE_MIN_DF,
                             _DISTINCTIVE_DF_FRACTION * _corpus_size(conn))

    marks = ",".join("?" * len(tokens))
    rows = conn.execute(
        f"""SELECT t.recall_id, t.token, t.field
            FROM recall_tokens t JOIN recalls r ON r.id = t.recall_id
            WHERE t.token IN ({marks}){_status_clause(include_closed)}""",
        list(tokens),
    ).fetchall()

    hits = {}
    for row in rows:
        hits.setdefault(row["recall_id"], {}).setdefault(row["token"], set()).add(row["field"])

    scored = []
    for rid, matched in hits.items():
        weight = 0.0
        for tok, fields in matched.items():
            w = idf[tok]
            # A brand word landing on the recalling firm is the strongest text
            # signal available; the same word buried in a description is weaker.
            if tok in brand_tokens and "firm" in fields:
                w *= 1.5
            elif tok in brand_tokens and "product" in fields:
                w *= 1.1
            weight += w
        score = min(weight / total_weight, 1.0)
        rarest_df = min((df[t] for t in matched), default=10 ** 9)

        # Distinctiveness gates the verdict, not just the cutoff. A one-word
        # query like "chicken" covers itself perfectly and so scores 1.0; without
        # this, perfect coverage of a generic word would read as LIKELY RECALLED.
        if rarest_df <= distinctive_cutoff:
            if score < _MIN_POSSIBLE:
                continue
            verdict = LIKELY if score >= _MIN_LIKELY else POSSIBLE
            if key_token and key_token not in matched:
                verdict = POSSIBLE
        else:
            # Nothing distinctive matched. Only near-total overlap is worth
            # showing at all, and it can never rise above "check the label".
            if score < _GENERIC_FLOOR:
                continue
            verdict = POSSIBLE
        # Ties broken by the token itself: this string is what a shopper reads,
        # and it must not depend on SQLite row order.
        top = sorted(matched, key=lambda t: (-idf[t], t))[:4]
        if key_token and key_token not in matched:
            top = top + [f"but not '{key_token}'"]
        scored.append((score, rid, verdict, top))

    # Recall id is the tiebreak, not decoration: `limit` truncates this list, so
    # without it two equally-scored matches would be kept or dropped according to
    # whatever order SQLite happened to return, and the browser port of this
    # scorer could not reproduce the same ten results.
    scored.sort(key=lambda s: (-s[0], s[1]))
    scored = scored[:limit]
    if not scored:
        return []

    ids = [s[1] for s in scored]
    marks = ",".join("?" * len(ids))
    recalls = {
        r["id"]: dict(r)
        for r in conn.execute(f"SELECT * FROM recalls WHERE id IN ({marks})", ids)
    }
    return [
        Match(recall=recalls[rid], verdict=verdict, score=round(score, 3),
              reasons=[f"name matches on: {', '.join(top)}"])
        for score, rid, verdict, top in scored
        if rid in recalls
    ]


def check(conn, query, include_closed=False, state=None, limit=10):
    """Run one Query against the corpus. Returns Matches, strongest first."""
    matches = []
    seen = set()

    if query.gtin:
        for m in _by_barcode(conn, query.gtin, include_closed):
            matches.append(m)
            seen.add(m.recall["id"])

    # The state filter is applied below, after scoring, so widen the candidate
    # window first - otherwise filtering can return fewer results than exist.
    text_limit = limit * 5 if state else limit
    for m in _by_text(conn, query, include_closed, text_limit):
        if m.recall["id"] not in seen:
            matches.append(m)
            seen.add(m.recall["id"])

    if state:
        matches = [m for m in matches if _distributed_to(conn, m.recall["id"], state)]

    order = {CERTAIN: 0, LIKELY: 1, POSSIBLE: 2}
    matches.sort(key=lambda m: (order[m.verdict], -m.score, m.recall["id"]))
    return matches[:limit]


def _distributed_to(conn, recall_id, state):
    """Nationwide recalls and recalls with no parsed distribution always pass -
    a missing distribution list must never hide a recall from a family."""
    rows = {r["state"] for r in conn.execute(
        "SELECT state FROM recall_states WHERE recall_id=?", (recall_id,))}
    return not rows or "US" in rows or state.upper() in rows
