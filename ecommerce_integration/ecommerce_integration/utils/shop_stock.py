# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Aged farm-shop stock, for the Shopify allocation board.

The board first read this from `csr_shop_age`, a Server Script that exists only
on the live Tambuzi site. That was fine while the page itself was a Web Page
record on that site; now the page ships with the app, a site-local script is the
one dependency that would keep it working nowhere else.

The rule is the one that script established, and it is the important part:

  * **`Bin` is the source of truth for quantity.** It is already net of what was
    sold, moved or discarded. Summing the Stock Entries that put stock into a
    shop double-counts everything that has since left — that mistake once read
    113,147 stems against a real 1,000.
  * **Age comes from the latest Stock Entry that put the item into that shop**,
    via the harvest batch stamped on it, date only.
  * Shop stock is *aged* stock: anything three days old or newer is excluded, so
    the figures match the Available for Sale > Shop tab.
"""

import frappe
from frappe.utils import cint, flt, getdate, nowdate

# Shop warehouses are named "<Farm> Shop Available for Sale - <abbr>" on the
# Tambuzi build. Matched by pattern rather than hardcoded, so a new farm shop is
# picked up without a code change.
SHOP_PATTERN = "%Shop Available for Sale%"

COLUMNS = [
	{"label": "Variety", "fieldtype": "Link", "fieldname": "variety", "options": "Item"},
	{"label": "Farm", "fieldtype": "Data", "fieldname": "farm"},
	{"label": "Warehouse", "fieldtype": "Link", "fieldname": "warehouse", "options": "Warehouse"},
	{"label": "Day 4", "fieldtype": "Int", "fieldname": "d4"},
	{"label": "Day 5", "fieldtype": "Int", "fieldname": "d5"},
	{"label": "Day 6", "fieldtype": "Int", "fieldname": "d6"},
	{"label": "Day 7+", "fieldtype": "Int", "fieldname": "d7"},
	{"label": "Total", "fieldtype": "Int", "fieldname": "total"},
]

# Stock younger than this is not shop stock yet.
FRESH_DAYS = 3


def _configured_source():
	"""(warehouse, is_group) from Shopify Settings, or (None, False)."""
	if not frappe.db.exists("DocType", "Shopify Settings"):
		return None, False
	name = frappe.db.get_single_value("Shopify Settings", "default_source_warehouse")
	if not name or not frappe.db.exists("Warehouse", name):
		return None, False
	return name, bool(frappe.db.get_value("Warehouse", name, "is_group"))


def shop_warehouses():
	"""Every farm-shop warehouse, plus whatever Shopify Settings sells out of.

	The configured source warehouse is included even when it is not named to the
	pattern: it is by definition the one the connector allocates from, and a board
	that silently omitted it would be worse than useless.

	A GROUP warehouse is not included. It holds no `Bin` rows of its own — stock
	sits on the leaves — so including it would add a warehouse that can only ever
	read as empty. It is also not a legal source for the reservation Stock Entry,
	so an allocation against it could not be submitted anyway.
	"""
	names = frappe.get_all(
		"Warehouse",
		filters={"name": ["like", SHOP_PATTERN], "is_group": 0},
		pluck="name",
	)
	configured, is_group = _configured_source()
	if configured and not is_group and configured not in names:
		names.append(configured)
	return sorted(names)


def _farm_of(warehouse):
	return str(warehouse or "").replace(" Shop Available for Sale", "").rsplit(" - ", 1)[0].strip()


def _age_by_item_and_warehouse(warehouses, items, to_date):
	"""{(item, warehouse): days old} from the latest entry that stocked it there.

	Read from the harvest batch reference the Tambuzi packing chain stamps on the
	Stock Entry. Guarded: a site without that field gets an empty map and every
	row lands in the oldest bucket, which is the safe end to be wrong at.
	"""
	if not frappe.db.has_column("Stock Entry", "custom_harvest_batch_no"):
		return {}

	rows = frappe.db.sql(
		"""
		SELECT x.item_code AS item_code, x.t_warehouse AS warehouse, x.days_ago AS days_ago
		FROM (
			SELECT sed.item_code AS item_code, sed.t_warehouse AS t_warehouse,
			       DATEDIFF(%(to_date)s,
			                DATE(LEFT(SUBSTRING_INDEX(se.custom_harvest_batch_no, '/', -1), 10))
			       ) AS days_ago,
			       ROW_NUMBER() OVER (
			           PARTITION BY sed.item_code, sed.t_warehouse
			           ORDER BY se.posting_date DESC, se.posting_time DESC
			       ) AS rn
			FROM `tabStock Entry` se
			INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
			WHERE se.docstatus = 1
			  AND se.custom_harvest_batch_no IS NOT NULL
			  AND se.posting_date >= %(since)s
			  AND sed.t_warehouse IN %(warehouses)s
			  AND sed.item_code IN %(items)s
		) x
		WHERE x.rn = 1
		""",
		{
			"to_date": to_date,
			"since": frappe.utils.add_days(to_date, -60),
			"warehouses": warehouses,
			"items": items,
		},
		as_dict=True,
	)
	return {(r.item_code, r.warehouse): r.days_ago for r in rows}


def _lengths_by_item_and_warehouse(warehouses, items, to_date):
	"""{(item, warehouse): ["53CM", "63CM", ...]} - the lengths actually stocked there.

	Stem length is a HEADER attribute of the Stock Entry that moved the stems in,
	not a Bin dimension, so this says which lengths a variety has been graded to
	in that shop - NOT how many stems of each are left. Bin cannot answer that,
	and pretending otherwise would put a quantity on screen that no ledger backs.

	Most recently stocked first, because that is the one to offer by default.
	Guarded: a site without the field gets an empty map and the board simply
	offers every Stem Length on record.
	"""
	if not frappe.db.has_column("Stock Entry", "custom_stem_length"):
		return {}

	rows = frappe.db.sql(
		"""
		SELECT sed.item_code AS item_code, sed.t_warehouse AS warehouse,
		       se.custom_stem_length AS stem_length,
		       MAX(se.posting_date) AS last_in
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1
		  AND se.custom_stem_length IS NOT NULL
		  AND se.custom_stem_length != ''
		  AND se.posting_date >= %(since)s
		  AND sed.t_warehouse IN %(warehouses)s
		  AND sed.item_code IN %(items)s
		GROUP BY sed.item_code, sed.t_warehouse, se.custom_stem_length
		ORDER BY last_in DESC
		""",
		{
			"since": frappe.utils.add_days(to_date, -60),
			"warehouses": warehouses,
			"items": items,
		},
		as_dict=True,
	)
	out = {}
	for r in rows:
		out.setdefault((r.item_code, r.warehouse), []).append(r.stem_length)
	return out


def _bucket(days_ago):
	"""Which age column a holding belongs in, or None when it is still too fresh."""
	if days_ago is None:
		return "d7"  # age unknown: treat as oldest rather than hiding it
	d = max(cint(days_ago), 0)
	if d <= FRESH_DAYS:
		return None
	if d == 4:
		return "d4"
	if d == 5:
		return "d5"
	if d == 6:
		return "d6"
	return "d7"


@frappe.whitelist()
def aged_shop_stock(to_date: str | None = None):
	"""{columns, result, reason} for the allocation board.

	`reason` is set when the result is empty for a knowable cause, so the board
	can say why rather than showing a bare empty table.
	"""
	as_of = getdate(to_date or nowdate())

	warehouses = shop_warehouses()
	if not warehouses:
		configured, is_group = _configured_source()
		if is_group:
			reason = (
				f"Shopify Settings allocates from {configured}, which is a GROUP warehouse. "
				"A group holds no stock of its own and cannot be the source of a reservation "
				"Stock Entry. Point it at a leaf warehouse."
			)
		else:
			reason = "This site has no farm shop warehouse, and Shopify Settings names none."
		return {"columns": COLUMNS, "result": [], "reason": reason}

	bins = frappe.get_all(
		"Bin",
		filters={"warehouse": ["in", warehouses], "actual_qty": [">", 0]},
		fields=["warehouse", "item_code", "actual_qty"],
	)
	if not bins:
		return {
			"columns": COLUMNS,
			"result": [],
			"reason": f"No stock on hand in {len(warehouses)} farm shop warehouse(s).",
		}

	items = sorted({b.item_code for b in bins if b.item_code})
	ages = _age_by_item_and_warehouse(warehouses, items, as_of)
	lengths = _lengths_by_item_and_warehouse(warehouses, items, as_of)

	rows = {}
	for b in bins:
		qty = flt(b.actual_qty)
		if qty <= 0:
			continue
		bucket = _bucket(ages.get((b.item_code, b.warehouse)))
		if bucket is None:
			continue  # still fresh, so not shop stock yet
		key = (b.item_code, b.warehouse)
		row = rows.get(key)
		if row is None:
			row = rows[key] = {
				"variety": b.item_code,
				"farm": _farm_of(b.warehouse),
				"warehouse": b.warehouse,
				"d4": 0,
				"d5": 0,
				"d6": 0,
				"d7": 0,
				"total": 0,
				# Which lengths this variety is graded to here, so the allocation
				# can be packed to one of them. Not a per-length quantity.
				"lengths": lengths.get(key, []),
			}
		row[bucket] += qty
		row["total"] += qty

	result = sorted(rows.values(), key=lambda r: (str(r["variety"]), str(r["warehouse"])))
	out = {"columns": COLUMNS, "result": result}
	if not result:
		out["reason"] = (
			f"Every holding in {len(warehouses)} farm shop warehouse(s) is "
			f"{FRESH_DAYS} days old or newer, so none of it counts as shop stock yet."
		)
	return out
