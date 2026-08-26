import unittest

from recall_radar import db, watchlist
from recall_radar.matcher import Match
from recall_radar.sources.base import blank_record


def _rec(**kw):
    r = blank_record("fda")
    r.update(id="fda:X", recall_number="X", status="Ongoing", classification="Class I",
             recalling_firm="Bazzini LLC", product_description="Almond Bites",
             reason="peanuts", distribution_pattern="FL, GA",
             code_info="UPC Code: 7 40235 50011 0", report_date="2026-08-19")
    r.update(kw)
    return r


class TestUpsert(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def q1(self, sql, *a):
        return self.conn.execute(sql, a).fetchone()[0]

    def test_derived_rows_are_populated(self):
        db.upsert_recall(self.conn, _rec())
        self.assertEqual(self.q1("SELECT COUNT(*) FROM recall_upcs"), 1)
        self.assertEqual(self.q1("SELECT gtin FROM recall_upcs"), "00740235500110")
        self.assertEqual(
            {r["state"] for r in self.conn.execute("SELECT state FROM recall_states")},
            {"FL", "GA"})
        self.assertTrue(self.q1("SELECT COUNT(*) FROM recall_tokens") > 0)

    def test_upsert_is_idempotent(self):
        db.upsert_recall(self.conn, _rec())
        db.upsert_recall(self.conn, _rec())
        self.assertEqual(self.q1("SELECT COUNT(*) FROM recalls"), 1)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM recall_upcs"), 1)

    def test_status_updates_are_picked_up_on_resync(self):
        """Agencies edit recalls in place after publication."""
        db.upsert_recall(self.conn, _rec())
        db.upsert_recall(self.conn, _rec(status="Terminated", termination_date="2026-09-01"))
        self.assertEqual(self.q1("SELECT status FROM recalls"), "Terminated")

    def test_derived_rows_do_not_accumulate_when_text_changes(self):
        db.upsert_recall(self.conn, _rec())
        db.upsert_recall(self.conn, _rec(distribution_pattern="TX"))
        self.assertEqual(
            {r["state"] for r in self.conn.execute("SELECT state FROM recall_states")},
            {"TX"})


class TestWatchlistAlerts(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.upsert_recall(self.conn, _rec())

    def tearDown(self):
        self.conn.close()

    def test_alert_is_recorded_once_so_families_are_not_told_twice(self):
        wid = watchlist.add(self.conn, "Bazzini bites", brand="Bazzini")
        recall = dict(self.conn.execute("SELECT * FROM recalls").fetchone())
        m = Match(recall=recall, verdict="likely", score=1.0)
        self.assertTrue(watchlist.record_alert(self.conn, wid, m))
        self.assertFalse(watchlist.record_alert(self.conn, wid, m))

    def test_remove(self):
        wid = watchlist.add(self.conn, "Thing")
        self.assertEqual(watchlist.remove(self.conn, wid), 1)
        self.assertEqual(watchlist.all_items(self.conn), [])


if __name__ == "__main__":
    unittest.main()
