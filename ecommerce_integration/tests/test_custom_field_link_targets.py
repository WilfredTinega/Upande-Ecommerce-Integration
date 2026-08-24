# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The custom fields this app adds to other apps' doctypes must never be created
with a Link pointing at a DocType that isn't installed.

`create_custom_field` is called with `ignore_validate=True`, so nothing else
stops it: the field lands on Sales Order / Warehouse with a broken control, and
Frappe's test-record dependency walk aborts on it with "DocType <x> not found"
(which is exactly how this surfaced — as a CI failure on "Business Unit", owned
by upande_core, on a site where that app isn't installed).

These tests live in the app-level `tests/` package on purpose: only test modules
inside a `doctype/` folder trigger the runner's test-record machinery, and this
invariant needs none of it.
"""

import frappe

from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_custom_fields import (
	BIFLORICA_CUSTOM_FIELDS,
	_missing_link_target,
	check_biflorica_custom_fields,
)
from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
	FLORIDAY_CUSTOM_FIELDS,
	check_floriday_custom_fields,
)
from ecommerce_integration.testing import IntegrationTestCase

LINK_FIELDTYPES = ("Link", "Table", "Table MultiSelect")


class TestCustomFieldLinkTargets(IntegrationTestCase):
	def test_missing_link_target_only_flags_absent_link_targets(self):
		self.assertEqual(
			_missing_link_target({"fieldtype": "Link", "options": "No Such DocType Here"}),
			"No Such DocType Here",
		)
		self.assertIsNone(_missing_link_target({"fieldtype": "Link", "options": "User"}))
		# Not a link: `options` means something else entirely (Select values).
		self.assertIsNone(_missing_link_target({"fieldtype": "Select", "options": "No Such DocType Here"}))
		self.assertIsNone(_missing_link_target({"fieldtype": "Data"}))

	def test_no_dangling_link_was_created(self):
		"""Every field this app owns that made it onto the site must resolve.

		install / migrate has already run both ensure_* steps by the time tests
		do, so this reads back what they actually created — no DDL of its own.
		"""
		dangling = []
		for spec in list(FLORIDAY_CUSTOM_FIELDS) + list(BIFLORICA_CUSTOM_FIELDS):
			df = spec["df"]
			if df.get("fieldtype") not in LINK_FIELDTYPES:
				continue
			if not frappe.db.exists("Custom Field", {"dt": spec["dt"], "fieldname": df["fieldname"]}):
				continue
			if not frappe.db.exists("DocType", df["options"]):
				dangling.append(f"{spec['dt']}.{df['fieldname']} -> {df['options']}")

		self.assertEqual(dangling, [], f"custom field(s) linking a missing DocType: {dangling}")

	def test_check_reports_a_row_per_spec(self):
		for rows, specs in (
			(check_floriday_custom_fields(), FLORIDAY_CUSTOM_FIELDS),
			(check_biflorica_custom_fields(), BIFLORICA_CUSTOM_FIELDS),
		):
			self.assertEqual(len(rows), len(specs))
			for row in rows:
				self.assertIn("link_target_missing", row)
				if row["link_target_missing"]:
					self.assertFalse(frappe.db.exists("DocType", row["link_target_missing"]))
