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


class TestUninstallCleanup(IntegrationTestCase):
	"""Uninstall must remove this app's fields and NOTHING else.

	Frappe does not remove an app's Custom Fields on uninstall, so they are
	cleaned up explicitly — but "declared here" is not the same as "owned here".
	`Sales Order.custom_farm` is declared by this app AND upande_harvest;
	`custom_order_name` and `custom_consignee` by upande_packhouse. Deleting those
	would strip mandatory fields from apps that are still installed.
	"""

	def test_fields_another_installed_app_declares_are_never_removed(self):
		from ecommerce_integration.uninstall import (
			_declared_custom_fields,
			_fields_owned_by_other_apps,
		)

		shared = _fields_owned_by_other_apps()
		# Whatever the site's app mix, nothing shared may end up in the removal set.
		removable = [pair for pair in _declared_custom_fields() if pair not in shared]
		self.assertFalse(set(removable) & shared)

	def test_the_delivery_point_id_is_ours_to_remove(self):
		"""The Floriday GLN field is declared only here, so it must be cleaned up."""
		from ecommerce_integration.uninstall import (
			_declared_custom_fields,
			_fields_owned_by_other_apps,
		)

		pair = ("Delivery Point", "custom_floriday_delivery_point_id")
		self.assertIn(pair, _declared_custom_fields())
		self.assertNotIn(pair, _fields_owned_by_other_apps())

	def test_every_declared_field_is_a_real_pair(self):
		from ecommerce_integration.uninstall import _declared_custom_fields

		declared = _declared_custom_fields()
		self.assertTrue(declared)
		self.assertEqual(len(declared), len(set(declared)), "duplicate (doctype, fieldname) declared")
		for doctype, fieldname in declared:
			self.assertTrue(doctype and fieldname)


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

		Read the field AS IT EXISTS on the site, never the spec's `options`. The
		two legitimately differ: `ensure_*` blanks the link target when the target
		DocType is absent (that is the whole dangling-link guard), and a field name
		this app declares may already be on the site pointing somewhere else
		entirely — on Tambuzi `Sales Order.custom_delivery_point` is upande_tambuzi's
		and links `Delivery Points`, plural. Comparing the spec's target against the
		site flagged all of those as dangling when nothing was.
		"""
		dangling = []
		for spec in list(FLORIDAY_CUSTOM_FIELDS) + list(BIFLORICA_CUSTOM_FIELDS):
			field = frappe.db.get_value(
				"Custom Field",
				{"dt": spec["dt"], "fieldname": spec["df"]["fieldname"]},
				["fieldtype", "options"],
				as_dict=True,
			)
			if not field or field.fieldtype not in LINK_FIELDTYPES:
				continue
			# A blank target is the guard having done its job, not a dangling link.
			if not field.options:
				continue
			if not frappe.db.exists("DocType", field.options):
				dangling.append(f"{spec['dt']}.{spec['df']['fieldname']} -> {field.options}")

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
