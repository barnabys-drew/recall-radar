import unittest

from recall_radar import db, matcher
from recall_radar.matcher import CERTAIN, LIKELY, POSSIBLE, Query
from recall_radar.sources.base import blank_record


def _recall(rid, firm, product, code_info="", status="Ongoing",
            classification="Class I", distribution="Nationwide"):
    rec = blank_record("fda")
    rec.update(id=rid, recall_number=rid, status=status, classification=classification,
               recalling_firm=firm, product_description=product, reason="test",
               distribution_pattern=distribution, code_info=code_info,
               report_date="2026-08-01")
    return rec


# Enough filler that inverse document frequency is meaningful: "organic" and
# "baby" are common here, "bazzini" is not.
FIXTURES = [
    _recall("fda:A", "Bazzini LLC", "Dark Chocolate Coconut Almond Bites, 3.17oz",
            "UPC Code: 7 40235 50011 0"),
    _recall("fda:B", "M.O.M Enterprises", "Organic BABY bedtime drops, dietary supplement"),
    _recall("fda:C", "Green Fields Inc", "Organic baby carrots, 1 lb bag"),
    _recall("fda:D", "Sunrise Organic Co", "Organic baby kale, clamshell"),
    _recall("fda:E", "Closed Firm", "Organic baby spinach, 5oz", status="Terminated"),
    _recall("fda:F", "Regional Dairy", "Whole milk gallon", distribution="FL, GA"),
] + [
    _recall(f"fda:pad{i}", f"Filler Foods {i}", f"Assorted organic snack item {i}")
    for i in range(40)
]


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        for rec in FIXTURES:
            db.upsert_recall(self.conn, rec)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def check(self, text, **kw):
        return matcher.check(self.conn, Query.from_text(text), **kw)

    def test_barcode_match_is_certain(self):
        m = matcher.check(self.conn, Query.from_barcode("740235500110"))
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].verdict, CERTAIN)
        self.assertEqual(m[0].recall["id"], "fda:A")

    def test_barcode_that_matches_nothing_returns_nothing(self):
        self.assertEqual(matcher.check(self.conn, Query.from_barcode("012345678905")), [])

    def test_full_name_match_is_likely(self):
        m = self.check("Bazzini chocolate almond bites")
        self.assertEqual(m[0].verdict, LIKELY)
        self.assertEqual(m[0].recall["id"], "fda:A")

    def test_partial_generic_overlap_never_reports_likely(self):
        """'organic baby spinach' overlaps bedtime drops on two common words.
        It may surface, but must never be announced as a recall."""
        for m in self.check("organic baby spinach"):
            self.assertEqual(m.verdict, POSSIBLE, m.recall["product_description"])

    def test_unrelated_query_matches_nothing(self):
        self.assertEqual(self.check("Ben and Jerry ice cream"), [])

    def test_single_generic_word_is_never_likely(self):
        for m in self.check("organic"):
            self.assertEqual(m.verdict, POSSIBLE)

    def test_terminated_recalls_are_hidden_by_default(self):
        ids = [m.recall["id"] for m in self.check("organic baby spinach", include_closed=True)]
        self.assertIn("fda:E", ids)
        ids_open = [m.recall["id"] for m in self.check("organic baby spinach")]
        self.assertNotIn("fda:E", ids_open)

    def test_state_filter_keeps_nationwide_recalls(self):
        """A nationwide recall must reach every state."""
        m = matcher.check(self.conn, Query.from_barcode("740235500110"), state="IL")
        self.assertEqual(len(m), 1)

    def test_state_filter_excludes_other_regions(self):
        self.assertEqual(self.check("Regional Dairy whole milk", state="CA"), [])
        self.assertTrue(self.check("Regional Dairy whole milk", state="FL"))


if __name__ == "__main__":
    unittest.main()
