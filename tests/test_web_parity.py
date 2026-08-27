"""The page must answer exactly what the CLI answers.

`web/matcher.js` is a hand port of `recall_radar/matcher.py`. A port that drifts
is worse than no port at all: the phone would say "no open recall" about food
the command line knows is recalled, and nobody would find out until it mattered.
So both are run over one corpus here and every verdict is compared.

The corpus is built in this file rather than read from the user's database, so
the test is deterministic, needs no network, and still contains the specific
shapes that broke earlier versions of the scorer.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402
from recall_radar import db, matcher  # noqa: E402
from recall_radar.matcher import Query  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "parity_driver.js")
NODE = shutil.which("node")


def recall(rid, firm, product, status="Ongoing", cls="Class I", reason="Undeclared peanuts",
           dist="Nationwide", codes="", date="2026-08-01"):
    return {
        "id": rid, "source": rid.split(":")[0], "recall_number": rid.split(":")[1],
        "event_id": "1", "status": status, "classification": cls, "recalling_firm": firm,
        "product_description": product, "reason": reason, "distribution_pattern": dist,
        "code_info": codes, "report_date": date, "initiation_date": date,
        "termination_date": None, "url": "https://example.invalid/" + rid, "raw": {},
    }


# The awkward cases, all of which are real false positives the Python scorer was
# taught to reject. If the port disagrees on any of these it is broken in a way
# that matters, not in a way that only shows up on exotic input.
FIXTURES = [
    recall("fda:F-1", "Bazzini LLC",
           "Dark Chocolate Coconut Almond Bites, 3.17oz, Plastic Pouches",
           codes="UPC Code: 7 40235 50011 0 Lot B15354", dist="Distributed to WA."),
    recall("fda:F-2", "Chicken of the Sea International",
           "Chunk Light Tuna in Water, 5 oz cans", dist="Nationwide"),
    recall("fda:F-3", "Taylor Farms Retail, Inc.",
           "Taylor Farms Chopped Salad Kit, Southwest, 12.8 oz bag",
           dist="Distributed in IL, IN, and WI."),
    recall("fda:F-4", "Little Sprout Naturals",
           "Organic BABY bedtime drops, 2 fl oz bottle", cls="Class II"),
    recall("fda:F-5", "Green Fields Produce",
           "Organic Baby Spinach, 5 oz clamshell", dist="CA, OR, WA"),
    recall("fsis:001-2026", "Shanghai Ravioli Corporation",
           "Frozen pork and chive ravioli, 16 oz boxes", dist="Nationwide"),
    recall("fsis:002-2026", "Prairie Packing Co",
           "Ground beef chubs, 10 lb", status="Terminated", dist="Illinois; Iowa"),
    # A second Bazzini record so a barcode hit and a name hit can disagree.
    recall("fda:F-6", "Bazzini LLC", "Roasted Salted Cashews, 8oz tub",
           codes="UPC 7 40235 50022 4", cls="Class II", dist="Nationwide"),
]

# Filler, so document frequency has something to measure and the distinctiveness
# cutoff (0.5% of the corpus) sits where it does in production.
FILLER_WORDS = ["harvest", "valley", "orchard", "creek", "summit", "meadow", "ridge",
                "pioneer", "cardinal", "juniper", "willow", "basin"]
FILLER_PRODUCTS = ["cheddar cheese slices", "sliced turkey breast", "cinnamon granola",
                   "chocolate chip cookies", "baby carrots", "chicken soup",
                   "almond butter", "romaine lettuce", "salted crackers", "apple juice"]


def build_corpus():
    conn = db.connect(":memory:")
    for r in FIXTURES:
        db.upsert_recall(conn, r)
    for i in range(300):
        word = FILLER_WORDS[i % len(FILLER_WORDS)]
        product = FILLER_PRODUCTS[i % len(FILLER_PRODUCTS)]
        # Two thirds terminated, as in the real corpus: document frequency is
        # measured over everything, matching only over the open subset.
        status = "Ongoing" if i % 3 == 0 else "Terminated"
        db.upsert_recall(conn, recall(
            f"fda:X-{i}", f"{word.title()} Foods {i}", f"{word} {product}, {i} oz",
            status=status, cls="Class I" if i % 2 else "Class III",
            dist="Nationwide" if i % 4 else "Distributed in TX and OK.",
            date=f"2026-0{1 + i % 8}-15"))
    conn.commit()
    return conn


QUERIES = [
    # The cases the README calls out by name.
    {"label": "Bazzini chocolate almond bites"},
    {"label": "Ben and Jerry ice cream"},
    {"label": "taylor farms chopped salad"},
    {"label": "chicken"},
    {"label": "organic baby spinach"},
    {"label": "Cheerios cereal"},
    # Brand weighting: the same word as brand vs. buried in a description.
    {"label": "Bazzini cashews", "brand": "Bazzini"},
    {"label": "cashews", "brand": "Bazzini", "product": "Roasted Salted Cashews"},
    # Barcodes: valid, valid-but-unknown, and one that is not a barcode at all.
    {"label": "scan", "gtin": "740235500110"},
    {"label": "scan", "gtin": "7 40235 50022 4"},
    {"label": "scan", "gtin": "000000000000"},
    # Barcode plus typed name, the way the page pairs them.
    {"label": "scan", "gtin": "740235500110", "product": "chocolate almond bites"},
    # State filtering, including the nationwide-must-not-be-hidden rule.
    {"label": "taylor farms chopped salad", "state": "IL"},
    {"label": "taylor farms chopped salad", "state": "CA"},
    {"label": "ravioli", "state": "CA"},
    {"label": "organic baby spinach", "state": "IL"},
    # Generic-only, misspelled, empty, punctuation, accents, casing.
    {"label": "organic"},
    {"label": "frozen food"},
    {"label": "Bazinni chocolate almond bites"},
    {"label": ""},
    {"label": "   "},
    {"label": "Taylor Farms' chopped salad!!"},
    {"label": "BAZZINI CHOCOLATE ALMOND BITES"},
    {"label": "Bazziní chöcolate almond bites"},
    {"label": "harvest cheddar cheese slices"},
    {"label": "pioneer chicken soup"},
    {"label": "ground beef"},
    {"label": "12345"},
    {"label": "a"},
    {"label": "willow almond butter", "state": "TX"},
]


def python_results(conn, queries):
    out = []
    for q in queries:
        if q.get("gtin"):
            query = Query.from_barcode(q["gtin"], label=q["label"])
            if q.get("product"):
                query.product = q["product"]
        else:
            query = Query(label=q["label"], product=q.get("product", q["label"]),
                          brand=q.get("brand"))
        matches = matcher.check(conn, query, state=q.get("state"), limit=q.get("limit", 10))
        out.append([{"id": m.recall["id"], "verdict": m.verdict, "score": m.score,
                     "reason": m.reasons[0]} for m in matches])
    return out


def js_results(data, queries):
    proc = subprocess.run(
        [NODE, DRIVER], input=json.dumps({"data": data, "queries": queries}),
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("parity driver failed:\n" + proc.stderr)
    return json.loads(proc.stdout)


def tiers(results):
    """Group a result list into runs of equal (verdict, score).

    Within such a run the two implementations are free to disagree on order:
    Python breaks ties by SQL row order and JavaScript by token iteration order,
    and neither is meaningful. Between runs, order is the actual ranking and
    must match exactly.
    """
    grouped, current, key = [], [], None
    for m in results:
        k = (m["verdict"], round(m["score"], 3))
        if k != key:
            if current:
                grouped.append((key, current))
            current, key = [], k
        current.append(m["id"])
    if current:
        grouped.append((key, current))
    return [(k, sorted(ids)) for k, ids in grouped]


@unittest.skipIf(NODE is None, "node is not installed")
class WebParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = build_corpus()
        cls.data = build.bake(cls.conn)
        cls.py = python_results(cls.conn, QUERIES)
        cls.js = js_results(cls.data, QUERIES)

    def test_same_verdicts_and_ranking(self):
        for q, py, js in zip(QUERIES, self.py, self.js):
            with self.subTest(query=q["label"], state=q.get("state"), gtin=q.get("gtin")):
                self.assertEqual(tiers(py), tiers(js))

    def test_same_scores(self):
        for q, py, js in zip(QUERIES, self.py, self.js):
            with self.subTest(query=q["label"]):
                self.assertEqual({m["id"]: round(m["score"], 3) for m in py},
                                 {m["id"]: round(m["score"], 3) for m in js})

    def test_same_explanations(self):
        """The 'why' line is what a shopper reads to decide. It has to agree too."""
        for q, py, js in zip(QUERIES, self.py, self.js):
            with self.subTest(query=q["label"]):
                self.assertEqual({m["id"]: m["reason"] for m in py},
                                 {m["id"]: m["reason"] for m in js})

    def test_the_corpus_actually_exercises_the_gates(self):
        """A parity test that matched nothing anywhere would pass vacuously."""
        verdicts = {m["verdict"] for row in self.py for m in row}
        self.assertEqual(verdicts, {"certain", "likely", "possible"})
        self.assertTrue(any(not row for row in self.py), "no query returned a clean result")

    def test_terminated_recalls_are_not_in_the_page(self):
        baked = {r[0] for r in self.data["r"]}
        self.assertIn("fda:F-1", baked)
        self.assertNotIn("fsis:002-2026", baked)

    def test_document_frequency_spans_the_whole_corpus(self):
        """Not just the open subset - otherwise common words look distinctive."""
        total = self.conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0]
        self.assertEqual(self.data["corpus"], total)
        self.assertGreater(total, len(self.data["r"]))
        # 'beef' appears only in a terminated recall, so it is absent from the
        # page's postings but must still carry a document frequency.
        self.assertIn("beef", self.data["df"])
        self.assertNotIn("beef", self.data["px"])


if __name__ == "__main__":
    unittest.main()
