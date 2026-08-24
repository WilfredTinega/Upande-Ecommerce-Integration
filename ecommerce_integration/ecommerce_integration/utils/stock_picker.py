# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Server side for the inline "enable stock → webshop" picker (shelf_move.js).

The panel is rendered by this app on its own Floriday Settings / Biflorica
Setting forms, so its endpoints live here. They were previously called on
upande_webshop, which made the Stock tab dead ("App upande_webshop is not
installed") on any site without that app.

Nothing here imports upande_webshop. The doctypes it owns — Shelf Item,
Webshop Settings, Webshop Item Prices, Stem Length Price — are probed by name
and every reader degrades to an empty list when they're absent, mirroring
`webshop_stock.get_webshop_enabled_rows`. Publishing is the one action that
genuinely needs those tables; without them it reports itself unavailable
instead of raising.
"""

import json
import re

import frappe
from frappe.utils import flt

from ecommerce_integration.ecommerce_integration.utils.stem_length import (
	_normalize_stem_length,
)
from ecommerce_integration.ecommerce_integration.utils.webshop_stock import (
	_stems_per_bunch_from_uom,
)
from ecommerce_integration.ecommerce_integration.utils.webshop_stock import (
	get_webshop_enabled_rows as _get_webshop_enabled_rows,
)

# Doctypes owned by upande_webshop. Present on webshop sites, absent elsewhere.
SHELF_ITEM = "Shelf Item"
WEBSHOP_SETTINGS = "Webshop Settings"
WEBSHOP_ITEM_PRICES = "Webshop Item Prices"
STEM_LENGTH_PRICE = "Stem Length Price"


def _has_doctype(name):
	return bool(frappe.db.exists("DocType", name))


def _bunch_size(sales_uom, stock_uom):
	"""Stems-per-bunch step for an item, parsed from its sales UOM name.

	"Bunch (10)" -> 10; falls back to 1 (single stems) so the picker's
	"Qty to Enable" steps in the unit the cart sells in."""
	size = _stems_per_bunch_from_uom(sales_uom or stock_uom)
	return size if size and size > 0 else 1


def _canon_length(value):
	"""Canonical "<n>cm" stem length, or "" when there's no number in `value`.

	Keys built with this line up with the published Stem Length Price rows."""
	if value is None:
		return ""
	m = re.search(r"\d+", str(value))
	return f"{int(m.group(0))}cm" if m else ""


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

	Webshop Settings → Stock Balances when that doctype exists (same list the
	storefront publishes from); otherwise this app's own settings, so the panel
	still has a source on a site with no upande_webshop."""
	if _has_doctype(WEBSHOP_SETTINGS):
		settings = frappe.get_cached_doc(WEBSHOP_SETTINGS)
		found = [row.warehouse for row in (settings.get("warehouses") or []) if row.warehouse]
		if found:
			return found

	names = []
	for doctype, fields in (
		("Floriday Settings", ("stock_warehouse", "warehouse")),
		("Biflorica Setting", ("warehouse", "deals_source_warehouse")),
	):
		if not _has_doctype(doctype):
			continue
		for field in fields:
			value = frappe.db.get_single_value(doctype, field)
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
def get_customer_warehouse_rows(warehouse):
	"""get_warehouse_rows() scoped to a single warehouse (Customer Settings tab)."""
	if not warehouse:
		return []
	return _bin_rows(_leaf_map([warehouse]))


@frappe.whitelist()
def get_webshop_enabled_rows():
	"""Currently-published (item, length, qty) rows shown alongside the picker."""
	return _get_webshop_enabled_rows()


def available_qty_by_key():
	"""{(item_code, canonical_length): available_qty} across both sources.

	Reads shelf AND warehouse rows so the publish cap holds whichever source the
	panel is showing; an item present in both is summed."""
	out = {}
	for r in get_shelf_rows() + get_warehouse_rows():
		key = (r.get("item_code"), _canon_length(r.get("stem_length")))
		out[key] = out.get(key, 0.0) + flt(r.get("shelf_qty"))
	return out


def _find_or_create_webshop_item_prices(item):
	"""The item's Webshop Item Prices doc, created (and back-filled) as needed."""
	existing = frappe.db.exists(WEBSHOP_ITEM_PRICES, {"item_code": item.item_code})
	if not existing and frappe.db.exists(WEBSHOP_ITEM_PRICES, item.item_name):
		existing = item.item_name
	if existing:
		doc = frappe.get_doc(WEBSHOP_ITEM_PRICES, existing)
		updated = False
		if not doc.item_code:
			doc.item_code = item.item_code
			updated = True
		if not doc.item_group:
			doc.item_group = item.item_group
			updated = True
		if updated:
			doc.save()
		return doc

	doc = frappe.get_doc({
		"doctype": WEBSHOP_ITEM_PRICES,
		"item_code": item.item_code,
		"item_name": item.item_name,
		"item_group": item.item_group,
	})
	doc.insert()
	return doc


def _stem_length_price_row(wip_doc, stem_length):
	"""The Stem Length Price child for `stem_length`, appended if absent.

	Matched on the canonical "<n>cm" form so "52CM"/"52 cm"/"52cm" collapse to
	one row, the same way rates and stock are stored elsewhere."""
	canon = _normalize_stem_length(stem_length) or (stem_length or "").strip()
	for row in wip_doc.stem_length_prices or []:
		if _normalize_stem_length(row.stem_length) == canon or row.stem_length == canon:
			return row
	return wip_doc.append(
		"stem_length_prices",
		{"stem_length": canon, "rate": 0, "stock_qty": 0},
	)


@frappe.whitelist()
def set_webshop_enabled_stock(items, enabled=1, source_warehouse=None):
	"""Publish (or un-publish) per-length stock to the storefront. No stock move.

	`items`: JSON list of {item_code, stem_length, qty}. Each entry's Webshop
	Item Prices doc and Stem Length Price row are found/created, the row's
	`enabled` flag is set, and (when enabling) `stock_qty` is set to `qty`.

	The published qty is CAPPED at what's actually available for that
	(item, length) — the server-side guard behind the panel's per-row max, so a
	stale page or a direct API call can't over-publish. `source_warehouse` adds
	that warehouse's Bin stock to the cap, for items that live only there.

	Returns {updated, items, capped}; `unavailable` is set instead when the
	storefront doctypes aren't installed on this site."""
	if not (_has_doctype(WEBSHOP_ITEM_PRICES) and _has_doctype(STEM_LENGTH_PRICE)):
		return {
			"updated": 0,
			"items": [],
			"capped": 0,
			"unavailable": "Webshop Item Prices is not available on this site, so stock cannot be published.",
		}

	if isinstance(items, str):
		items = json.loads(items or "[]")
	enabled = 1 if str(enabled) not in ("0", "false", "False", "", "no") else 0

	# Availability is only needed when enabling — a disable is never capped.
	avail = available_qty_by_key() if enabled else {}
	if enabled and source_warehouse:
		for r in get_customer_warehouse_rows(source_warehouse):
			key = (r.get("item_code"), _canon_length(r.get("stem_length")))
			avail[key] = avail.get(key, 0.0) + flt(r.get("shelf_qty"))

	# Group requested lengths per item so each doc is saved once.
	by_item = {}
	for entry in items or []:
		item_code = (entry.get("item_code") or "").strip()
		if not item_code:
			continue
		by_item.setdefault(item_code, []).append({
			"stem_length": (entry.get("stem_length") or "").strip(),
			"qty": flt(entry.get("qty")),
		})

	updated = 0
	capped = 0
	touched_items = []
	for item_code, lengths in by_item.items():
		item = frappe.db.get_value(
			"Item", item_code, ["name", "item_name", "item_group"], as_dict=True
		)
		if not item:
			continue
		item.item_code = item.name
		wip_doc = _find_or_create_webshop_item_prices(item)

		for entry in lengths:
			row = _stem_length_price_row(wip_doc, entry["stem_length"])
			row.enabled = enabled
			if enabled:
				qty = flt(entry["qty"])
				available = flt(avail.get((item_code, _canon_length(entry["stem_length"]))))
				if qty > available:
					qty = available
					capped += 1
				row.stock_qty = qty
			updated += 1

		wip_doc.save(ignore_permissions=True)
		touched_items.append(item_code)

	frappe.db.commit()
	return {"updated": updated, "items": touched_items, "capped": capped}
