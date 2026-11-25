import unittest

from utils.validation import (
    normalize_birthday,
    normalize_member_role,
    normalize_positive_amount,
)


class ValidationTests(unittest.TestCase):
    def test_birthday_is_normalized(self):
        self.assertEqual(normalize_birthday("1.2"), "01.02")
        self.assertEqual(normalize_birthday("29.02"), "29.02")

    def test_impossible_birthday_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_birthday("31.02")

    def test_only_supported_roles_are_accepted(self):
        self.assertEqual(normalize_member_role(" main "), "Main")
        self.assertEqual(normalize_member_role("super friend"), "Super Friend")
        self.assertEqual(normalize_member_role("FRIEND"), "Friend")
        with self.assertRaises(ValueError):
            normalize_member_role("Administrator")

    def test_amount_must_be_positive(self):
        self.assertEqual(normalize_positive_amount("100"), 100)
        for value in ("nope", "0", "-1", "9000000000000001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_positive_amount(value)
