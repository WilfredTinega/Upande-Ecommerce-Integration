# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Read side for the packhouse portal's Biflorica Orders / Floriday Orders pages.

The pages (upande_packhouse www/biflorica-orders, www/floriday-orders) do their
work through the endpoints the desk forms already use — get_deals / get_predeals
/ post_offers, create_sales_orders_from_floriday / create_batches_on_floriday,
and the stock_picker enable/disable pair — so a poll from the portal behaves
exactly like the button on Biflorica Setting / Floriday Settings. What those
endpoints do not give a page is one snapshot of the channel: how its Single is
configured, what its schedules last did, and which orders it has imported. That
is all this module adds, plus `poll_orders`, which runs those same importers
over a look-back the operator picks on the page instead of the Single's Period.

Every value is read from the channel's own Single, so the pages always reflect
the settings the desk forms hold.
"""

import json
import re

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, cint, flt, now_datetime, nowdate

# Bounds of the page's look-back picker, in hours: one hour up to 30 days.
MIN_POLL_HOURS = 1
MAX_POLL_HOURS = 720

from ecommerce_integration.ecommerce_integration.utils import create_orders_as_quotation
from ecommerce_integration.ecommerce_integration.utils.shelf_stock import shelf_stock_enabled

# How each channel's orders are recognised. Biflorica stamps po_no with
# "BIFLORICA-<id>" / "BIFLORICA-PREDEAL-<id>" (biflorica_setting.deal_po_ref).
# Floriday stamps po_no with the Floriday salesOrderId, a UUID, and carries the
# buyer GLN / fulfilment id in its own custom fields.
FLORIDAY_UUID_LIKE = "________-____-____-____-____________"

CHANNELS = {
	"biflorica": {
		"label": "Biflorica",
		"settings": "Biflorica Setting",
		"credentials": ("base_url", "access_token", "platform", "farm"),
		"period_field": "deals_period",
		# (prefix, label) of each scheduled job the pages show and can edit. The
		# access-token refresh is deliberately absent: it is plumbing, managed on
		# the settings form only.
		"schedules": (
			("deals", "Deals"),
			("predeal", "Predeals"),
			("offer", "Post Offers"),
		),
		# Predeals run inside the deals job, on the deals frequency; only their
		# on/off switch is their own.
		"shared_frequency": {"predeal": "deals"},
	},
	"floriday": {
		"label": "Floriday",
		"settings": "Floriday Settings",
		"credentials": ("api_key", "base_url", "access_token", "organization_supplier_id"),
		"period_field": "period",
		"schedules": (
			("so", "Sales Orders"),
			("of", "Order Fulfilment"),
			("batch", "Create Batch"),
			("supplyline", "Supply Line"),
			("stock", "Refresh Stock"),
			("fi", "Sync Trade Items"),
		),
		"shared_frequency": {},
	},
}


def _channel(channel):
	cfg = CHANNELS.get((channel or "").strip().lower())
	if not cfg:
		frappe.throw(_("Unknown sales channel: {0}").format(channel))
	if not frappe.db.exists("DocType", cfg["settings"]):
		frappe.throw(_("{0} is not set up on this site.").format(cfg["settings"]))
	frappe.has_permission(cfg["settings"], "read", throw=True)
	return cfg


def _single_values(doctype, fieldnames):
	"""{fieldname: value} for the fields this Single actually has."""
	meta = frappe.get_meta(doctype)
	present = [f for f in fieldnames if meta.has_field(f)]
	values = frappe.db.get_singles_dict(doctype) if present else {}
	return {f: values.get(f) for f in present}


def _settings_summary(cfg):
	doctype = cfg["settings"]
	values = _single_values(
		doctype,
		(
			"warehouse",
			"stock_warehouse",
			"price_list",
			"customer",
			"use_shelf_stock",
			"publish_enabled_stock_only",
			"create_orders_as_quotation",
			cfg["period_field"],
			"predeal_period",
			*cfg["credentials"],
		),
	)
	missing = [f for f in cfg["credentials"] if f in values and not values.get(f)]

	schedules = _schedules(cfg)

	return {
		"settings_doctype": doctype,
		"warehouse": values.get("warehouse") or values.get("stock_warehouse"),
		"price_list": values.get("price_list"),
		"customer": values.get("customer"),
		"use_shelf_stock": bool(shelf_stock_enabled(doctype)),
		"use_shelf_stock_flag": cint(values.get("use_shelf_stock")),
		"publish_enabled_stock_only": cint(values.get("publish_enabled_stock_only")),
		"order_doctype": "Quotation" if create_orders_as_quotation(doctype) else "Sales Order",
		# The Single's own look-back, used by the scheduled jobs. Zero means
		# unset: Floriday then falls back to 24h, Biflorica to no window at all.
		"period_hours": cint(values.get(cfg["period_field"])),
		"predeal_period_hours": cint(values.get("predeal_period")),
		"scheduled_lookback_hours": _scheduled_lookback_hours(cfg, cint(values.get(cfg["period_field"]))),
		"missing_credentials": [frappe.get_meta(doctype).get_label(f) for f in missing],
		"schedules": schedules,
	}


def _scheduled_lookback_hours(cfg, period):
	"""Hours the scheduled order import looks back, or None for "everything".

	Floriday's run_sales_order reads `period` alone (24h when unset). Biflorica's
	run_sync_deals_and_predeals looks back one frequency interval, widened by
	`deals_period` when that is longer; for All/Cron it falls back to the period.
	"""
	if cfg["settings"] == "Floriday Settings":
		return period or 24

	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
		_FREQUENCY_LOOKBACK,
	)

	frequency = (frappe.db.get_single_value(cfg["settings"], "deals_event_frequency") or "").strip()
	delta = _FREQUENCY_LOOKBACK.get(frequency) or {}
	interval = cint(delta.get("hours")) + cint(delta.get("days")) * 24
	if interval:
		return max(interval, period)
	return period or None


def _schedules(cfg):
	"""The channel's editable scheduled jobs, with live run times.

	Last/next run come from the Scheduled Job Type rows (the same lookup the
	settings form does on load); the copies stored on the Single only refresh
	when someone opens that form, so they go stale.
	"""
	doc = frappe.get_single(cfg["settings"])
	# Biflorica also stamps *_last_run itself when a run is triggered by hand;
	# keep that as the fallback for a job the scheduler has not executed yet.
	stored_last = {prefix: doc.get(f"{prefix}_last_run") for prefix, _label in cfg["schedules"]}
	populate = getattr(doc, "_populate_scheduler_run_times", None)
	if populate:
		populate()
	meta = frappe.get_meta(cfg["settings"])

	out = []
	for prefix, label in cfg["schedules"]:
		if not meta.has_field(f"{prefix}_enabled"):
			continue
		shared = cfg["shared_frequency"].get(prefix)
		freq_df = meta.get_field(f"{prefix}_event_frequency")
		out.append(
			{
				"key": prefix,
				"label": label,
				"enabled": cint(doc.get(f"{prefix}_enabled")),
				"frequency": doc.get(f"{shared or prefix}_event_frequency"),
				"cron_format": doc.get(f"{shared or prefix}_cron_format"),
				"frequency_options": [o for o in (freq_df.options or "").split("\n") if o] if freq_df else [],
				"shared_with": dict(cfg["schedules"]).get(shared) if shared else None,
				"last_run": doc.get(f"{shared or prefix}_last_run") or stored_last.get(shared or prefix),
				"next_run": doc.get(f"{shared or prefix}_next_run"),
			}
		)
	return out


def _channel_filters(channel_key, doctype):
	"""(filters, or_filters) picking this channel's orders out of `doctype`."""
	if channel_key == "biflorica":
		return {"po_no": ["like", "BIFLORICA-%"]}, None

	or_filters = [["po_no", "like", FLORIDAY_UUID_LIKE]]
	for fieldname in ("custom_floriday_delivery_id", "custom_floriday_fulfillment_order_id"):
		if frappe.db.has_column(doctype, fieldname):
			or_filters.append([fieldname, "is", "set"])
	return {}, or_filters


