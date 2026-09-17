"""Closed-set tests for format_tid -- pure function, no DB needed."""

import re

from django.test import SimpleTestCase

from .tid_format import format_tid


class FormatTidTests(SimpleTestCase):
    def test_already_masked_tid_preserved_verbatim(self):
        self.assertEqual(format_tid("COY-568796", 568796), "COY-568796")

    def test_already_masked_tid_preserved_even_if_digits_dont_match_ticket_id(self):
        # WHMCS's own masked tid isn't required to relate to the internal
        # id at all -- if it already looks right, it IS WHMCS's real value,
        # never regenerated.
        self.assertEqual(format_tid("COY-568796", 999), "COY-568796")

    def test_blank_tid_generates_masked_format(self):
        result = format_tid("", 199093)
        self.assertRegex(result, r"^[A-Z]{3}-\d{6}$")

    def test_none_tid_generates_masked_format(self):
        result = format_tid(None, 199093)
        self.assertRegex(result, r"^[A-Z]{3}-\d{6}$")

    def test_plain_numeric_tid_generates_masked_format(self):
        result = format_tid("199093", 199093)
        self.assertRegex(result, r"^[A-Z]{3}-\d{6}$")

    def test_generated_digits_are_the_real_ticket_id_zero_padded(self):
        self.assertEqual(format_tid("199093", 199093)[-6:], "199093")
        self.assertEqual(format_tid("", 42)[-6:], "000042")

    def test_generation_is_deterministic(self):
        self.assertEqual(format_tid("199093", 199093), format_tid("199093", 199093))
        self.assertEqual(format_tid("", 143957), format_tid(None, 143957))

    def test_different_ticket_ids_generate_different_tids(self):
        self.assertNotEqual(format_tid("", 1), format_tid("", 2))

    def test_whitespace_only_tid_treated_as_blank(self):
        result = format_tid("   ", 5000)
        self.assertRegex(result, r"^[A-Z]{3}-\d{6}$")
        self.assertTrue(result.endswith("005000"))

    def test_synthesize_false_uses_real_tid_verbatim_even_if_not_masked(self):
        # The exact case that motivated this flag: a real, distinct WHMCS
        # ticket number ("453611") that doesn't look letter-masked and isn't
        # equal to the internal id (143966) either -- still a real ticket
        # number that must not be thrown away.
        self.assertEqual(format_tid("453611", 143966, synthesize=False), "453611")

    def test_synthesize_false_still_preserves_an_already_masked_tid(self):
        self.assertEqual(format_tid("COY-568796", 568796, synthesize=False), "COY-568796")

    def test_synthesize_false_falls_back_to_internal_id_when_genuinely_blank(self):
        self.assertEqual(format_tid("", 199093, synthesize=False), "199093")
        self.assertEqual(format_tid(None, 199093, synthesize=False), "199093")
        self.assertEqual(format_tid("   ", 199093, synthesize=False), "199093")
