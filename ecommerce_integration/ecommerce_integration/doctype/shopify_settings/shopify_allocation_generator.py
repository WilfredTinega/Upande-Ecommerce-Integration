# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Raise the draft allocations an order needs — one per delivery.

A one-off box gets a single allocation. "3 boxes over 3 months" gets three, dated
by the order's frequency from its first delivery date. Each draft is deliberately
empty of lines: what fills a box depends on what is actually available, which is a
decision for the sales team rather than for a scheduler.
"""

import frappe
from frappe.utils import cint, cstr, flt, getdate

from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	get_shopify_settings,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle import (
	next_delivery_date,
)

INACTIVE_SUBSCRIPTION_STATUSES = ("Cancelled", "Expired", "Inactive", "Paused")


def _required_stems(order):
	return sum(flt(line.stems) for line in order.items if line.line_class == "Box")


def create_allocations_for_order(order_name, settings=None):
	"""Raise every allocation this order still needs. Returns the names created.

	Idempotent: allocation names are derived from the order and delivery number, so
	re-running tops up only what's missing.
	"""
	settings = settings or get_shopify_settings()
	order = frappe.get_doc("Shopify Order", order_name)

	if not order.needs_allocation:
		return []

	if not order.customer:
		frappe.throw(
			f"{order.name} has no ERPNext customer. Link one, or set a Fallback Customer "
			"in Shopify Settings and re-sync."
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

		allocation = frappe.new_doc("Shopify Allocation")
		allocation.shopify_order = order.name
		allocation.delivery_index = index
		allocation.deliveries_total = deliveries_total
		allocation.shopify_subscription = order.shopify_subscription
		allocation.customer = order.customer
		allocation.delivery_date = next_delivery_date(getdate(first_date), order.frequency, periods=index - 1)
		allocation.required_stems = required_stems
		allocation.status = "Draft"
		allocation.source_warehouse = settings.default_source_warehouse
		allocation.reserve_warehouse = settings.default_reserve_warehouse
		allocation.insert(ignore_permissions=True)
		created.append(allocation.name)

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
def generate_allocations(force=False):
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.alloc_enabled:
			return {"skipped": True, "reason": "Generate Allocations is disabled"}

	# Both warehouses are mandatory on the allocation, so fail once here rather than
	# logging one identical error per order.
	if not settings.default_source_warehouse:
		frappe.throw("Shopify Settings: set a Default Source Warehouse before generating allocations.")
	if not settings.default_reserve_warehouse:
		frappe.throw("Shopify Settings: set a Default Reserve Warehouse before generating allocations.")

	# Fee-only orders (delivery fees, packaging) carry no boxes and are skipped.
	orders = frappe.get_all(
		"Shopify Order",
		filters=[["needs_allocation", "=", 1], ["allocation_status", "!=", "Cancelled"]],
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

	summary = f"allocations created {created}, orders already complete {complete}, failed {failed}"
	if errors:
		summary += " | " + "; ".join(errors[:3])
	settings.db_set("last_allocation_summary", summary[:900], update_modified=False)
	frappe.db.commit()

	return {"summary": summary, "created": created, "complete": complete, "failed": failed}
