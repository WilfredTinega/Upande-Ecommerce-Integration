# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Self-contained helpers for the ecommerce integration.

This app configures itself. Its channel Singles — `Biflorica Setting` and
`Floriday Settings` — own the price list and the order-as-quotation toggle, so
nothing here reads another app's Single. `Webshop Settings` in particular is
never touched: it belongs to upande_webshop, and reading it on a site without
that app queued "DocType Webshop Settings not found" onto `frappe.message_log`
once per priced row, which surfaced as a wall of identical dialogs on the
Biflorica Stock tab.

This app reads no upande_webshop doctype at all. Enabled stock lives in its own
`Ecommerce Enabled Stock`, Floriday trade-item mappings in its own
`Floriday Item Length`, and every per-stem rate comes from ERPNext `Item Price`
plus the post-harvest `Stem Length.price` master.
"""

import frappe
from frappe.utils import cint

USD_PRICE_LIST = "USD Price List"

# The channel Singles this app owns. Probed in this order wherever a setting is
# channel-agnostic, so a site running only one of the two still resolves.
CHANNEL_SETTINGS = ("Biflorica Setting", "Floriday Settings")


def has_doctypes(*doctypes):
	"""True only when every named DocType exists on this site.

	Guards raw SQL against doctypes this app references but does not own — chiefly
	`Shelf Item` and `Stem Length`, which belong to the post-harvest suite.
	Without this, a site running only part of the suite gets a bare
	`MySQLdb.ProgrammingError: Table '...' doesn't exist` from the Floriday and
	Biflorica screens instead of an empty result.
	"""
	return all(frappe.db.exists("DocType", doctype) for doctype in doctypes)


def channel_setting(fieldname, settings_doctype=None):
	"""One value from a channel Single, or None.

	`settings_doctype` names the channel; without it both are tried in
	`CHANNEL_SETTINGS` order and the first non-empty answer wins.

	Existence is CHECKED rather than a caught read failure: `frappe.get_meta` on
	an absent doctype (and `get_cached_value` on an absent field) queue a message
	on `frappe.message_log` *before* raising, so a try/except swallows the
	exception and leaves the message to pop up later as a dialog.
	"""
	for doctype in [settings_doctype] if settings_doctype else CHANNEL_SETTINGS:
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.get_meta(doctype).has_field(fieldname):
			continue
		value = frappe.db.get_single_value(doctype, fieldname)
		if value:
			return value
	return None


def create_orders_as_quotation(settings_doctype=None):
	"""True when this channel keeps incoming orders as draft Quotations.

	Read from the channel's own Single (`Biflorica Setting` /
	`Floriday Settings`), so each channel decides for itself. Defaults to False,
	which is the historical behaviour.
	"""
	return bool(cint(channel_setting("create_orders_as_quotation", settings_doctype)))


def _resolve_price_list(settings_doctype=None):
	"""Resolve the selling price list to read Item Price rates from.

	The channel's own `price_list` wins; otherwise the canonical
	"USD Price List", then the first enabled USD selling Price List. Memoized per
	request and per channel: the price chain asks once per item priced, and the
	answer cannot change mid-request.
	"""
	cache = getattr(frappe.local, "_ei_price_list", None)
	if cache is None:
		cache = frappe.local._ei_price_list = {}
	if settings_doctype in cache:
		return cache[settings_doctype]

	configured = channel_setting("price_list", settings_doctype)

	if configured and frappe.db.exists("Price List", configured):
		resolved = configured
	elif frappe.db.exists("Price List", USD_PRICE_LIST):
		resolved = USD_PRICE_LIST
	else:
		usd_lists = frappe.get_all(
			"Price List",
			filters={"currency": "USD", "enabled": 1, "selling": 1},
			fields=["name"],
			order_by="creation asc",
			limit=1,
		)
		resolved = usd_lists[0].name if usd_lists else configured

	cache[settings_doctype] = resolved
	return resolved
