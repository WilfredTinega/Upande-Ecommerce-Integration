# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Server side for the inline "enable stock for the channels" picker (shelf_move.js).

The panel is rendered by this app on its own Floriday Settings / Biflorica
Setting forms, so its endpoints live here.

Enabling writes to `Ecommerce Enabled Stock`, a doctype THIS app owns, so the
feature works on any site running this app alone. It used to write upande_webshop's
`Webshop Item Prices` + `Stem Length Price` pair, which made the button a silent
no-op wherever that app was absent.

Rates are never written here. Availability is all this stores; the per-stem rate
is resolved at read time from ERPNext `Item Price` and the post-harvest
`Stem Length.price` master.

`Shelf Item` is the one doctype read that this app does not own; it is probed by
name and every reader degrades to an empty list when it is absent.
"""

import json

import frappe
from frappe.utils import flt

from ecommerce_integration.ecommerce_integration.utils import channel_setting
from ecommerce_integration.ecommerce_integration.utils.enabled_stock import (
	ENABLED_STOCK,
	_stems_per_bunch_from_uom,
)
from ecommerce_integration.ecommerce_integration.utils.enabled_stock import (
	get_enabled_stock_rows as _get_enabled_stock_rows,
)
from ecommerce_integration.ecommerce_integration.utils.stem_length import (
	_normalize_stem_length,
)

# Owned by the post-harvest suite; present on farm sites, absent elsewhere.
SHELF_ITEM = "Shelf Item"


def _has_doctype(name):
	return bool(frappe.db.exists("DocType", name))


def _bunch_size(sales_uom, stock_uom):
	"""Stems-per-bunch step for an item, parsed from its sales UOM name.

	"Bunch (10)" -> 10; falls back to 1 (single stems) so the picker's
	"Qty to Enable" steps in the unit the cart sells in."""
	size = _stems_per_bunch_from_uom(sales_uom or stock_uom)
	return size if size and size > 0 else 1


def _canon_length(value):
	"""Canonical "<n>cm" stem length, or "" when `value` names no length.

	Keys built with this line up with the `Ecommerce Enabled Stock` rows.
	Goes through the post-harvest master first: on farms where `Shelf
	Item.stem_length` Links to `Stem Length`, the stored value is a docname and
	may carry no digits at all."""
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		canonical_stem_length,
	)

	if value is None:
		return ""
	return canonical_stem_length(value) or ""


@frappe.whitelist()
def get_shelf_rows():
	"""Every (shelf, variety, stem length) currently on a Shelf with positive qty.

	Returns {shelf, item_code, item_name, stem_length, shelf_qty, bunch_size},
	one row per distinct combination, summed across the FIFO Shelf Item rows.
	Empty list when the Shelf Item doctype isn't on this site."""
	if not _has_doctype(SHELF_ITEM):
		return []

	rows = frappe.db.sql(
		"""
		SELECT si.parent AS shelf, si.variety AS item_code, i.item_name,
		       i.sales_uom, i.stock_uom,
		       si.stem_length, SUM(si.stem_qty) AS shelf_qty
		FROM `tabShelf Item` si
		JOIN `tabItem` i ON i.name = si.variety
		WHERE si.parenttype = 'Shelf' AND si.stem_qty > 0
		GROUP BY si.parent, si.variety, si.stem_length
		HAVING shelf_qty > 0
		ORDER BY si.parent, i.item_name, si.stem_length
		""",
		as_dict=True,
	)
	for r in rows:
		r["shelf_qty"] = int(flt(r.get("shelf_qty")))
		r["bunch_size"] = _bunch_size(r.get("sales_uom"), r.get("stock_uom"))
	return rows


def _configured_warehouses():
	"""Warehouses the "warehouse" source of the picker should read.

	Taken from this app's own channel Singles only. It used to prefer
	`Webshop Settings` → Stock Balances, but that Single belongs to
	upande_webshop, and reading it on a site without that app queued
	"DocType Webshop Settings not found" on every call."""
	names = []
	for doctype, fields in (
		("Floriday Settings", ("stock_warehouse", "warehouse")),
		("Biflorica Setting", ("warehouse", "deals_source_warehouse")),
	):
		if not _has_doctype(doctype):
			continue
		for field in fields:
			value = channel_setting(field, doctype)
			if value and value not in names:
				names.append(value)
	return names


def _leaf_map(warehouses):
	"""{leaf warehouse: name to report it under}, expanding group warehouses.

	Stock is summed per leaf but attributed to the configured (possibly group)
	warehouse, so a group row aggregates its children."""
	from erpnext.stock.doctype.warehouse.warehouse import get_child_warehouses

	name_by_leaf = {}
	for wh in warehouses:
		if not wh:
			continue
		if frappe.get_cached_value("Warehouse", wh, "is_group") == 1:
			leaves = get_child_warehouses(wh) or []
		else:
			leaves = [wh]
		for leaf in leaves:
			name_by_leaf.setdefault(leaf, wh)
	return name_by_leaf


