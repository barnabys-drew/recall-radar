import unittest

from recall_radar.sources import fsis, openfda
from recall_radar.sources.base import iso_date


class TestBase(unittest.TestCase):
    def test_iso_date(self):
        self.assertEqual(iso_date("20260819"), "2026-08-19")
        self.assertIsNone(iso_date(""))
        self.assertEqual(iso_date("2026-08-19"), "2026-08-19")


class TestFSISQuirks(unittest.TestCase):
    def test_unwraps_stringified_python_lists(self):
        self.assertEqual(fsis._unwrap("['Nationwide']"), "Nationwide")
        self.assertEqual(fsis._unwrap("['California', 'New York']"), "California; New York")
        self.assertEqual(fsis._unwrap("Plain string"), "Plain string")
        self.assertIsNone(fsis._unwrap("[]"))

    def test_unwrap_survives_malformed_input(self):
        self.assertEqual(fsis._unwrap("[not valid python"), "[not valid python")

    def test_firm_parsed_from_recall_title(self):
        self.assertEqual(
            fsis._firm_from_title("City Foods, Inc Recalls Beef Products Due To Contamination"),
            "City Foods, Inc")

    def test_public_health_alerts_name_no_firm(self):
        """PHA titles have no 'Recalls' verb; the old parser returned the whole
        headline as the company name."""
        self.assertIsNone(
            fsis._firm_from_title("FSIS Issues Public Health Alert for Various Meat Products"))

    def test_active_notice_maps_to_shared_status_vocabulary(self):
        rec = fsis._to_record({"field_recall_number": "001-2026",
                               "field_active_notice": "True",
                               "field_archive_recall": "False",
                               "field_title": "Acme Recalls Chicken"})
        self.assertEqual(rec["status"], "Ongoing")
        self.assertEqual(rec["source"], "fsis")
        self.assertEqual(rec["id"], "fsis:001-2026")

    def test_archived_recall_is_not_ongoing(self):
        rec = fsis._to_record({"field_recall_number": "002-2020",
                               "field_active_notice": "True",
                               "field_archive_recall": "True",
                               "field_title": "Acme Recalls Beef"})
        self.assertEqual(rec["status"], "Terminated")

    def test_record_without_a_number_is_dropped(self):
        self.assertIsNone(fsis._to_record({"field_title": "No number here"}))


class TestOpenFDAMapping(unittest.TestCase):
    def test_maps_api_fields_to_shared_shape(self):
        rec = openfda._to_record({
            "recall_number": "H-1228-2026", "event_id": "9999",
            "status": "Ongoing", "classification": "Class I",
            "recalling_firm": "Bazzini LLC",
            "product_description": "Dark Chocolate Almond Bites",
            "reason_for_recall": "Undeclared peanuts",
            "distribution_pattern": "WA", "code_info": "B15354",
            "report_date": "20260819", "recall_initiation_date": "20260801",
        })
        self.assertEqual(rec["id"], "fda:H-1228-2026")
        self.assertEqual(rec["report_date"], "2026-08-19")
        self.assertEqual(rec["reason"], "Undeclared peanuts")
        self.assertEqual(rec["source"], "fda")


if __name__ == "__main__":
    unittest.main()
