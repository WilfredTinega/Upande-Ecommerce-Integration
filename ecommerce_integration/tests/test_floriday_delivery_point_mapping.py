# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Naming a Floriday GLN has to reach the orders that already used it.

When Floriday can name neither the delivery location nor the organisation behind
a GLN, the importer creates a placeholder Delivery Point called "GLN <code>" so
the order still imports. That placeholder name then lands in four places on the
Sales Order: the Delivery Point link, Drop Off Point, Truck Details, and a
Shipping Agent created to match (Floriday names no cargo agent, so the delivery
point fills that role).

Giving the GLN a real name therefore cannot be a matter of moving a tag. The tag
is unique, so the placeholder holds it hostage; and Drop Off Point and Truck
Details are Data fields, so no rename would follow them. These tests pin the
rename/merge behaviour that makes all four land on the real name, and the guards
that stop it renaming a Delivery Point somebody named themselves.
"""

from unittest.mock import patch

import frappe

from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order import (
	DELIVERY_POINT_NAME_MIRRORS,
	ensure_delivery_point_for_gln,
	is_gln_placeholder,
	map_delivery_point,
	repoint_delivery_point_name,
)
from ecommerce_integration.testing import IntegrationTestCase
from ecommerce_integration.tests.fixtures import has

GLN = "8719604906480"
PLACEHOLDER = f"GLN {GLN}"
SALES_ORDER_MODULE = (
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order"
)


class TestGlnPlaceholderRecognition(IntegrationTestCase):
	"""Only names WE invented may be renamed on the operator's behalf."""

	def test_an_auto_created_gln_name_is_a_placeholder(self):
		self.assertTrue(is_gln_placeholder(PLACEHOLDER))
		self.assertTrue(is_gln_placeholder(PLACEHOLDER, GLN))

	def test_it_is_only_a_placeholder_for_its_own_gln(self):
		self.assertFalse(is_gln_placeholder(PLACEHOLDER, "1111111111111"))

	def test_a_name_somebody_chose_is_never_a_placeholder(self):
		for name in ("Royal FloraHolland Aalsmeer", "JKIA", "GLN Warehouse", "GLN ", "", None):
			with self.subTest(name=name):
				self.assertFalse(is_gln_placeholder(name))


class TestRepointDeliveryPointName(IntegrationTestCase):
	def test_a_no_op_rename_touches_nothing(self):
		for old, new in ((None, "X"), ("X", None), ("X", "X")):
			with self.subTest(old=old, new=new):
				self.assertEqual(repoint_delivery_point_name(old, new), {})

	def test_both_data_mirrors_are_covered(self):
		"""The two Data fields are the reason a rename alone is not enough."""
		self.assertEqual(set(DELIVERY_POINT_NAME_MIRRORS), {"custom_drop_off_point", "custom_truck_details"})


class TestMapDeliveryPointGuards(IntegrationTestCase):
	def test_missing_arguments_are_refused(self):
		for gln, name in ((None, "X"), ("123", None), ("", "")):
			with self.subTest(gln=gln, name=name):
				result = map_delivery_point(gln, name)
				self.assertEqual(result["status"], "error")
				self.assertIn("Missing", result["message"])

	def test_an_unknown_delivery_point_is_refused_when_there_is_no_placeholder(self):
		with patch(f"{SALES_ORDER_MODULE}.get_delivery_point_from_floriday_gln", return_value=None):
			result = map_delivery_point(GLN, "No Such Delivery Point Here")
		self.assertEqual(result["status"], "error")
		self.assertIn("not found", result["message"])

	def test_a_delivery_point_already_holding_another_gln_is_not_hijacked(self):
		"""Two GLNs on one Delivery Point cannot both be stored — say so."""
		with (
			patch(f"{SALES_ORDER_MODULE}.get_delivery_point_from_floriday_gln", return_value=None),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value="1111111111111"),
		):
			result = map_delivery_point(GLN, "Someone Elses Point")
		self.assertEqual(result["status"], "error")
		self.assertIn("already mapped to GLN 1111111111111", result["message"])