def _recent_orders(channel_key, days, limit=200):
	since = add_days(nowdate(), -max(1, cint(days) or 14))
	rows = []
	for doctype in ("Sales Order", "Quotation"):
		if not frappe.db.has_column(doctype, "po_no"):
			continue
		if not frappe.has_permission(doctype, "read"):
			continue

		if doctype == "Sales Order":
			fields = [
				"name",
				"customer",
				"customer_name",
				"transaction_date",
				"delivery_date",
				"po_no",
				"status",
				"docstatus",
				"grand_total",
				"currency",
				"total_qty",
			]
		else:
			fields = [
				"name",
				"party_name as customer",
				"customer_name",
				"transaction_date",
				"valid_till as delivery_date",
				"po_no",
				"status",
				"docstatus",
				"grand_total",
				"currency",
				"total_qty",
			]
		for optional in ("custom_consignee", "custom_ordered_stems", "custom_floriday_fulfillment_order_id"):
			if frappe.db.has_column(doctype, optional):
				fields.append(optional)

		filters, or_filters = _channel_filters(channel_key, doctype)
		filters["transaction_date"] = [">=", since]
		for r in frappe.get_list(
			doctype,
			filters=filters,
			or_filters=or_filters,
			fields=fields,
			order_by="creation desc",
			limit_page_length=limit,
		):
			r["doctype"] = doctype
			r["grand_total"] = flt(r.get("grand_total"))
			r["total_qty"] = flt(r.get("total_qty"))
			po_no = r.get("po_no") or ""
			if channel_key == "biflorica":
				r["kind"] = "Predeal" if po_no.startswith("BIFLORICA-PREDEAL-") else "Deal"
				r["channel_ref"] = po_no.split("-")[-1]
			else:
				r["kind"] = "Fulfilled" if r.get("custom_floriday_fulfillment_order_id") else "Order"
				r["channel_ref"] = po_no[-8:]
			rows.append(r)

	rows.sort(key=lambda r: (str(r.get("transaction_date") or ""), r["name"]), reverse=True)
	return rows[:limit]


