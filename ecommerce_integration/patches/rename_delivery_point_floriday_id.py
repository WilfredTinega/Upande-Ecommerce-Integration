# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Rename Delivery Point.custom_floriday_delivery_id -> custom_floriday_delivery_point_id.

The field holds the Floriday GLN a Delivery Point maps to, so an order for that
GLN reuses the record instead of creating another. It was called
`custom_floriday_delivery_id`, which is also the name of a DIFFERENT field on
Sales Order (that one holds the GLN of the order itself) — two unrelated fields
sharing a name across doctypes. Only the Delivery Point one is renamed here.

Values are carried across before the old field goes, so existing GLN mappings
survive. Idempotent: safe to re-run, and a no-op once the rename has happened.
"""

import frappe

OLD = "custom_floriday_delivery_id"
NEW = "custom_floriday_delivery_point_id"


def execute():
	if not frappe.db.exists("DocType", "Delivery Point"):
		return

	old_field = frappe.db.exists("Custom Field", {"dt": "Delivery Point", "fieldname": OLD})
	if not old_field:
		return  # already renamed, or never created here

	# Make sure the new field exists before anything is copied into it.
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
		ensure_floriday_custom_fields,
	)

	ensure_floriday_custom_fields()

	if not (frappe.db.has_column("Delivery Point", OLD) and frappe.db.has_column("Delivery Point", NEW)):
		return

	# Carry the GLN mappings over. Only fill blanks, so a value already written
	# under the new name is never clobbered by a stale one.
	# Both column names are module constants, never user input.
	# nosemgrep: frappe-sql-format-injection
	moved = frappe.db.sql(
		f"""SELECT COUNT(*) FROM `tabDelivery Point`
		    WHERE IFNULL(`{OLD}`, '') != '' AND IFNULL(`{NEW}`, '') = ''"""
	)[0][0]
	if moved:
		# nosemgrep: frappe-sql-format-injection
		frappe.db.sql(
			f"""UPDATE `tabDelivery Point` SET `{NEW}` = `{OLD}`
			    WHERE IFNULL(`{OLD}`, '') != '' AND IFNULL(`{NEW}`, '') = ''"""
		)

	frappe.delete_doc("Custom Field", old_field, ignore_permissions=True, force=True)

	# Deleting the Custom Field leaves the COLUMN behind — Frappe never drops it,
	# and migrate will not either. That is exactly the orphan this rename is meant
	# to avoid, so drop it now that every value has been carried across.
	if frappe.db.has_column("Delivery Point", OLD):
		# nosemgrep: frappe-sql-format-injection
		frappe.db.sql(f"ALTER TABLE `tabDelivery Point` DROP COLUMN `{OLD}`")

	frappe.clear_cache(doctype="Delivery Point")
	print(f"rename_delivery_point_floriday_id: moved {moved} mapping(s), dropped column {OLD}")
