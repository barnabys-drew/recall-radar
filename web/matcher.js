/* The matcher, ported to the browser.
 *
 * This is `recall_radar/matcher.py` and the parts of `recall_radar/normalize.py`
 * it depends on, rewritten in JavaScript so a phone with no signal can answer
 * the same question the CLI answers. It is a port, not a reimplementation: any
 * drift here is drift in what counts as a recall, so it mirrors the Python line
 * for line rather than being "improved" along the way.
 *
 * `tests/test_web_parity.py` runs both against the same corpus and fails if
 * they ever disagree.
 *
 * build.py inlines this into the page; Node loads it directly for the test.
 */
function RecallMatcher(DATA) {
"use strict";

// ===========================================================================
// normalize.py, ported. Any drift here is drift in what counts as a match, so
// these mirror the Python line for line rather than being "improved".
// ===========================================================================
const STOPWORDS = new Set(`
a an and or the of in with for to by from at on
inc llc l.l.c corp corporation co company ltd limited holdings brands foods food
oz ozs ounce ounces lb lbs pound pounds g gr gram grams kg ml l liter liters
fl floz net wt weight ct count pk pack packs package packages packaged
bag bags box boxes case cases carton cartons pouch pouches jar jars can cans
bottle bottles container containers tray trays tub tubs wrapper wrapped sleeve
plastic paper clear vacuum sealed frozen refrigerated fresh
item items product products size sizes each per approx approximately
`.trim().split(/\s+/));

function normalizeText(value) {
  if (!value) return "";
  return String(value).normalize("NFKD").replace(/\p{M}/gu, "")
    .replace(/\s+/g, " ").trim().toLowerCase();
}

function tokenize(value) {
  return (normalizeText(value).match(/[a-z0-9]+/g) || [])
    .filter((t) => !STOPWORDS.has(t) && !/^\d+$/.test(t) && t.length > 1);
}

function toGtin14(digits) {
  const d = String(digits || "").replace(/\D/g, "");
  if (![12, 13, 14].includes(d.length)) return null;
  return d.padStart(14, "0");
}

// UPC-E is a zero-suppressed UPC-A, and BarcodeDetector reports the 8-digit
// short form. toGtin14 rejects that length by design, so the scanner expands it
// first. This is an input adapter, like OCR would be - the matcher still only
// ever sees a 12-to-14 digit code.
function expandUpcE(d) {
  if (!/^\d{8}$/.test(d)) return d;
  const sys = d[0], body = d.slice(1, 7), check = d[7], last = body[5];
  let mid;
  if (last <= "2") mid = body.slice(0, 2) + last + "0000" + body.slice(2, 5);
  else if (last === "3") mid = body.slice(0, 3) + "00000" + body.slice(3, 5);
  else if (last === "4") mid = body.slice(0, 4) + "00000" + body[4];
  else mid = body.slice(0, 5) + "0000" + last;
  return sys + mid + check;
}

// ===========================================================================
// matcher.py, ported. A barcode is proof; a name is a guess; the two are never
// reported the same way.
// ===========================================================================
const CERTAIN = "certain", LIKELY = "likely", POSSIBLE = "possible";
const MIN_LIKELY = 0.85, MIN_POSSIBLE = 0.50, GENERIC_FLOOR = 0.85;
const DISTINCTIVE_DF_FRACTION = 0.005, DISTINCTIVE_MIN_DF = 3;

const Query = {
  fromText: (text) => ({ label: text, product: text, brand: null, gtin: null }),
  fromBarcode: (code, label) => ({ label: label || code, product: null, brand: null,
                                   gtin: toGtin14(code) }),
  fromWatch: (w) => ({ label: w.label, product: w.product || w.label,
                       brand: w.brand || null, gtin: toGtin14(w.gtin) }),
};

function idfOf(tokens) {
  const n = DATA.corpus, idf = {}, df = {};
  for (const t of tokens) {
    // An unseen token gets maximum weight: it is maximally distinctive, and it
    // simply will not match anything, so it can only lower a score honestly.
    df[t] = DATA.df[t] || 0;
    idf[t] = Math.log(n / (df[t] + 1));
  }
  return { idf, df };
}

function byBarcode(gtin) {
  return (DATA.u[gtin] || []).map((packed) => {
    const strong = (packed & 1) === 1;
    return {
      idx: packed >> 1,
      verdict: strong ? CERTAIN : LIKELY,
      score: strong ? 1.0 : 0.8,
      reasons: ["barcode " + gtin + " appears in this recall's code information" +
                (strong ? "" : " (unverified check digit)")],
    };
  });
}

function byText(query, limit) {
  const brandTokens = new Set(tokenize(query.brand));
  const productTokens = new Set(tokenize(query.product));
  const tokens = [...new Set([...brandTokens, ...productTokens])];
  if (!tokens.length) return [];

  const { idf, df } = idfOf(tokens);
  const total = tokens.reduce((s, t) => s + idf[t], 0) || 1.0;

  // The most distinctive token that exists in the corpus carries the query's
  // identity. If it did not match, the overlap is probably coincidence, so the
  // result is capped at POSSIBLE rather than dropped. Tokens absent from the
  // corpus are ignored: a misspelled brand must not cap an otherwise exact hit.
  let keyToken = null;
  for (const t of tokens) {
    if (df[t] > 0 && (keyToken === null || idf[t] > idf[keyToken])) keyToken = t;
  }
  const cutoff = Math.max(DISTINCTIVE_MIN_DF, DISTINCTIVE_DF_FRACTION * DATA.corpus);

  const hits = new Map();
  for (const t of tokens) {
    for (const packed of DATA.px[t] || []) {
      const idx = packed >> 2;
      let m = hits.get(idx);
      if (!m) hits.set(idx, (m = new Map()));
      m.set(t, (m.get(t) || 0) | (packed & 3));
    }
  }

  const scored = [];
  for (const [idx, matched] of hits) {
    let weight = 0;
    for (const [tok, fields] of matched) {
      let w = idf[tok];
      // A brand word landing on the recalling firm is the strongest text signal
      // available; the same word buried in a description is weaker.
      if (brandTokens.has(tok) && (fields & 1)) w *= 1.5;
      else if (brandTokens.has(tok) && (fields & 2)) w *= 1.1;
      weight += w;
    }
    const score = Math.min(weight / total, 1.0);
    let rarestDf = Infinity;
    for (const tok of matched.keys()) rarestDf = Math.min(rarestDf, df[tok]);

    let verdict;
    if (rarestDf <= cutoff) {
      // A one-word query like "chicken" covers itself perfectly and so scores
      // 1.0; without this gate, perfect coverage of a generic word would read
      // as LIKELY RECALLED.
      if (score < MIN_POSSIBLE) continue;
      verdict = score >= MIN_LIKELY ? LIKELY : POSSIBLE;
      if (keyToken && !matched.has(keyToken)) verdict = POSSIBLE;
    } else {
      if (score < GENERIC_FLOOR) continue;
      verdict = POSSIBLE;
    }

    let top = [...matched.keys()]
      .sort((a, b) => idf[b] - idf[a] || cmp(a, b)).slice(0, 4);
    if (keyToken && !matched.has(keyToken)) top = top.concat(["but not '" + keyToken + "'"]);
    scored.push({ idx, verdict, score: Math.round(score * 1000) / 1000,
                  reasons: ["name matches on: " + top.join(", ")] });
  }

  scored.sort((a, b) => b.score - a.score || cmp(idOf(a.idx), idOf(b.idx)));
  return scored.slice(0, limit);
}

// A missing distribution list must never hide a recall from a family, so
// nationwide recalls and unparsed ones always pass the filter.
function distributedTo(idx, state) {
  const st = DATA.s[idx] || [];
  return !st.length || st.includes("US") || st.includes(state.toUpperCase());
}

const RANK = { certain: 0, likely: 1, possible: 2 };

// Ties are broken by recall id, exactly as matcher.py does, so the ten results
// this returns are the same ten the CLI returns. Column 0 is the id - the one
// thing the matcher assumes about the row layout build.py bakes.
const idOf = (idx) => DATA.r[idx][0];
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function check(query, opts) {
  const state = (opts && opts.state) || null;
  const limit = (opts && opts.limit) || 10;
  const matches = [], seen = new Set();

  if (query.gtin) {
    for (const m of byBarcode(query.gtin)) { matches.push(m); seen.add(m.idx); }
  }
  // The state filter is applied after scoring, so widen the candidate window
  // first - otherwise filtering can return fewer results than exist.
  for (const m of byText(query, state ? limit * 5 : limit)) {
    if (!seen.has(m.idx)) { matches.push(m); seen.add(m.idx); }
  }

  const out = state ? matches.filter((m) => distributedTo(m.idx, state)) : matches;
  out.sort((a, b) => RANK[a.verdict] - RANK[b.verdict] || b.score - a.score ||
                     cmp(idOf(a.idx), idOf(b.idx)));
  return out.slice(0, limit);
}

return { check, byText, byBarcode, distributedTo, Query,
         tokenize, normalizeText, toGtin14, expandUpcE,
         CERTAIN, LIKELY, POSSIBLE, RANK };
}

if (typeof module !== "undefined" && module.exports) module.exports = RecallMatcher;