def _warehouse_stock(channel_key, summary):
	"""Stock the channel offers when Use Shelf Stock is off, read-only.

	Mirrors the desk: Biflorica's "Stock Available for Offers" table (filled by
	its Refresh Stock button) and Floriday's "Stock In Online Available for Sale"
	(computed live by get_floriday_stock).
	"""
	if summary["use_shelf_stock"]:
		return []

	if channel_key == "biflorica":
		return [
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"stem_length": r.stem_length,
				"qty": flt(r.qty),
				"price_per_stem": flt(r.price_per_stem),
				"warehouse": r.warehouse,
			}
			for r in frappe.get_all(
				"Biflorica Stock View",
				filters={"parent": "Biflorica Setting", "parenttype": "Biflorica Setting"},
				fields=["item_code", "item_name", "stem_length", "qty", "price_per_stem", "warehouse"],
				order_by="idx asc",
			)
		]

	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings import (
		get_floriday_stock,
	)

	return [
		{
			"item_code": r.get("item_code"),
			"item_name": r.get("item_name"),
			"stem_length": r.get("stem_length"),
			"qty": flt(r.get("qty")),
			"trade_item_id": r.get("trade_item_id"),
			"warehouse": r.get("warehouse"),
		}
		for r in get_floriday_stock() or []
	]


def _live_offers(channel_key):
	"""The offers last fetched from Biflorica (Biflorica Setting's Live Offers table)."""
	if channel_key != "biflorica":
		return []
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
		_offer_has_expired,
	)

	rows = frappe.get_all(
		"Biflorica Offer View",
		filters={"parent": "Biflorica Setting", "parenttype": "Biflorica Setting"},
		fields=[
			"offer_id",
			"type",
			"variety",
			"color",
			"size",
			"sizes_stems",
			"quantity",
			"packing",
			"price_per_stem",
			"price_per_stem_list",
			"price",
			"box_type",
			"date_start",
			"date_end",
		],
		order_by="idx asc",
	)
	# The table is a snapshot from the last fetch: an offer running then may
	# have ended since, so flag it rather than show it as live.
	for r in rows:
		r["expired"] = bool(r.date_end and _offer_has_expired({"dateEnd": str(r.date_end)}))
	return rows


def _box_types(channel_key):
	"""Post Offers box types: Biflorica's codes merged with the site's Box Types."""
	if channel_key != "biflorica":
		return []
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
		get_box_type_options,
	)

	return get_box_type_options()


