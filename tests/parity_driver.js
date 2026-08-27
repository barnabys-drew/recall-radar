/* Runs the browser matcher over a baked corpus, for tests/test_web_parity.py.
 *
 * Reads {"data": <bake output>, "queries": [...]} on stdin and writes one
 * result list per query on stdout. Deliberately dumb: the point is to exercise
 * web/matcher.js exactly as the page does, not to add logic of its own.
 */
const fs = require("fs");
const path = require("path");

const RecallMatcher = require(path.join(__dirname, "..", "web", "matcher.js"));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const M = RecallMatcher(input.data);

const out = input.queries.map((q) => {
  // Built through the same constructors the page uses, so the test exercises
  // the adapters too rather than hand-assembling a Query the page never makes.
  const query = q.gtin ? M.Query.fromBarcode(q.gtin, q.label) : M.Query.fromText(q.label);
  if (q.brand) query.brand = q.brand;
  if (q.product) query.product = q.product;
  return M.check(query, { state: q.state || null, limit: q.limit || 10 }).map((m) => ({
    id: input.data.r[m.idx][0],
    verdict: m.verdict,
    score: m.score,
    reason: m.reasons[0],
  }));
});

process.stdout.write(JSON.stringify(out));
