# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Shelf-based stock source (read side used by the Floriday stock refresh).

Shelf rows model plain items: `Shelf Item.variety` is the Item code,
`stem_length` is the Stem Length name (e.g. "52cm"), `stem_qty` is the count.
The `Shelf`/`Shelf Item` doctypes are owned by upande_kaitet; every read here is
guarded on their existence so this app is safe on sites without that app.

Vendored into ecommerce_integration so the integration carries no import
dependency on upande_webshop.
"""

import frappe
from frappe.utils import flt


def shelf_stock_enabled(settings_doctype):
	"""True when `settings_doctype` (a Single) has use_shelf_stock on and Shelf exists.

	Guarded by the `Shelf Item` doctype so sites without upande_kaitet are safe.
	"""
	try:
		if not frappe.get_cached_value(settings_doctype, settings_doctype, "use_shelf_stock"):
			return False
	except Exception:
		return False
	return bool(frappe.db.exists("DocType", "Shelf Item"))


def get_shelf_qty_by_length(item_code):
	"""Return {stem_length_name: total_stems} for one item across all shelves."""
	if not frappe.db.exists("DocType", "Shelf Item"):
		return {}
	rows = frappe.db.get_all(
		"Shelf Item",
		filters={"variety": item_code, "parenttype": "Shelf"},
		fields=["stem_length", "stem_qty"],
	)
	qty_by_sl = {}
	for r in rows:
		if not r.stem_length:
			continue
		qty_by_sl[r.stem_length] = qty_by_sl.get(r.stem_length, 0.0) + flt(r.stem_qty)
	return qty_by_sl
