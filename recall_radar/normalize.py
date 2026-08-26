"""Turn messy free-text recall fields into things we can actually match on.

The FDA publishes identifying codes as unstructured prose in `code_info`, e.g.

    'UPC Code: 7 40235 50011 0 Exp Date-Lot Code 01726-040225 11245-012825'
    'Lot Code 0082026U00232   Best By date:  01/26/2029'
    '21FEB2026, 28FEB2026, 12MAR2026'

so every consumer-facing feature depends on pulling structure back out of it.
"""

import re
import unicodedata

# Words that carry no brand signal: packaging, units, corporate suffixes.
STOPWORDS = frozenset("""
a an and or the of in with for to by from at on
inc llc l.l.c corp corporation co company ltd limited holdings brands foods food
oz ozs ounce ounces lb lbs pound pounds g gr gram grams kg ml l liter liters
fl floz net wt weight ct count pk pack packs package packages packaged
bag bags box boxes case cases carton cartons pouch pouches jar jars can cans
bottle bottles container containers tray trays tub tubs wrapper wrapped sleeve
plastic paper clear vacuum sealed frozen refrigerated fresh
item items product products size sizes each per approx approximately
""".split())

# A UPC-12 / EAN-13 / GTIN-14 may be printed with spaces or hyphens between groups.
_DIGIT_RUN = re.compile(r"\d[\d\s\-]{9,22}\d")
_UPC_KEYWORD = re.compile(r"(?:UPC|U\.P\.C|GTIN|EAN|BARCODE|BAR\s*CODE|SKU)", re.I)
_LOT_KEYWORD = re.compile(r"(?:LOT|BATCH|CODE)\s*(?:#|NO\.?|NUMBER|CODE)?\s*:?\s*", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")
_LOOKS_LIKE_DATE = re.compile(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")


def normalize_text(value):
    """Lowercase, strip accents, collapse whitespace."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip().lower()


def tokenize(value):
    """Meaningful lowercase tokens, stopwords and bare numbers removed."""
    return [
        t for t in _TOKEN.findall(normalize_text(value))
        if t not in STOPWORDS and not t.isdigit() and len(t) > 1
    ]


def to_gtin14(digits):
    """Pad a UPC-12 / EAN-13 to GTIN-14 so the three formats compare equal."""
    digits = re.sub(r"\D", "", digits or "")
    if len(digits) not in (12, 13, 14):
        return None
    return digits.rjust(14, "0")


def gtin_check_digit_ok(gtin14):
    """Validate the GS1 mod-10 check digit. Filters out lot numbers and dates
    that happen to be 12-14 digits long."""
    if not gtin14 or len(gtin14) != 14 or not gtin14.isdigit():
        return False
    body, check = gtin14[:13], int(gtin14[13])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == check


def extract_upcs(text):
    """Pull GTIN-14 barcodes out of free text.

    Returns a list of dicts: {gtin, confidence, raw}. `confidence` is 'high'
    when the digits sit near a UPC/GTIN keyword or pass the GS1 check digit,
    'low' when it is a bare digit run that could be something else entirely.
    """
    if not text:
        return []
    found, seen = [], set()
    for m in _DIGIT_RUN.finditer(str(text)):
        raw = m.group(0)
        gtin = to_gtin14(raw)
        if not gtin or gtin in seen:
            continue
        # A keyword within the preceding 40 chars is strong evidence.
        near_keyword = bool(_UPC_KEYWORD.search(str(text)[max(0, m.start() - 40):m.start()]))
        valid_check = gtin_check_digit_ok(gtin)
        if not (near_keyword or valid_check):
            continue  # bare unvalidated run - almost always a lot or date code
        seen.add(gtin)
        found.append({
            "gtin": gtin,
            "confidence": "high" if (near_keyword and valid_check) or valid_check else "low",
            "raw": raw.strip(),
        })
    return found


def extract_lot_codes(text):
    """Best-effort lot/batch codes. Deliberately loose: lot formats are chaotic,
    and we only ever use these to *explain* a match, never to make one."""
    if not text:
        return []
    codes, seen = [], set()
    text = str(text)
    for m in _LOT_KEYWORD.finditer(text):
        # "UPC Code:" is a barcode label, not a lot label - let extract_upcs own it.
        if _UPC_KEYWORD.search(text[max(0, m.start() - 12):m.start()]):
            continue
        tail = text[m.end():m.end() + 60]
        for tok in re.findall(r"[A-Z0-9][A-Z0-9\-/]{3,}", tail, re.I)[:6]:
            tok = tok.strip("-/").upper()
            # A lot code always carries a digit; this drops prose that trails
            # the keyword ("Best By date", "and", "Exp").
            if not tok or tok in seen or not any(c.isdigit() for c in tok):
                continue
            if _LOOKS_LIKE_DATE.fullmatch(tok):
                continue
            seen.add(tok)
            codes.append(tok)
    return codes


def states_from_distribution(pattern):
    """Parse `distribution_pattern` into USPS state codes.

    Nationwide language is reported as the sentinel 'US' - the caller decides
    what that means, because 'nationwide' must never filter a recall *out*.
    """
    text = normalize_text(pattern)
    if not text:
        return []
    if re.search(r"nationwide|nation wide|all states|throughout the (?:us|u\.s|united states)", text):
        return ["US"]
    found = {c.upper() for c in re.findall(r"\b([a-z]{2})\b", text) if c in _STATE_CODES}
    # FDA writes "FL, MI, MS"; FSIS writes "California; New York".
    # Longest name first, consuming each match, so "West Virginia" does not also
    # register as "Virginia".
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text):
            found.add(_STATE_NAMES[name])
            text = re.sub(pattern, " ", text)
    return sorted(found)


_STATE_CODES = frozenset("""
al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo
mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy
dc pr vi gu as mp
""".split())


_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}