class TestMapDeliveryPointRenamesThePlaceholder(IntegrationTestCase):
	"""The whole point: the real name has to land on the order, not just the tag."""

	# A GLN of its own, so the fixture never collides with a real mapping the
	# site already holds — the tag is unique, and kaitet16 carries a live one.
	TEST_GLN = "9990000000001"
	TEST_PLACEHOLDER = f"GLN {TEST_GLN}"

	def setUp(self):
		if not has("Delivery Point"):
			self.skipTest("Delivery Point is not on this site")
		if not frappe.db.has_column("Delivery Point", "custom_floriday_delivery_point_id"):
			self.skipTest("Delivery Point has no Floriday GLN field on this site")

		self._forget_by_gln()
		self.addCleanup(self._forget_by_gln)

		placeholder = frappe.get_doc(
			{
				"doctype": "Delivery Point",
				"delivery_point": self.TEST_PLACEHOLDER,
				"custom_floriday_delivery_point_id": self.TEST_GLN,
			}
		).insert(ignore_permissions=True)

		if placeholder.name != self.TEST_PLACEHOLDER:
			# This site autonames Delivery Point (a hash on the CI stub) rather
			# than naming it after `delivery_point`, so "GLN <code>" is never a
			# docname here and the placeholder convention does not exist to test.
			self.skipTest(f"Delivery Point autonames ({placeholder.name}), not named by delivery_point")

	@classmethod
	def _forget_by_gln(cls):
		"""Clean up by GLN, not by name — the record gets RENAMED mid-test."""
		for name in frappe.get_all(
			"Delivery Point",
			filters={"custom_floriday_delivery_point_id": cls.TEST_GLN},
			pluck="name",
		):
			frappe.delete_doc("Delivery Point", name, force=True, ignore_permissions=True)

	def test_naming_the_gln_renames_the_placeholder_and_keeps_the_tag(self):
		target = "Royal FloraHolland Aalsmeer _Test"

		result = map_delivery_point(self.TEST_GLN, target)

		self.assertEqual(result["status"], "success", result)
		self.assertIn("renamed to", result["message"])
		self.assertFalse(frappe.db.exists("Delivery Point", self.TEST_PLACEHOLDER))
		self.assertEqual(
			frappe.db.get_value("Delivery Point", target, "custom_floriday_delivery_point_id"),
			self.TEST_GLN,
		)

	def test_mapping_twice_is_a_no_op_not_an_error(self):
		target = "Royal FloraHolland Aalsmeer _Test"

		first = map_delivery_point(self.TEST_GLN, target)
		self.assertEqual(first["status"], "success", first)

		again = map_delivery_point(self.TEST_GLN, target)
		self.assertEqual(again["status"], "success", again)
		self.assertIn("already mapped", again["message"])


class TestRenameHookIsRegistered(IntegrationTestCase):
	def test_renaming_a_delivery_point_in_the_desk_repoints_the_mirrors(self):
		"""Renaming the record in the desk is the natural way to name a GLN."""
		handlers = frappe.get_hooks("doc_events").get("Delivery Point", {}).get("after_rename", [])
		self.assertTrue(
			any("on_delivery_point_renamed" in h for h in handlers),
			f"after_rename handlers: {handlers}",
		)


