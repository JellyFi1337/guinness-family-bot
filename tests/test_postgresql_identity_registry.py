"""Identity-registry diff helpers and offline regression (F13)."""

from __future__ import annotations

import unittest

from infrastructure.postgresql import _IDENTITY_TABLES, diff_identity_registry


class IdentityRegistryDiffTests(unittest.TestCase):
    def test_empty_diff_when_sets_match(self):
        tables = {"cars", "members"}
        self.assertEqual(diff_identity_registry(tables, tables), ([], []))

    def test_reports_both_mismatch_directions(self):
        registry = {"cars", "permission_grants"}
        with_id = {"cars", "tickets"}
        missing_id, missing_registry = diff_identity_registry(registry, with_id)
        self.assertEqual(missing_id, ["permission_grants"])
        self.assertEqual(missing_registry, ["tickets"])

    def test_known_composite_key_tables_are_absent_from_registry(self):
        for table in (
            "permission_grants",
            "contract_run_participants",
            "contract_report_shares",
        ):
            self.assertNotIn(table, _IDENTITY_TABLES)


if __name__ == "__main__":
    unittest.main()