@frappe.whitelist()
def get_channel_overview(channel: str, days: int | str = 14):
	"""Settings, schedules, recent orders, live offers and (warehouse-mode) stock."""
	cfg = _channel(channel)
	key = channel.strip().lower()
	summary = _settings_summary(cfg)
	return {
		"channel": key,
		"label": cfg["label"],
		"settings": summary,
		"orders": _recent_orders(key, days),
		"warehouse_stock": _warehouse_stock(key, summary),
		"live_offers": _live_offers(key),
		"box_types": _box_types(key),
		"price_lists": frappe.get_all(
			"Price List",
			filters={"selling": 1, "enabled": 1},
			fields=["name", "currency"],
			order_by="name asc",
		),
		"can_edit_settings": bool(frappe.has_permission(cfg["settings"], "write")),
	}


@frappe.whitelist(methods=["POST"])
def set_channel_period(channel: str, hours: int | str):
	"""Set how far back the channel's scheduled order import looks, in hours.

	Floriday: Floriday Settings.period, read by run_sales_order. Biflorica: both
	deals_period and predeal_period — the scheduled job looks back the longer of
	its frequency interval and deals_period, and the desk Deals/Predeals buttons
	use each period directly, so the two are kept equal.
	"""
	cfg = _channel(channel)
	frappe.has_permission(cfg["settings"], "write", throw=True)
	hours = cint(hours)
	if not MIN_POLL_HOURS <= hours <= MAX_POLL_HOURS:
		frappe.throw(_("Enter between {0} and {1} hours.").format(MIN_POLL_HOURS, MAX_POLL_HOURS))
	fields = ("deals_period", "predeal_period") if cfg["settings"] == "Biflorica Setting" else ("period",)
	for fieldname in fields:
		frappe.db.set_single_value(cfg["settings"], fieldname, hours)
	return {"period_hours": hours, "scheduled_lookback_hours": _scheduled_lookback_hours(cfg, hours)}


@frappe.whitelist(methods=["POST"])
def set_channel_price_list(channel: str, price_list: str | None = None):
	"""Point the channel at another selling Price List (blank = customer default).

	Written straight to the Single's `price_list`: saving the whole form would
	also re-sync its scheduled jobs, which a price change has no reason to do.
	The offer builder, stock picker and order import all read this field, so the
	new list applies to the next offer, price lookup and imported order.
	"""
	cfg = _channel(channel)
	frappe.has_permission(cfg["settings"], "write", throw=True)
	price_list = (price_list or "").strip() or None
	if price_list:
		row = frappe.db.get_value("Price List", price_list, ["selling", "enabled"], as_dict=True)
		if not row:
			frappe.throw(_("Price List {0} does not exist.").format(price_list))
		if not (row.selling and row.enabled):
			frappe.throw(_("Price List {0} is not an enabled selling price list.").format(price_list))
	frappe.db.set_single_value(cfg["settings"], "price_list", price_list)
	return {"price_list": price_list}


@frappe.whitelist(methods=["POST"])
def set_channel_schedule(
	channel: str,
	key: str,
	enabled: int | str | None = None,
	frequency: str | None = None,
	cron_format: str | None = None,
):
	"""Change one scheduled job: switch it on/off, or change how often it runs.

	Saved through the settings document, so its on_update re-syncs the Scheduled
	Job Type exactly as saving the desk form would. Only the jobs the page lists
	are accepted — never the access-token refresh.
	"""
	cfg = _channel(channel)
	frappe.has_permission(cfg["settings"], "write", throw=True)
	key = (key or "").strip()
	if key not in dict(cfg["schedules"]):
		frappe.throw(_("{0} has no schedule {1} that can be changed here.").format(cfg["label"], key))

	doc = frappe.get_single(cfg["settings"])
	if enabled is not None:
		doc.set(f"{key}_enabled", 1 if cint(enabled) else 0)

	if frequency is not None:
		# A predeal's frequency is the deals job's; there is nothing of its own to set.
		if key in cfg["shared_frequency"]:
			frappe.throw(
				_("This job runs on the {0} schedule.").format(
					dict(cfg["schedules"])[cfg["shared_frequency"][key]]
				)
			)
		df = doc.meta.get_field(f"{key}_event_frequency")
		options = [o for o in (df.options or "").split("\n") if o] if df else []
		if frequency not in options:
			frappe.throw(_("Unknown frequency: {0}").format(frequency))
		doc.set(f"{key}_event_frequency", frequency)
		if frequency == "Cron":
			cron_format = (cron_format or "").strip()
			if not cron_format:
				frappe.throw(_("Enter a cron expression for a Cron schedule."))
			from croniter import croniter

			if not croniter.is_valid(cron_format):
				frappe.throw(_("{0} is not a valid cron expression.").format(cron_format))
			doc.set(f"{key}_cron_format", cron_format)

	doc.save()
	return {"schedules": _schedules(cfg)}