class TestEveryGlnGetsItsOwnDeliveryPoint(IntegrationTestCase):
	"""An unresolved GLN must never be routed onto a shared catch-all Delivery Point.

	Sending it to JKIA (or any other shared record) throws away which location the
	order was for and merges unrelated buyers, so the importer always creates a
	Delivery Point carrying the GLN — named by Floriday when Floriday knows the
	name, "GLN <code>" when it doesn't.
	"""

	# One GLN per test: the tag is unique, so a GLN reused across tests would be
	# resolved from the record the previous test left behind.
	UNNAMED_GLN = "9990000000002"
	UNNAMED_GLN_2 = "9990000000003"
	NAMED_GLN = "9990000000004"
	REUSED_GLN = "9990000000005"
	CLASHING_GLN = "9990000000006"
	HOLDER_GLN = "9990000000007"

	def setUp(self):
		if not has("Delivery Point"):
			self.skipTest("Delivery Point is not on this site")
		if not frappe.db.has_column("Delivery Point", "custom_floriday_delivery_point_id"):
			self.skipTest("Delivery Point has no Floriday GLN field on this site")
		if self._insert_point("_Test EI Naming Probe") != "_Test EI Naming Probe":
			self.skipTest("Delivery Point autonames rather than being named by delivery_point")

		# Floriday is not called: each test declares what, if anything, it names.
		self.floriday_names = {}
		for target, value in (
			("fetch_floriday_delivery_location_by_gln", self._named_location),
			("fetch_floriday_organization_by_gln", lambda gln, settings: None),
		):
			patcher = patch(f"{SALES_ORDER_MODULE}.{target}", side_effect=value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _named_location(self, gln, settings):
		name = self.floriday_names.get(gln)
		return {"name": name} if name else None

	@staticmethod
	def _insert_point(name, gln=None):
		if frappe.db.exists("Delivery Point", name):
			return name
		doc = frappe.get_doc({"doctype": "Delivery Point", "delivery_point": name})
		if gln:
			doc.custom_floriday_delivery_point_id = gln
		return doc.insert(ignore_permissions=True).name

	@staticmethod
	def _tag_of(name):
		return frappe.db.get_value("Delivery Point", name, "custom_floriday_delivery_point_id")

	def test_a_gln_floriday_cannot_name_still_gets_a_tagged_placeholder(self):
		name = ensure_delivery_point_for_gln(self.UNNAMED_GLN, frappe._dict())

		self.assertEqual(name, f"GLN {self.UNNAMED_GLN}")
		self.assertEqual(self._tag_of(name), self.UNNAMED_GLN)

	def test_an_existing_jkia_is_not_used_as_a_catch_all(self):
		self._insert_point("JKIA")

		name = ensure_delivery_point_for_gln(self.UNNAMED_GLN_2, frappe._dict())

		self.assertNotEqual(name, "JKIA")
		self.assertIsNone(self._tag_of("JKIA"), "JKIA must not be tagged with somebody's GLN")

	def test_floriday_s_own_name_is_used_when_it_has_one(self):
		self.floriday_names[self.NAMED_GLN] = "_Test EI Nairobi Airport"

		name = ensure_delivery_point_for_gln(self.NAMED_GLN, frappe._dict())

		self.assertEqual(name, "_Test EI Nairobi Airport")
		self.assertEqual(self._tag_of(name), self.NAMED_GLN)

	def test_an_untagged_delivery_point_of_that_name_is_reused_and_tagged(self):
		self._insert_point("_Test EI Untagged Point")
		self.floriday_names[self.REUSED_GLN] = "_Test EI Untagged Point"

		name = ensure_delivery_point_for_gln(self.REUSED_GLN, frappe._dict())

		self.assertEqual(name, "_Test EI Untagged Point")
		self.assertEqual(self._tag_of(name), self.REUSED_GLN)

	def test_a_name_another_gln_holds_is_not_hijacked(self):
		"""Two delivery locations can share a name; they cannot share the tag."""
		held = self._insert_point("_Test EI Shared Name", gln=self.HOLDER_GLN)
		self.floriday_names[self.CLASHING_GLN] = "_Test EI Shared Name"

		name = ensure_delivery_point_for_gln(self.CLASHING_GLN, frappe._dict())

		self.assertEqual(name, f"_Test EI Shared Name ({self.CLASHING_GLN})")
		self.assertEqual(self._tag_of(name), self.CLASHING_GLN)
		self.assertEqual(self._tag_of(held), self.HOLDER_GLN, "the first GLN keeps its record")
