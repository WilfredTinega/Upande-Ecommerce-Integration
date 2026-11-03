# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Clear the Shopify connector off a site that never used it.

It now ships in the shopify_integration app. A site with Shopify data has that
app installed by `setup.shopify_split.before_migrate`; on a site without any,
this drops what the old module left behind: the DocTypes and their tables, the
onboarding steps, the Order Pick List link field and the scheduled jobs.
"""

import frappe

from ecommerce_integration.setup.shopify_split import (
	OLD_MODULE,
	has_shopify_data,
	shopify_doctypes_left_here,
)


def execute():
	doctypes = shopify_doctypes_left_here()
	if not doctypes or has_shopify_data():
		return

	for name in frappe.get_all(
		"Scheduled Job Type",
		filters={"method": ["like", "ecommerce_integration.ecommerce_integration.doctype.shopify_%"]},
		pluck="name",
	):
		frappe.delete_doc("Scheduled Job Type", name, ignore_permissions=True, force=True)

	field = frappe.db.exists(
		"Custom Field", {"dt": "Order Pick List", "fieldname": "custom_shopify_allocation"}
	)
	if field:
		frappe.delete_doc("Custom Field", field, ignore_permissions=True, force=True)

	for name in frappe.get_all("Onboarding Step", filters={"name": ["like", "%Shopify%"]}, pluck="name"):
		frappe.delete_doc("Onboarding Step", name, ignore_permissions=True, force=True)

	for doctype in doctypes:
		frappe.delete_doc("DocType", doctype, ignore_permissions=True, force=True, ignore_missing=True)
		if frappe.db.table_exists(doctype):
			# nosemgrep: frappe-sql-format-injection
			frappe.db.sql_ddl(f"DROP TABLE `tab{doctype}`")

	frappe.db.delete("Singles", {"doctype": "Shopify Settings"})
	frappe.db.delete("Logs To Clear", {"ref_doctype": "Shopify API Error Log"})
	print(f"remove_unused_shopify_connector: removed {len(doctypes)} DocType(s) from {OLD_MODULE}")
