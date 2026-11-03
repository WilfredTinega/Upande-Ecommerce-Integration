# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Drop Sales Order.custom_floriday_delivery_id; the GLN lives on the Delivery Point.

A delivery GLN names a delivery location, which is what a Delivery Point is, and
`Delivery Point.custom_floriday_delivery_point_id` already holds it. The copy on
every Sales Order duplicated that. Before the field goes, a GLN an order carries
is written onto its Delivery Point, but only where that Delivery Point has no GLN
yet and no other Delivery Point holds this one. Idempotent.
"""

import re

import frappe

FIELD = "custom_floriday_delivery_id"
DP_FIELD = "custom_floriday_delivery_point_id"
GLN = re.compile(r"\d{13}")


def execute():
	for doctype in ("Sales Order", "Quotation"):
		_move_and_drop(doctype)


def _move_and_drop(doctype):
	custom_field = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": FIELD})
	has_column = frappe.db.has_column(doctype, FIELD)
	if not (custom_field or has_column):
		return

	moved = 0
	if (
		has_column
		and frappe.db.has_column(doctype, "custom_delivery_point")
		and frappe.db.table_exists("Delivery Point")
		and frappe.db.has_column("Delivery Point", DP_FIELD)
	):
		# Both column names are module constants and `doctype` is one of two
		# literals above, never user input.
		# nosemgrep: frappe-sql-format-injection
		rows = frappe.db.sql(
			f"""SELECT DISTINCT custom_delivery_point AS delivery_point, `{FIELD}` AS gln
			    FROM `tab{doctype}`
			    WHERE IFNULL(custom_delivery_point, '') != '' AND IFNULL(`{FIELD}`, '') != ''""",
			as_dict=True,
		)
		for row in rows:
			gln = (row.gln or "").strip()
			if not GLN.fullmatch(gln) or not frappe.db.exists("Delivery Point", row.delivery_point):
				continue
			if frappe.db.get_value("Delivery Point", row.delivery_point, DP_FIELD):
				continue
			if frappe.db.exists("Delivery Point", {DP_FIELD: gln}):
				continue
			frappe.db.set_value("Delivery Point", row.delivery_point, DP_FIELD, gln, update_modified=False)
			moved += 1

	# Fields placed after this one move up to its own place.
	for name in frappe.get_all("Custom Field", filters={"dt": doctype, "insert_after": FIELD}, pluck="name"):
		frappe.db.set_value("Custom Field", name, "insert_after", "custom_delivery_point")

	if custom_field:
		frappe.delete_doc("Custom Field", custom_field, ignore_permissions=True, force=True)

	# Deleting the Custom Field leaves the column; drop it (sql_ddl commits the
	# writes above first, as MariaDB's implicit DDL commit requires).
	if frappe.db.has_column(doctype, FIELD):
		# nosemgrep: frappe-sql-format-injection
		frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `{FIELD}`")

	frappe.clear_cache(doctype=doctype)
	print(
		f"move_sales_order_gln_to_delivery_point: {doctype}: tagged {moved} Delivery Point(s), dropped {FIELD}"
	)
