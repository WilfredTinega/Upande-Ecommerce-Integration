# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Read side for admin-published ("enabled") webshop stock rows.

`Webshop Item Prices` and its `Stem Length Price` child are owned by
upande_webshop. This helper reads them when present and returns [] otherwise, so
the Floriday stock refresh works whether or not that app is installed. Vendored
here so this app carries no import dependency on upande_webshop.
"""

import re

import frappe
from frappe.utils import flt


def _stems_per_bunch_from_uom(uom_name):
	"""Parse stems per bunch from a UOM name like 'Bunch (10)' -> 10."""
	if uom_name:
		m = re.search(r"\((\d+)\)", uom_name)
		if m:
			return int(m.group(1))
	return 1


def get_webshop_enabled_rows():
	"""Currently-enabled (item, length, published qty) rows for the Stock panel.

	Returns a list of {item_code, item_name, stem_length, stock_qty, bunch_size}.
	Empty list when the Webshop Item Prices / Stem Length Price doctypes are not
	on the site (upande_webshop not installed)."""
	if not (
		frappe.db.exists("DocType", "Webshop Item Prices")
		and frappe.db.exists("DocType", "Stem Length Price")
	):
		return []

	rows = frappe.db.sql(
		"""
		SELECT wip.item_code, wip.item_name, slp.stem_length,
		       slp.stock_qty, i.sales_uom, i.stock_uom
		FROM `tabStem Length Price` slp
		JOIN `tabWebshop Item Prices` wip ON wip.name = slp.parent
		LEFT JOIN `tabItem` i ON i.name = wip.item_code
		WHERE slp.parenttype = 'Webshop Item Prices'
		  AND slp.enabled = 1
		ORDER BY wip.item_name, slp.stem_length
		""",
		as_dict=True,
	)
	for r in rows:
		r["stock_qty"] = flt(r.get("stock_qty"))
		size = _stems_per_bunch_from_uom(r.get("sales_uom") or r.get("stock_uom"))
		r["bunch_size"] = size if size and size > 0 else 1
	return rows
