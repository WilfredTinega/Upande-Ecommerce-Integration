# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Shopify Order Allocation board.

The working page for allocating aged farm-shop stock to Shopify subscription
deliveries: what each shop holds in bunches, which orders are waiting on it, and
the allocate / submit / pick / pack / dispatch / cancel actions on each one.

It was first built directly on tambuzi.upande.com as a Web Page record, with the
CSS and JS pasted into doctype fields. That is why it lives here now: a field is
not linted, not diffed and not deployed, and a re-escaped backslash in one of
them silently turned the bunch-size regex into "a literal backslash followed by
digits" — so every bunch of 12 was counted as a single stem.

All the data comes from whitelisted endpoints called by the page itself
(`csr_shop_age` for shop stock, `frappe.client.*` for the allocations,
`run_doc_method` for the allocation's own pipeline methods), so this module only
has to guard the door and hand over the markup.

NOTE ON ROUTES: `DocumentPage` resolves before `TemplatePage`, so a Web Page
record whose route matches this folder would shadow it entirely. The original
record lives at /shop-bunches, which is why this page has its own route.
"""

import pathlib

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		raise frappe.PermissionError(_("Log in to allocate Shopify orders."))
	if not frappe.has_permission("Shopify Allocation", "read"):
		raise frappe.PermissionError(_("You are not permitted to read Shopify Allocations."))

	context.no_cache = 1
	context.title = _("Shopify Order Allocation")
	context.asset_version = _asset_version()


ASSETS = (
	"public/js/shopify_order_allocation.js",
	"public/css/shopify_order_allocation.css",
)


def _asset_version():
	"""Cache-buster taken from the assets' own mtimes.

	Files under `public/` are served straight off disk: no bundling, no hash in
	the filename. A browser will therefore keep the copy it fetched before the
	deploy, which is how a page that has been fixed goes on showing the old
	failure — it happened here, with a traceback for a Server Script the code no
	longer called.

	Their mtimes change when the app is deployed, so this changes with them.
	"""
	root = pathlib.Path(frappe.get_app_path("ecommerce_integration"))
	stamps = []
	for relative in ASSETS:
		try:
			stamps.append(int((root / relative).stat().st_mtime))
		except OSError:
			continue
	return str(max(stamps)) if stamps else frappe.utils.now()
