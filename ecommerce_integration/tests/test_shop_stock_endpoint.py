# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The allocation board's stock endpoint has to work on a deployed site.

While the board was a Web Page record on tambuzi it read `csr_shop_age`, a Server
Script that exists only there. Shipping the page in the app without the data
source made it fail on every other site with "Failed to get method for command
csr_shop_age" — the page rendered, the orders listed, and the stock column was
an error. So the rule was ported into `utils/shop_stock.py`.

The rule itself is the expensive part:

  * `Bin` is the quantity, because it is already net of what was sold, moved or
    discarded. Summing the Stock Entries that put stock into a shop double-counts
    everything that has since left, which once read 113,147 stems against 1,000.
  * A GROUP warehouse holds no `Bin` rows and is not a legal source for the
    reservation Stock Entry, so it is excluded — and the board is told why, since
    that is a setting someone can fix.
"""

from unittest.mock import patch

import frappe

from ecommerce_integration.ecommerce_integration.utils.shop_stock import (
	COLUMNS,
	FRESH_DAYS,
	_bucket,
	_farm_of,
	aged_shop_stock,
	shop_warehouses,
)
from ecommerce_integration.testing import IntegrationTestCase

MODULE = "ecommerce_integration.ecommerce_integration.utils.shop_stock"


class TestAgeBuckets(IntegrationTestCase):
	def test_fresh_stock_is_not_shop_stock_yet(self):
		"""Day 0-3 is excluded, matching Available for Sale > Shop."""
		for d in range(0, FRESH_DAYS + 1):
			with self.subTest(days=d):
				self.assertIsNone(_bucket(d))

	def test_each_day_lands_in_its_own_column(self):
		self.assertEqual(_bucket(4), "d4")
		self.assertEqual(_bucket(5), "d5")
		self.assertEqual(_bucket(6), "d6")
		self.assertEqual(_bucket(7), "d7")
		self.assertEqual(_bucket(40), "d7")

	def test_unknown_age_counts_as_oldest_rather_than_vanishing(self):
		"""A holding with no harvest batch is still stock; hiding it is worse."""
		self.assertEqual(_bucket(None), "d7")

	def test_a_negative_age_is_treated_as_today(self):
		"""A harvest date in the future is bad data, not a reason to crash."""
		self.assertIsNone(_bucket(-5))


class TestFarmName(IntegrationTestCase):
	def test_the_farm_is_read_off_the_warehouse_name(self):
		self.assertEqual(_farm_of("Burguret Shop Available for Sale - TL"), "Burguret")
		self.assertEqual(_farm_of("Pendekeza Shop Available for Sale - TL"), "Pendekeza")

	def test_an_unnamed_warehouse_does_not_break_it(self):
		self.assertEqual(_farm_of(None), "")


class TestGroupWarehouseIsRefusedWithAReason(IntegrationTestCase):
	"""The case that actually bites on a fresh site."""

	def test_a_group_source_warehouse_is_not_offered(self):
		with patch(f"{MODULE}._configured_source", return_value=("All Warehouses - T", True)):
			with patch(f"{MODULE}.frappe.get_all", return_value=[]):
				self.assertEqual(shop_warehouses(), [])

	def test_and_the_board_is_told_why(self):
		with (
			patch(f"{MODULE}._configured_source", return_value=("All Warehouses - T", True)),
			patch(f"{MODULE}.shop_warehouses", return_value=[]),
		):
			out = aged_shop_stock()
		self.assertEqual(out["result"], [])
		self.assertIn("GROUP warehouse", out["reason"])
		self.assertIn("All Warehouses - T", out["reason"])

	def test_a_leaf_source_warehouse_is_included(self):
		with (
			patch(f"{MODULE}._configured_source", return_value=("Burguret Shop AFS - TL", False)),
			patch(f"{MODULE}.frappe.get_all", return_value=[]),
		):
			self.assertEqual(shop_warehouses(), ["Burguret Shop AFS - TL"])

	def test_no_shop_warehouse_at_all_says_so(self):
		with (
			patch(f"{MODULE}._configured_source", return_value=(None, False)),
			patch(f"{MODULE}.shop_warehouses", return_value=[]),
		):
			out = aged_shop_stock()
		self.assertIn("no farm shop warehouse", out["reason"])


class TestQuantityComesFromBin(IntegrationTestCase):
	def test_rows_are_built_from_bin_and_bucketed_by_age(self):
		bins = [
			frappe._dict(
				{
					"warehouse": "Burguret Shop Available for Sale - TL",
					"item_code": "Gelatto",
					"actual_qty": 150,
				}
			),
			frappe._dict(
				{"warehouse": "Burguret Shop Available for Sale - TL", "item_code": "Femke", "actual_qty": 36}
			),
		]
		ages = {
			("Gelatto", "Burguret Shop Available for Sale - TL"): 4,
			("Femke", "Burguret Shop Available for Sale - TL"): 7,
		}
		with (
			patch(f"{MODULE}.shop_warehouses", return_value=["Burguret Shop Available for Sale - TL"]),
			patch(f"{MODULE}.frappe.get_all", return_value=bins),
			patch(f"{MODULE}._age_by_item_and_warehouse", return_value=ages),
		):
			out = aged_shop_stock()

		rows = {r["variety"]: r for r in out["result"]}
		self.assertEqual(rows["Gelatto"]["d4"], 150)
		self.assertEqual(rows["Gelatto"]["total"], 150)
		self.assertEqual(rows["Femke"]["d7"], 36)
		self.assertEqual(rows["Femke"]["total"], 36)
		self.assertEqual(rows["Gelatto"]["farm"], "Burguret")

	def test_stock_still_too_fresh_is_left_out(self):
		bins = [
			frappe._dict(
				{
					"warehouse": "Burguret Shop Available for Sale - TL",
					"item_code": "Gelatto",
					"actual_qty": 150,
				}
			)
		]
		with (
			patch(f"{MODULE}.shop_warehouses", return_value=["Burguret Shop Available for Sale - TL"]),
			patch(f"{MODULE}.frappe.get_all", return_value=bins),
			patch(
				f"{MODULE}._age_by_item_and_warehouse",
				return_value={("Gelatto", "Burguret Shop Available for Sale - TL"): 1},
			),
		):
			out = aged_shop_stock()
		self.assertEqual(out["result"], [])
		self.assertIn("days old or newer", out["reason"])

	def test_the_shape_the_board_reads_is_stable(self):
		"""The page maps result rows straight onto its columns."""
		fields = {c["fieldname"] for c in COLUMNS}
		self.assertEqual(fields, {"variety", "farm", "warehouse", "d4", "d5", "d6", "d7", "total"})


class TestItRunsOnThisSite(IntegrationTestCase):
	def test_the_endpoint_answers_without_the_live_server_script(self):
		"""The whole point: no dependency on `csr_shop_age`."""
		out = aged_shop_stock()
		self.assertIn("columns", out)
		self.assertIsInstance(out["result"], list)
		# An empty answer must always explain itself.
		if not out["result"]:
			self.assertTrue(out.get("reason"))
