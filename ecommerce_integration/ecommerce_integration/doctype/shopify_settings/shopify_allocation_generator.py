# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Raise the draft allocations an order needs — one per delivery.

A one-off box gets a single allocation. "3 boxes over 3 months" gets three, dated
by the order's frequency from its first delivery date. Each draft is deliberately
empty of lines: what fills a box depends on what is actually available, which is a
decision for the sales team rather than for a scheduler.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate

from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	get_shopify_settings,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle import (
	next_delivery_date,
	scheduled_delivery_dates,
)

INACTIVE_SUBSCRIPTION_STATUSES = ("Cancelled", "Expired", "Inactive", "Paused")


def _required_stems(order):
	return sum(flt(line.stems) for line in order.items if line.line_class == "Box")


def _resolve_missing_customer(order, settings):
	"""Retry customer resolution for an order stored before a fallback existed.

	Re-syncing cannot do this. The order pull skips any order whose stored
	`shopify_updated_at` already matches Shopify's, so an order that has not changed
	on Shopify is never re-applied however far the watermark is rewound — the fix
	has to happen against the stored doc. Everything needed is already there: the
	subscriber email is a field, and the buyer's name is in the kept payload.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_sync import (
		_resolve_customer,
	)

	display_name = None
	if order.shopify_payload:
		try:
			display_name = ((json.loads(order.shopify_payload).get("customer") or {}) or {}).get(
				"displayName"
			)
		except Exception:
			# The payload is a debugging convenience, not a contract. A malformed one
			# must not be what stops an allocation being raised.
			display_name = None

	customer = _resolve_customer(order.customer_email, display_name, settings)
	if customer:
		frappe.db.set_value("Shopify Order", order.name, "customer", customer, update_modified=False)
		order.customer = customer
	return customer


def create_allocations_for_order(order_name, settings=None):
	"""Raise every allocation this order still needs. Returns the names created.

	Idempotent: allocation names are derived from the order and delivery number, so
	re-running tops up only what's missing.
	"""
	settings = settings or get_shopify_settings()
	order = frappe.get_doc("Shopify Order", order_name)

	if not order.needs_allocation:
		return []

	# Stock is committed by an allocation, so nothing unpaid should reserve any. This
	# is re-checked on every run rather than recorded, so an order paid later simply
	# starts allocating on the next pass.
	if cint(settings.allocate_paid_only) and cstr(order.financial_status).upper() != "PAID":
		return []

	# Orders pulled before a Fallback Customer was configured carry no customer, and
	# no amount of re-syncing will revisit them, so resolution is retried here.
	if not order.customer:
		_resolve_missing_customer(order, settings)

	if not order.customer:
		frappe.throw(
			f"{order.name} has no ERPNext customer, and none could be resolved from "
			f"{order.customer_email or 'a missing email'}. Set a Fallback Customer or tick "
			"Create Missing Customers in Shopify Settings, then run this again — or link a "
			"Customer on the order by hand. Re-syncing will not help: an order that has not "
			"changed on Shopify is never re-applied."
		)

	first_date = order.first_delivery_date or order.requested_delivery_date
	if not first_date:
		frappe.throw(f"{order.name} has no delivery date to allocate against.")

	# A subscription that has been stopped on Shopify must not keep generating work.
	if order.shopify_subscription:
		status = frappe.db.get_value("Shopify Subscription", order.shopify_subscription, "status")
		if status in INACTIVE_SUBSCRIPTION_STATUSES:
			return []

	deliveries_total = max(cint(order.duration_boxes), 1)
	required_stems = _required_stems(order)
	created = []

	for index in range(1, deliveries_total + 1):
		allocation_name = f"SHOP-ALL-{order.name}-{index}"
		if frappe.db.exists("Shopify Allocation", allocation_name):
			continue

		created.append(
			_new_allocation(
				order,
				settings,
				index,
				deliveries_total,
				next_delivery_date(getdate(first_date), order.frequency, periods=index - 1),
				required_stems,
			)
		)

	if created:
		frappe.db.set_value(
			"Shopify Order",
			order.name,
			{
				"allocation": created[-1],
				"allocations_raised": frappe.db.count("Shopify Allocation", {"shopify_order": order.name}),
			},
			update_modified=False,
		)
	return created


def _new_allocation(order, settings, index, deliveries_total, delivery_date, required_stems):
	"""One draft allocation. Deliberately empty of lines: what fills a box depends on
	what is actually available, which is the sales team's call, not a scheduler's."""
	allocation = frappe.new_doc("Shopify Allocation")
	allocation.shopify_order = order.name
	allocation.delivery_index = index
	allocation.deliveries_total = deliveries_total
	allocation.shopify_subscription = order.shopify_subscription
	allocation.customer = order.customer
	allocation.delivery_date = delivery_date
	allocation.required_stems = required_stems
	allocation.status = "Draft"
	allocation.source_warehouse = settings.default_source_warehouse
	allocation.reserve_warehouse = settings.default_reserve_warehouse
	allocation.insert(ignore_permissions=True)
	return allocation.name


def create_allocations_for_subscription(subscription_name, settings=None):
	"""Raise the draft allocations an open-ended subscription still needs.

	A prepaid order knows how many deliveries it bought, so its allocations are raised
	once and that is the end of it. An open-ended subscription never finishes, so it
	gets a rolling window instead of a count: every scheduled date between today and
	the horizon that has no allocation yet.

	The schedule is computed from the subscription's own start date rather than from
	today, so the index of a given date never shifts as the window rolls forward —
	that is what keeps re-running this idempotent, since an allocation's name is
	derived from that index.
	"""
	settings = settings or get_shopify_settings()
	subscription = frappe.get_doc("Shopify Subscription", subscription_name)

	if not cint(subscription.is_open_ended):
		return []
	if subscription.status in INACTIVE_SUBSCRIPTION_STATUSES:
		return []
	if not subscription.source_order or not frappe.db.exists("Shopify Order", subscription.source_order):
		return []

	order = frappe.get_doc("Shopify Order", subscription.source_order)

	# Same rule as the order path: the subscription is real and worth recording, but an
	# unpaid one does not get stock committed to it.
	if cint(settings.allocate_paid_only) and cstr(order.financial_status).upper() != "PAID":
		return []

	if not order.customer:
		_resolve_missing_customer(order, settings)
	if not order.customer:
		frappe.throw(
			f"{subscription.name} cannot be scheduled: {order.name} has no ERPNext customer. "
			"Set a Fallback Customer or tick Create Missing Customers in Shopify Settings."
		)

	start = subscription.start_date or order.first_delivery_date or order.requested_delivery_date
	if not start:
		frappe.throw(f"{subscription.name} has no start date to schedule from.")

	today = getdate(nowdate())
	horizon = add_days(today, (cint(settings.allocation_horizon_weeks) or 8) * 7)
	dates = scheduled_delivery_dates(start, order.frequency, subscription.delivery_days, horizon)

	required_stems = _required_stems(order)
	created = []

	for index, delivery_date in enumerate(dates, start=1):
		# Never backfill. A pile of overdue drafts for deliveries that already happened
		# is noise the packing team has to wade through, and the index stays stable
		# whether or not the early dates were ever raised.
		if delivery_date < today:
			continue
		allocation_name = f"SHOP-ALL-{order.name}-{index}"
		if frappe.db.exists("Shopify Allocation", allocation_name):
			continue
		created.append(_new_allocation(order, settings, index, 0, delivery_date, required_stems))

	if created:
		frappe.db.set_value(
			"Shopify Order",
			order.name,
			{
				"allocation": created[-1],
				"allocations_raised": frappe.db.count("Shopify Allocation", {"shopify_order": order.name}),
			},
			update_modified=False,
		)
	return created


@frappe.whitelist()
def generate_allocations(force: bool = False):
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.alloc_enabled:
			return {"skipped": True, "reason": "Generate Allocations is disabled"}

	# Both warehouses are mandatory on the allocation, so fail once here rather than
	# logging one identical error per order.
	if not settings.default_source_warehouse:
		frappe.throw(_("Shopify Settings: set a Default Source Warehouse before generating allocations."))
	if not settings.default_reserve_warehouse:
		frappe.throw(_("Shopify Settings: set a Default Reserve Warehouse before generating allocations."))

	# Fee-only orders (delivery fees, packaging) carry no boxes and are skipped.
	order_filters = [
		["needs_allocation", "=", 1],
		["allocation_status", "!=", "Cancelled"],
		["is_open_ended", "=", 0],
	]
	if cint(settings.allocate_paid_only):
		order_filters.append(["financial_status", "=", "PAID"])

	# Open-ended orders are skipped here too: their deliveries belong to a rolling
	# schedule on the subscription, not to a count on the order, and the pass below
	# raises them. Left in, each would get exactly one allocation and then stop.
	orders = frappe.get_all(
		"Shopify Order",
		filters=order_filters,
		fields=["name", "duration_boxes", "allocations_raised"],
		order_by="order_date asc",
	)

	created = complete = failed = 0
	errors = []

	for order in orders:
		# Cheap pre-filter: nothing to do when every delivery already has its allocation.
		if cint(order.allocations_raised) >= max(cint(order.duration_boxes), 1):
			complete += 1
			continue
		try:
			names = create_allocations_for_order(order.name, settings=settings)
			frappe.db.commit()
			created += len(names)
		except Exception as e:
			failed += 1
			frappe.db.rollback()
			errors.append(f"{order.name}: {cstr(e)}")
			frappe.log_error(cstr(e), f"Shopify Allocations: {order.name}")

	# Rolling pass for the subscriptions that never finish.
	rolling = 0
	open_ended = frappe.get_all(
		"Shopify Subscription",
		filters=[["is_open_ended", "=", 1], ["status", "not in", INACTIVE_SUBSCRIPTION_STATUSES]],
		fields=["name"],
	)
	for subscription in open_ended:
		try:
			names = create_allocations_for_subscription(subscription.name, settings=settings)
			frappe.db.commit()
			created += len(names)
			rolling += len(names)
		except Exception as e:
			failed += 1
			frappe.db.rollback()
			errors.append(f"{subscription.name}: {cstr(e)}")
			frappe.log_error(cstr(e), f"Shopify Allocations: {subscription.name}")

	summary = (
		f"allocations created {created} (rolling {rolling} across {len(open_ended)} open-ended), "
		f"orders already complete {complete}, failed {failed}"
	)
	if errors:
		summary += " | " + "; ".join(errors[:3])
	settings.db_set("last_allocation_summary", summary[:900], update_modified=False)
	# Background sync: persist the records written above and the summary field
	# the form reads, so a later failure cannot discard a completed run.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {"summary": summary, "created": created, "complete": complete, "failed": failed}
