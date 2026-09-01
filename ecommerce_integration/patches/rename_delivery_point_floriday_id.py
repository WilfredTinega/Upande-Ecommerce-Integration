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
	old_column = frappe.db.has_column("Delivery Point", OLD)
	if not (old_field or old_column):
		return  # already renamed, or never created here

	# Each step below is guarded on its own state rather than on `old_field`, so a
	# re-run after a partial failure finishes the job. The first version bailed
	# out when the Custom Field was gone — which is exactly where a failed column
	# drop leaves the site, stranding the orphan column permanently.

	# Make sure the new field exists before anything is copied into it.
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
		ensure_floriday_custom_fields,
	)

	ensure_floriday_custom_fields()

	moved = 0
	if old_column and frappe.db.has_column("Delivery Point", NEW):
		# Carry the GLN mappings over. Only fill blanks, so a value already
		# written under the new name is never clobbered by a stale one.
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

	if old_field:
		frappe.delete_doc("Custom Field", old_field, ignore_permissions=True, force=True)

	# Deleting the Custom Field leaves the COLUMN behind — Frappe never drops it,
	# and migrate will not either. That is exactly the orphan this rename is meant
	# to avoid, so drop it now that every value has been carried across.
	#
	# `sql_ddl`, not `sql`: MariaDB implicitly commits a DDL statement, so Frappe
	# refuses one issued after writes in the same transaction
	# (`ImplicitCommitError`). The copy above and the Custom Field deletion are
	# exactly such writes, so the drop has to go through the API that commits
	# them first — and committing before the drop is the right order anyway.
	if frappe.db.has_column("Delivery Point", OLD):
		# nosemgrep: frappe-sql-format-injection
		frappe.db.sql_ddl(f"ALTER TABLE `tabDelivery Point` DROP COLUMN `{OLD}`")

	frappe.clear_cache(doctype="Delivery Point")
	print(f"rename_delivery_point_floriday_id: moved {moved} mapping(s), dropped column {OLD}")
