# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Move this app's data out of upande_webshop's tables into its own.

Two things used to live in doctypes upande_webshop owns, which made both features
dead on any site without that app:

  * Floriday trade-item mappings — `Stem Length Price` rows parented to
    `Floriday Items` → now `Floriday Item Length`.
  * The enable-for-channels flags — `Webshop Item Prices` + its enabled
    `Stem Length Price` children → now `Ecommerce Enabled Stock`.

Reads are guarded and idempotent, so this is a no-op on a site that never had
upande_webshop (nothing to copy) and safe to re-run on one that did. The source
rows are left in place: upande_webshop's own storefront still reads them, and
deleting another app's data is not this patch's business.
"""

import frappe
from frappe.utils import flt


def _has_table(doctype):
	"""True when the doctype exists AND its table is really there.

	A site can carry an orphaned DocType record whose table was dropped; querying
	it would raise instead of returning nothing.
	"""
	if not frappe.db.exists("DocType", doctype):
		return False
	return bool(frappe.db.table_exists(doctype))


def _migrate_floriday_lengths():
	"""`Stem Length Price` rows under Floriday Items → `Floriday Item Length`."""
	if not (_has_table("Stem Length Price") and _has_table("Floriday Item Length")):
		return 0

	rows = frappe.db.sql(
		"""
		SELECT parent, stem_length, trade_item_id, rate, idx
		FROM `tabStem Length Price`
		WHERE parenttype = 'Floriday Items'
		""",
		as_dict=True,
	)

	created = 0
	for row in rows:
		if not row.parent or not frappe.db.exists("Floriday Items", row.parent):
			continue
		# Keyed on (parent, stem_length) so a re-run updates instead of duplicating.
		existing = frappe.db.get_value(
			"Floriday Item Length",
			{"parenttype": "Floriday Items", "parent": row.parent, "stem_length": row.stem_length},
			"name",
		)
		if existing:
			frappe.db.set_value(
				"Floriday Item Length",
				existing,
				{"trade_item_id": row.trade_item_id, "rate": flt(row.rate)},
				update_modified=False,
			)
			continue

		child = frappe.get_doc(
			{
				"doctype": "Floriday Item Length",
				"parenttype": "Floriday Items",
				"parentfield": "table_ppvq",
				"parent": row.parent,
				"idx": row.idx,
				"stem_length": row.stem_length,
				"trade_item_id": row.trade_item_id,
				"rate": flt(row.rate),
			}
		)
		child.insert(ignore_permissions=True)
		created += 1

	return created


def _migrate_enabled_stock():
	"""Enabled `Webshop Item Prices` lengths → `Ecommerce Enabled Stock`."""
	if not (
		_has_table("Stem Length Price")
		and _has_table("Webshop Item Prices")
		and _has_table("Ecommerce Enabled Stock")
	):
		return 0

	rows = frappe.db.sql(
		"""
		SELECT wip.item_code, slp.stem_length, slp.stock_qty, slp.enabled
		FROM `tabStem Length Price` slp
		JOIN `tabWebshop Item Prices` wip ON wip.name = slp.parent
		WHERE slp.parenttype = 'Webshop Item Prices'
		  AND IFNULL(wip.item_code, '') != ''
		  AND IFNULL(slp.stem_length, '') != ''
		""",
		as_dict=True,
	)

	created = 0
	for row in rows:
		if not frappe.db.exists("Item", row.item_code):
			continue
		existing = frappe.db.get_value(
			"Ecommerce Enabled Stock",
			{"item_code": row.item_code, "stem_length": row.stem_length},
			"name",
		)
		if existing:
			frappe.db.set_value(
				"Ecommerce Enabled Stock",
				existing,
				{"stock_qty": flt(row.stock_qty), "enabled": int(row.enabled or 0)},
				update_modified=False,
			)
			continue

		frappe.get_doc(
			{
				"doctype": "Ecommerce Enabled Stock",
				"item_code": row.item_code,
				"stem_length": row.stem_length,
				"stock_qty": flt(row.stock_qty),
				"enabled": int(row.enabled or 0),
			}
		).insert(ignore_permissions=True)
		created += 1

	return created


def execute():
	lengths = _migrate_floriday_lengths()
	enabled = _migrate_enabled_stock()
	if lengths or enabled:
		print(
			f"migrate_webshop_tables_to_app_doctypes: "
			f"{lengths} Floriday Item Length row(s), {enabled} Ecommerce Enabled Stock row(s)"
		)
