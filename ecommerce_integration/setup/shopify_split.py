# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Hand the Shopify connector over to the shopify_integration app.

The connector's DocTypes used to ship in this app's module. Once this app stops
shipping them, `bench migrate` treats any still assigned to "Ecommerce
Integration" as orphans and deletes their DocType records, leaving the site's
orders, allocations and settings with no DocType to open them.

`before_migrate` runs ahead of that sweep:
* site with Shopify data, shopify_integration on the bench: reassign the
  DocTypes to that app's module and install it, with every row kept;
* site with Shopify data, app not on the bench: stop the migrate and say so;
* site without Shopify data: nothing to keep — the patch
  `remove_unused_shopify_connector` clears what is left.
"""

import frappe

APP = "shopify_integration"
OLD_MODULE = "Ecommerce Integration"
NEW_MODULE = "Shopify Integration"
DATA_DOCTYPES = (
	"Shopify Order",
	"Shopify Allocation",
	"Shopify Subscription",
	"Shopify Product Map",
	"Shopify API Error Log",
)


def shopify_doctypes_left_here():
	return frappe.get_all(
		"DocType", filters={"module": OLD_MODULE, "name": ["like", "Shopify %"]}, pluck="name"
	)


def has_shopify_data():
	for doctype in DATA_DOCTYPES:
		if frappe.db.table_exists(doctype) and frappe.get_all(doctype, limit=1, pluck="name"):
			return True
	# Shopify Settings always has rows (its defaults); only a connected store counts.
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM `tabSingles` WHERE doctype = 'Shopify Settings'
			AND field IN ('shop_domain', 'client_id') AND IFNULL(value, '') != '' LIMIT 1"""
		)
	)


def before_migrate():
	doctypes = shopify_doctypes_left_here()
	if not doctypes:
		return

	installed = APP in frappe.get_installed_apps()
	if not installed and not has_shopify_data():
		return
	if not installed and APP not in frappe.get_all_apps():
		raise frappe.ValidationError(
			f"This site has Shopify orders and settings. Add the {APP} app to the bench before "
			f"updating ecommerce_integration, or migrate would delete their DocTypes."
		)

	# The shipped JSON matches the DocTypes on the site except for `module`, so
	# install's sync skips them as unchanged; reassign them here instead, before
	# install runs the app's after_install against them.
	frappe.db.set_value("DocType", {"name": ["in", doctypes]}, "module", NEW_MODULE, update_modified=False)
	frappe.clear_cache()
	# A module map cached before the app reached the bench would not resolve it.
	frappe.local.module_app.setdefault(frappe.scrub(NEW_MODULE), APP)

	if not installed:
		from frappe.installer import install_app

		print(f"ecommerce_integration: Shopify connector moved, installing {APP} on {frappe.local.site}")
		install_app(APP, set_as_patched=True)