def _bin_rows(name_by_leaf):
	"""Picker rows from Bin stock, in get_shelf_rows() shape.

	`stem_length` is "" — warehouse items are variants, so the length is encoded
	in the item code rather than carried as its own column."""
	if not name_by_leaf:
		return []

	placeholders = ",".join(["%s"] * len(name_by_leaf))
	# The f-string only interpolates `placeholders`, a locally built run of %s
	# markers; every warehouse name travels as a bound parameter below.
	# nosemgrep: frappe-sql-format-injection
	bins = frappe.db.sql(
		f"""
		SELECT b.warehouse, b.item_code, i.item_name,
		       i.sales_uom, i.stock_uom, b.actual_qty
		FROM `tabBin` b
		JOIN `tabItem` i ON i.name = b.item_code
		WHERE b.warehouse IN ({placeholders}) AND b.actual_qty > 0
		""",
		tuple(name_by_leaf.keys()),
		as_dict=True,
	)

	agg = {}
	for b in bins:
		shelf = name_by_leaf.get(b.warehouse, b.warehouse)
		key = (shelf, b.item_code)
		row = agg.get(key)
		if not row:
			row = {
				"shelf": shelf,
				"item_code": b.item_code,
				"item_name": b.item_name or b.item_code,
				"stem_length": "",
				"shelf_qty": 0,
				"bunch_size": _bunch_size(b.get("sales_uom"), b.get("stock_uom")),
			}
			agg[key] = row
		row["shelf_qty"] += int(flt(b.actual_qty))

	rows = [r for r in agg.values() if r["shelf_qty"] > 0]
	rows.sort(key=lambda r: (r["shelf"], r["item_name"]))
	return rows


@frappe.whitelist()
def get_warehouse_rows():
	"""get_shelf_rows() shape, sourced from the configured warehouses' Bin stock."""
	return _bin_rows(_leaf_map(_configured_warehouses()))


@frappe.whitelist()
def get_customer_warehouse_rows(warehouse: str):
	"""get_warehouse_rows() scoped to a single warehouse (Customer Settings tab)."""
	if not warehouse:
		return []
	return _bin_rows(_leaf_map([warehouse]))


@frappe.whitelist()
def get_enabled_stock_rows():
	"""Currently-enabled (item, length, qty) rows shown alongside the picker."""
	return _get_enabled_stock_rows()


def available_qty_by_key():
	"""{(item_code, canonical_length): available_qty} across both sources.

	Reads shelf AND warehouse rows so the publish cap holds whichever source the
	panel is showing; an item present in both is summed."""
	out = {}
	for r in get_shelf_rows() + get_warehouse_rows():
		key = (r.get("item_code"), _canon_length(r.get("stem_length")))
		out[key] = out.get(key, 0.0) + flt(r.get("shelf_qty"))
	return out


def _enabled_stock_name(item_code, stem_length):
	"""The `Ecommerce Enabled Stock` row for one (item, length), or None.

	Matched on the canonical "<n>cm" form so "52CM"/"52 cm"/"52cm" collapse to
	one row, the same way rates and stock are keyed everywhere else.
	"""
	canon = _canon_length(stem_length)
	for row in frappe.get_all(
		ENABLED_STOCK, filters={"item_code": item_code}, fields=["name", "stem_length"]
	):
		if _canon_length(row.stem_length) == canon:
			return row.name
	return None


@frappe.whitelist()
def set_enabled_stock(
	items: str | list,
	enabled: str | int | bool = 1,
	source_warehouse: str | None = None,
):
	"""Enable (or disable) per-length stock for the sales channels. No stock move.

	`items`: JSON list of {item_code, stem_length, qty}. Each entry gets an
	`Ecommerce Enabled Stock` row (created on first use), its `enabled` flag is
	set, and when enabling `stock_qty` is set to `qty`.

	The qty is CAPPED at what is actually available for that (item, length) — the
	server-side guard behind the panel's per-row max, so a stale page or a direct
	API call cannot over-offer. `source_warehouse` adds that warehouse's Bin stock
	to the cap, for items that live only there.

	Returns {updated, items, capped}. No rate is written: the per-stem price is
	resolved at read time from `Item Price` and the post-harvest `Stem Length`
	master.
	"""
	if isinstance(items, str):
		items = json.loads(items or "[]")
	enabled = 1 if str(enabled) not in ("0", "false", "False", "", "no") else 0

	# Availability is only needed when enabling — a disable is never capped.
	avail = available_qty_by_key() if enabled else {}
	if enabled and source_warehouse:
		for r in get_customer_warehouse_rows(source_warehouse):
			key = (r.get("item_code"), _canon_length(r.get("stem_length")))
			avail[key] = avail.get(key, 0.0) + flt(r.get("shelf_qty"))

	updated = 0
	capped = 0
	touched_items = []
	for entry in items or []:
		item_code = (entry.get("item_code") or "").strip()
		stem_length = (entry.get("stem_length") or "").strip()
		if not (item_code and stem_length):
			continue
		if not frappe.db.exists("Item", item_code):
			continue

		qty = flt(entry.get("qty"))
		if enabled:
			available = flt(avail.get((item_code, _canon_length(stem_length))))
			if qty > available:
				qty = available
				capped += 1

		canon = _canon_length(stem_length) or stem_length
		name = _enabled_stock_name(item_code, stem_length)
		if name:
			doc = frappe.get_doc(ENABLED_STOCK, name)
		else:
			doc = frappe.get_doc({"doctype": ENABLED_STOCK, "item_code": item_code, "stem_length": canon})

		doc.enabled = enabled
		if enabled:
			doc.stock_qty = qty
		doc.save(ignore_permissions=True)

		updated += 1
		if item_code not in touched_items:
			touched_items.append(item_code)

	# Each row was saved in its own iteration above; commit so a failure on a
	# later row cannot roll back what is already enabled.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"updated": updated, "items": touched_items, "capped": capped}
