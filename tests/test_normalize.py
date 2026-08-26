import unittest

from recall_radar import normalize as n


class TestUPC(unittest.TestCase):
    def test_extracts_spaced_upc_after_keyword(self):
        got = n.extract_upcs("UPC Code: 7 40235 50011 0 Exp Date-Lot Code 01726-040225")
        self.assertEqual([u["gtin"] for u in got], ["00740235500110"])
        self.assertEqual(got[0]["confidence"], "high")

    def test_pads_upc12_and_ean13_to_the_same_space(self):
        self.assertEqual(n.to_gtin14("012345678905"), "00012345678905")
        self.assertEqual(len(n.to_gtin14("0123456789012")), 14)
        self.assertIsNone(n.to_gtin14("12345"))

    def test_rejects_date_strings_that_look_like_digit_runs(self):
        self.assertEqual(n.extract_upcs("21FEB2026, 28FEB2026, 12MAR2026"), [])

    def test_rejects_long_digit_runs_failing_the_check_digit(self):
        # A 12-digit lot number with no UPC keyword nearby must not become a barcode.
        self.assertEqual(n.extract_upcs("Batch 111111111111 packed today"), [])

    def test_check_digit(self):
        self.assertTrue(n.gtin_check_digit_ok("00740235500110"))
        self.assertFalse(n.gtin_check_digit_ok("00740235500111"))


class TestLotCodes(unittest.TestCase):
    def test_pulls_lot_and_ignores_trailing_prose(self):
        self.assertEqual(
            n.extract_lot_codes("Lot Code 0082026U00232   Best By date:  01/26/2029"),
            ["0082026U00232"])

    def test_does_not_claim_upc_digits(self):
        got = n.extract_lot_codes("UPC Code: 7 40235 50011 0")
        self.assertNotIn("40235", got)


class TestStates(unittest.TestCase):
    def test_fda_two_letter_codes(self):
        self.assertEqual(n.states_from_distribution("FL, MI, MS, and OH."),
                         ["FL", "MI", "MS", "OH"])

    def test_fsis_full_names(self):
        self.assertEqual(n.states_from_distribution("California; New York"), ["CA", "NY"])

    def test_west_virginia_is_not_also_virginia(self):
        self.assertEqual(n.states_from_distribution("West Virginia"), ["WV"])

    def test_nationwide_is_a_sentinel(self):
        self.assertEqual(n.states_from_distribution("Nationwide"), ["US"])


class TestTokenize(unittest.TestCase):
    def test_drops_packaging_and_corporate_noise(self):
        got = n.tokenize("Bazzini LLC 3.17oz Plastic Pouches, 10 Packages per case")
        self.assertIn("bazzini", got)
        for noise in ("llc", "oz", "plastic", "pouches", "packages", "case"):
            self.assertNotIn(noise, got)


if __name__ == "__main__":
    unittest.main()
