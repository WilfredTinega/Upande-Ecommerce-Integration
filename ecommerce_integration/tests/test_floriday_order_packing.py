# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""An imported Floriday line has to carry what upande_packhouse requires.

upande_packhouse blocks a Roses Sales Order line with no box type, packrate or
number of boxes. Floriday names only a VBN package code and pieces per package,
so the box type comes from the Floriday Settings map, and the bunch size from
the sales UOM's own name when the item has no UOM Conversion row for it.
"""

from unittest.mock import patch

from frappe import _dict

from ecommerce_integration.ecommerce_integration.doctype.floriday_settings import floriday_sales_order
from ecommerce_integration.testing import IntegrationTestCase


def _settings(rows=(), default=None):
	return _dict(
		box_type_map=[_dict(vbn_package_code=code, box_type=box) for code, box in rows],
		default_box_type=default,
	)


class TestFloridayBoxType(IntegrationTestCase):
	def box_type(self, settings, code):
		with patch.object(floriday_sales_order.frappe, "get_cached_doc", return_value=settings):
			return floriday_sales_order._floriday_box_type(code)

	def test_mapped_vbn_code_wins_over_the_default(self):
		settings = _settings([(884, "Flower Pack Pro"), (885, "XPOL BOX")], default="Standard")
		self.assertEqual(self.box_type(settings, 885), "XPOL BOX")

	def test_unmapped_code_falls_back_to_the_default(self):
		self.assertEqual(self.box_type(_settings([(884, "Flower Pack Pro")], "Standard"), 999), "Standard")

	def test_no_map_and_no_default_raises(self):
		with self.assertRaises(Exception):
			self.box_type(_settings(), 884)


class TestSalesUomFactor(IntegrationTestCase):
	def factor(self, sales_uom, rows=()):
		item = _dict(
			sales_uom=sales_uom,
			stock_uom="Stems",
			uoms=[_dict(uom=uom, conversion_factor=f) for uom, f in rows],
		)
		with patch.object(floriday_sales_order.frappe, "get_cached_doc", return_value=item):
			return floriday_sales_order.get_item_sales_uom_and_factor("Madam Red")

	def test_bunch_size_comes_from_the_uom_name_without_a_conversion_row(self):
		self.assertEqual(self.factor("Bunch (10)", [("Stems", 1)]), ("Bunch (10)", 10))

	def test_no_sales_uom_sells_in_stems(self):
		self.assertEqual(self.factor(None), ("Stems", 1))

	def test_uom_without_a_count_uses_the_items_row(self):
		self.assertEqual(self.factor("Box", [("Box", 20)]), ("Box", 20))

	def test_uom_without_a_count_or_a_row_raises(self):
		with self.assertRaises(Exception):
			self.factor("10")


class TestOrderNameAndConsignee(IntegrationTestCase):
	def test_order_name_is_the_buyer_and_the_day_week_year_code(self):
		from datetime import date

		self.assertEqual(
			floriday_sales_order._floriday_order_name("Juniflor SVXI", date(2026, 11, 3)),
			"JUNIFLOR SVXI 24526",
		)
		self.assertEqual(
			floriday_sales_order._floriday_order_name("Sjaak van der Vijver BV", date(2026, 11, 1)),
			"SJAAK VAN DER VIJVER BV 74426",
		)

	def test_buyer_matches_a_consignee_written_differently(self):
		key = floriday_sales_order._name_key
		self.assertEqual(key("Botanic Flower Group BV"), key("Botanic Flower Group B.V"))
		self.assertEqual(key("Sjaak van der Vijver BV"), key("Sjaak Van Der Vijver"))
		self.assertNotEqual(key("Teresa Group shpk"), key("Sjaak Van Der Vijver"))

	def test_matching_consignee_returns_the_existing_record(self):
		with (
			patch.object(floriday_sales_order.frappe.db, "exists", return_value=True),
			patch.object(
				floriday_sales_order.frappe,
				"get_all",
				return_value=["Juniflor SVXI", "Botanic Flower Group B.V"],
			),
		):
			self.assertEqual(
				floriday_sales_order._matching_consignee("Botanic Flower Group BV"),
				"Botanic Flower Group B.V",
			)
			self.assertIsNone(floriday_sales_order._matching_consignee("Jouri Box"))
