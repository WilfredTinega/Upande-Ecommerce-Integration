# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The local stock/price seeder.

Two properties matter more than the seeding itself:

  * it is idempotent — a second run must converge on the same shelf quantities
    and the same prices, not double them, because it will be run repeatedly
    while testing;
  * it never reaches Floriday or Biflorica. Seeding is a local act; posting is
    a separate, deliberate one.
"""

from unittest.mock import patch

import frappe

from ecommerce_integration.setup import simulate
from ecommerce_integration.testing import IntegrationTestCase
from ecommerce_integration.tests.fixtures import ensure_item, ensure_price_list, has


class TestStemLengthSelection(IntegrationTestCase):
	def test_lengths_outside_the_master_are_rejected_not_forced(self):
		if not has("Stem Length"):
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")

		field = frappe.get_meta("Stem Length").get_field("length")
		if not field or field.fieldtype != "Select":
			self.skipTest("this site's Stem Length.length is free text, so nothing is rejected")

		allowed, rejected = simulate._allowed_stem_lengths(["50CM", "999CM"])
		self.assertIn("999CM", rejected)
		self.assertNotIn("999CM", allowed)

	def test_variety_offset_is_stable_across_calls(self):
		"""Non-stable hashing would reprice every item on every run."""
		first = simulate._variety_offset("Wild Thing")
		self.assertEqual(first, simulate._variety_offset("Wild Thing"))
		self.assertLess(first, simulate.VARIETY_SPREAD)
		self.assertGreaterEqual(first, 0)


class TestSeeding(IntegrationTestCase):
	def setUp(self):
		if not has("Stem Length", "Shelf", "Shelf Item"):
			self.skipTest("the post-harvest Stem Length / Shelf doctypes are not installed")
		self.items = [
			frappe._dict(
				{
					"item_code": ensure_item("_Test EI Seed Rose"),
					"item_name": "_Test EI Seed Rose",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"sales_uom": None,
				}
			)
		]
		self.price_list = ensure_price_list()
		self.lengths, _ = simulate._allowed_stem_lengths(["50CM", "60CM"])
		if not self.lengths:
			self.skipTest("this site's Stem Length master accepts neither 50CM nor 60CM")

	def tearDown(self):
		for name in frappe.get_all(
			"Shelf", filters={"name": ["like", f"{simulate.SIM_SHELF_PREFIX}%"]}, pluck="name"
		):
			frappe.delete_doc("Shelf", name, ignore_permissions=True, force=True)

	def test_the_master_gets_a_price_for_every_length(self):
		master, error = simulate.ensure_stem_length_master(self.lengths)
		self.assertIsNone(error)
		self.assertEqual(len(master), len(self.lengths))
		for name in master.values():
			self.assertGreater(frappe.db.get_value("Stem Length", name, "price"), 0)

	def test_a_hand_set_master_price_is_never_overwritten(self):
		master, _ = simulate.ensure_stem_length_master(self.lengths)
		name = next(iter(master.values()))
		frappe.db.set_value("Stem Length", name, "price", 9.99)

		simulate.ensure_stem_length_master(self.lengths)
		self.assertEqual(frappe.db.get_value("Stem Length", name, "price"), 9.99)

	def test_shelf_stock_does_not_accumulate_across_runs(self):
		master, _ = simulate.ensure_stem_length_master(self.lengths)
		first = simulate.seed_shelf_stock(
			self.items, self.lengths, master=master, qty_per_length=100, warehouse=None
		)
		second = simulate.seed_shelf_stock(
			self.items, self.lengths, master=master, qty_per_length=100, warehouse=None
		)

		self.assertEqual(first["rows"], second["rows"])
		self.assertEqual(first["rows"], len(self.lengths) * len(self.items))
		total = frappe.db.sql(
			"select sum(stem_qty) from `tabShelf Item` where parent = %s", (second["shelf"],)
		)[0][0]
		self.assertEqual(int(total), 100 * first["rows"])

	def test_prices_converge_rather_than_duplicate(self):
		master, _ = simulate.ensure_stem_length_master(self.lengths)
		first = simulate.seed_item_prices(self.items, self.lengths, master=master, price_list=self.price_list)
		second = simulate.seed_item_prices(
			self.items, self.lengths, master=master, price_list=self.price_list
		)

		# How many rows land depends on whether this ERPNext tolerates more than
		# one Item Price per (item, list, UOM); either way a re-run must update
		# what the first run created and create nothing new.
		self.assertGreater(first["created"], 0)
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["updated"], first["created"])

		rows = frappe.get_all(
			"Item Price",
			filters={"item_code": self.items[0].item_code, "price_list": self.price_list},
			fields=["custom_length"],
		)
		self.assertEqual(len(rows), len({r.custom_length for r in rows}))

	def test_seeded_stock_reaches_the_offer_builder_once_enabled(self):
		"""Seed -> Enable -> offer, which is the whole operator flow.

		Seeding alone must offer nothing: the shelf is not a source of offers, only
		of candidates to enable. Enabling at a qty BELOW the shelf total also pins
		that the offered number is the enabled one.
		"""
		import json

		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
			get_enabled_offer_items,
		)
		from ecommerce_integration.ecommerce_integration.utils.stock_picker import set_enabled_stock

		item_code = self.items[0].item_code
		master, _ = simulate.ensure_stem_length_master(self.lengths)
		simulate.seed_shelf_stock(self.items, self.lengths, master=master, qty_per_length=250, warehouse=None)

		# Nothing enabled yet -> nothing offered, however much is on the shelf.
		self.assertEqual([o for o in get_enabled_offer_items() if o["item_code"] == item_code], [])

		with patch("frappe.db.commit"):
			set_enabled_stock(
				json.dumps(
					[{"item_code": item_code, "stem_length": length, "qty": 100} for length in self.lengths]
				),
				1,
			)

		offers = [o for o in get_enabled_offer_items() if o["item_code"] == item_code]
		self.assertEqual(len(offers), len(self.lengths))
		for offer in offers:
			self.assertEqual(offer["actual_qty"], 100)
			self.assertGreater(offer["price_per_stem"], 0)


class TestSeedingIsLocal(IntegrationTestCase):
	def test_seeding_never_calls_floriday_or_biflorica(self):
		if not has("Stem Length", "Shelf", "Shelf Item"):
			self.skipTest("the post-harvest Stem Length / Shelf doctypes are not installed")

		ensure_item("_Test EI Local Seed Rose")

		# simulate() commits so a long seeding run cannot lose work half way
		# through. Inside a test that would escape the rollback, so it is a no-op
		# here — what is under test is the absence of HTTP, not the commit.
		with (
			patch("requests.post") as post,
			patch("requests.get") as get,
			patch("frappe.db.commit"),
		):
			try:
				simulate.simulate(item_group_like="All Item Groups", limit=1, qty_per_length=10)
			finally:
				for name in frappe.get_all(
					"Shelf", filters={"name": ["like", f"{simulate.SIM_SHELF_PREFIX}%"]}, pluck="name"
				):
					frappe.delete_doc("Shelf", name, ignore_permissions=True, force=True)

		post.assert_not_called()
		get.assert_not_called()
