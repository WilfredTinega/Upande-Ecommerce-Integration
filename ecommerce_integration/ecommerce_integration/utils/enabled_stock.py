# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Read side for the (item, stem length) rows enabled for the sales channels.

Backed by `Ecommerce Enabled Stock`, a doctype this app owns. It replaces
`webshop_stock`, which read upande_webshop's `Webshop Item Prices` +
`Stem Length Price` pair and therefore only worked on a webshop site.

Availability only. Rates come from ERPNext `Item Price` and the post-harvest
`Stem Length.price` master via `utils.stem_length.resolve_stem_length_rates`.
"""

import re

import frappe
from frappe.utils import flt

ENABLED_STOCK = "Ecommerce Enabled Stock"


def _stems_per_bunch_from_uom(uom_name):
	"""Parse stems per bunch from a UOM name like 'Bunch (10)' -> 10."""
	if uom_name:
		m = re.search(r"\((\d+)\)", uom_name)
		if m:
			return int(m.group(1))
	return 1


def get_enabled_stock_rows(enabled_only=True):
	"""Rows currently enabled for the channels, for the picker's "Qty Enabled" column.

	Returns a list of {item_code, item_name, stem_length, stock_qty, bunch_size}.
	"""
	filters = {"enabled": 1} if enabled_only else {}
	rows = frappe.get_all(
		ENABLED_STOCK,
		filters=filters,
		fields=["item_code", "item_name", "stem_length", "stock_qty"],
		order_by="item_name asc, stem_length asc",
	)
	if not rows:
		return []

	uoms = {
		i.name: (i.sales_uom or i.stock_uom)
		for i in frappe.get_all(
			"Item",
			filters={"name": ["in", list({r.item_code for r in rows})]},
			fields=["name", "sales_uom", "stock_uom"],
		)
	}

	for r in rows:
		r["stock_qty"] = flt(r.get("stock_qty"))
		size = _stems_per_bunch_from_uom(uoms.get(r.item_code))
		r["bunch_size"] = size if size and size > 0 else 1
	return rows