@frappe.whitelist()
def poll_orders(channel: str, kind: str | None = None, hours: int | str = 24):
	"""Import the channel's orders from the last `hours`, whatever its Period says.

	Runs the same importer as the desk button: get_deals / get_predeals for
	Biflorica (`kind` = "deals" | "predeals"), create_sales_orders_from_floriday
	for Floriday. Only the time window differs; the Single is not changed, so the
	scheduled jobs keep their own Period.
	"""
	cfg = _channel(channel)
	key = channel.strip().lower()
	hours = min(max(cint(hours) or 24, MIN_POLL_HOURS), MAX_POLL_HOURS)

	if key == "floriday":
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order import (
			create_sales_orders_from_floriday,
		)

		return create_sales_orders_from_floriday(period_hours=hours)

	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting import biflorica_setting

	importers = {"deals": biflorica_setting.get_deals, "predeals": biflorica_setting.get_predeals}
	importer = importers.get((kind or "deals").strip().lower())
	if not importer:
		frappe.throw(_("Unknown {0} order type: {1}").format(cfg["label"], kind))
	# A site-local datetime; get_deals converts it to the UTC window Biflorica expects.
	return importer(window_from=add_to_date(now_datetime(), hours=-hours))


def _belongs_to_channel(channel_key, doc):
	"""True when `doc` is an order this channel imported (same test as the list)."""
	po_no = (doc.get("po_no") or "").strip()
	if channel_key == "biflorica":
		return po_no.startswith("BIFLORICA-")
	if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", po_no):
		return True
	return bool(doc.get("custom_floriday_delivery_id") or doc.get("custom_floriday_fulfillment_order_id"))


@frappe.whitelist(methods=["POST"])
def submit_channel_orders(channel: str, names: str | list):
	"""Submit draft Sales Orders and approve them on the channel.

	Submitting is the channel's own confirmation step (see hooks.py): a Biflorica
	predeal is approved on Biflorica by its on_submit hook, and a Floriday order
	is queued for fulfilment on Floriday. A Biflorica *deal* has no such hook — it
	is only confirmed at import when "Confirm Deals on Biflorica" is ticked — so
	when that is off it is confirmed here, and a refusal rolls the submit back:
	an order stays a draft until its channel has accepted it.

	Each order is its own transaction, so one failure never undoes the others.
	Returns {submitted: [...], failed: [{sales_order, reason}]}.
	"""
	cfg = _channel(channel)
	key = channel.strip().lower()
	if isinstance(names, str):
		names = json.loads(names or "[]")

	confirm_deals = key == "biflorica" and not cint(
		frappe.db.get_single_value(cfg["settings"], "deals_auto_approve")
	)

	submitted, failed = [], []
	for name in names or []:
		try:
			doc = frappe.get_doc("Sales Order", name)
			if not _belongs_to_channel(key, doc):
				frappe.throw(_("{0} is not a {1} order.").format(name, cfg["label"]))
			if doc.docstatus != 0:
				frappe.throw(_("{0} is not a draft.").format(name))
			doc.check_permission("submit")
			doc.submit()

			approved = None
			po_no = doc.po_no or ""
			if key == "biflorica" and po_no.startswith("BIFLORICA-PREDEAL-"):
				approved = "predeal approved on Biflorica"
			elif confirm_deals:
				from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
					approve_deal,
				)

				deal_id = po_no.split("-")[-1]
				res = approve_deal(deal_id) or {}
				if not res.get("success"):
					raise frappe.ValidationError(
						_("Biflorica did not confirm deal {0}: {1}").format(deal_id, res.get("message") or "")
					)
				approved = "deal confirmed on Biflorica"
			elif key == "floriday":
				approved = "fulfilment queued on Floriday"

			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			submitted.append({"sales_order": name, "approved": approved})
		except Exception as e:
			frappe.db.rollback()
			# The hooks' own msgprints would pop one dialog per order on top of
			# the summary the page shows; `failed` carries the reason instead.
			reason = frappe.utils.strip_html(str(e)) or e.__class__.__name__
			frappe.clear_messages()
			failed.append({"sales_order": name, "reason": reason})

	return {"submitted": submitted, "failed": failed}
