# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Self-contained helpers for the ecommerce integration.

Vendored here (rather than imported from upande_webshop) so this app carries no
code dependency on any other custom app. Where a helper reads another app's
Single (e.g. Webshop Settings), the read is guarded so it degrades to a safe
default when that app/doctype is not installed.
"""

import frappe
from frappe.utils import cint

USD_PRICE_LIST = "USD Price List"


def create_orders_as_quotation():
	"""True when the site is configured to keep orders as draft Quotations
	instead of creating Sales Orders directly.

	Read from the Webshop Settings single when present so every order source
	honours the same toggle. Defaults to False when the field or the doctype is
	not on the site, so behaviour is unchanged on sites without upande_webshop.
	"""
	try:
		return bool(cint(frappe.get_cached_doc("Webshop Settings").get("create_orders_as_quotation")))
	except Exception:
		return False


def _resolve_price_list():
	"""Resolve the price list to read Item Price rates from.

	Prefers a Webshop Settings.price_list value if that app/field is present,
	then the canonical "USD Price List", then the first enabled USD selling
	Price List. Fully guarded so it never raises on a site without upande_webshop.
	"""
	configured = None
	try:
		if frappe.get_meta("Webshop Settings").has_field("price_list"):
			configured = frappe.db.get_single_value("Webshop Settings", "price_list")
	except Exception:
		configured = None

	if configured and frappe.db.exists("Price List", configured):
		return configured
	if frappe.db.exists("Price List", USD_PRICE_LIST):
		return USD_PRICE_LIST
	usd_lists = frappe.get_all(
		"Price List",
		filters={"currency": "USD", "enabled": 1, "selling": 1},
		fields=["name"],
		order_by="creation asc",
		limit=1,
	)
	if usd_lists:
		return usd_lists[0].name
	return configured
