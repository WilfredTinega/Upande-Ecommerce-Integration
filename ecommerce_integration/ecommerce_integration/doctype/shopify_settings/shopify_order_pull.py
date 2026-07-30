# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Pull orders from the shop and turn subscription purchases into schedules.

This store sells its subscriptions **without** Shopify selling plans: every box
product returns `selling_plan_groups: []`, so there are no SubscriptionContracts
to read. A "3 boxes over 3 months" gift is a single prepaid order carrying the
duration, start date and gift details as cart attributes or line-item properties.

So orders are the source of truth here. Each order is stored verbatim — including
every attribute Shopify sent, because the storefront's property *names* are not
discoverable from outside — and a Shopify Subscription is derived from it when it
covers more than one delivery.
"""

import json
import re

import frappe
from frappe.utils import add_days, cint, cstr, flt, get_datetime, getdate, now_datetime

from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
	flush_api_log,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_product_map.shopify_product_map import (
	resolve_line,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	get_shopify_settings,
	shopify_datetime,
	shopify_graphql,
	to_shopify_utc,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle import (
	end_date_for,
)

PAGE_SIZE = 25
MAX_PAGES = 200

ORDERS_QUERY = """
query ShopOrders($cursor: String, $pageSize: Int!, $filter: String) {
  orders(first: $pageSize, after: $cursor, query: $filter, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        updatedAt
        email
        note
        displayFinancialStatus
        displayFulfillmentStatus
        currencyCode
        customAttributes { key value }
        customer { id email displayName }
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        shippingAddress {
          firstName lastName phone
          address1 address2 city province country zip
        }
        lineItems(first: 50) {
          edges {
            node {
              id
              title
              variantTitle
              sku
              quantity
              customAttributes { key value }
              variant { id }
              originalUnitPriceSet { shopMoney { amount } }
            }
          }
        }
      }
    }
  }
}
"""


def _gid_suffix(gid):
	return (gid or "").rsplit("/", 1)[-1]


def _first_int(text):
	"""'3 boxes over 3 months' -> 3. Returns 0 when there's no leading number."""
	match = re.search(r"\d+", cstr(text))
	return cint(match.group()) if match else 0


def _parse_frequency(text, default="Monthly"):
	"""Map whatever the storefront wrote into Weekly / Fortnightly / Monthly.

	Seal Subscriptions expresses cadence several ways on the same order —
	`_frequency_days` = "14", `Frequency` = "Delivered every 14 days",
	`_frequency_unit` = "months", `_sealsub_interval_7-day` — so accept a day count
	or a unit word rather than requiring one exact spelling.
	"""
	raw = cstr(text).strip().lower()
	if not raw:
		return default

	for word, value in (("fortnight", "Fortnightly"), ("week", "Weekly"), ("month", "Monthly")):
		if word in raw:
			# "every 2 weeks" is fortnightly, not weekly.
			if value == "Weekly" and re.search(r"\b2\b", raw):
				return "Fortnightly"
			return value

	days = _first_int(raw)
	if days:
		if days <= 7:
			return "Weekly"
		if days <= 20:
			return "Fortnightly"
		return "Monthly"

	return default


def _maybe_date(text):
	try:
		return getdate(cstr(text)) if text else None
	except Exception:
		# Storefront date pickers emit all sorts of formats; an unparseable one is
		# not worth failing the whole order over.
		return None


class _Attributes:
	"""Order-level and line-level attributes, looked up case-insensitively.

	The storefront's exact property names are unknown, so lookups tolerate case
	and surrounding whitespace, and every attribute is also kept verbatim on the
	order for a human to read.
	"""

	def __init__(self):
		self.flat = {}
		self.rows = []

	def add(self, key, value, scope="Order", line_reference=None):
		if not key:
			return
		self.flat.setdefault(cstr(key).strip().lower(), cstr(value))
		self.rows.append(
			{
				"scope": scope,
				"attribute": cstr(key),
				"value": cstr(value),
				"line_reference": line_reference,
			}
		)

	def get(self, configured_name):
		if not configured_name:
			return None
		return self.flat.get(cstr(configured_name).strip().lower())


def _collect_attributes(node):
	attributes = _Attributes()

	for attribute in node.get("customAttributes") or []:
		attributes.add(attribute.get("key"), attribute.get("value"), scope="Order")

	for edge in ((node.get("lineItems") or {}).get("edges")) or []:
		line = edge.get("node") or {}
		for attribute in line.get("customAttributes") or []:
			attributes.add(
				attribute.get("key"),
				attribute.get("value"),
				scope="Line Item",
				line_reference=line.get("title"),
			)

	if node.get("note"):
		attributes.add("note", node.get("note"), scope="Order")

	return attributes


def _apply_order(doc, node, settings, attributes):
	doc.order_name = node.get("name")
	doc.order_date = shopify_datetime(node.get("createdAt"))
	doc.shopify_created_at = shopify_datetime(node.get("createdAt"))
	doc.shopify_updated_at = shopify_datetime(node.get("updatedAt"))
	doc.last_synced_on = now_datetime()
	doc.financial_status = node.get("displayFinancialStatus")
	doc.fulfillment_status = node.get("displayFulfillmentStatus")
	doc.currency = node.get("currencyCode")

	shopify_customer = node.get("customer") or {}
	doc.customer_email = shopify_customer.get("email") or node.get("email")

	from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_sync import (
		_resolve_customer,
	)

	doc.customer = _resolve_customer(doc.customer_email, shopify_customer.get("displayName"), settings)

	total = ((node.get("currentTotalPriceSet") or {}).get("shopMoney")) or {}
	if total.get("currencyCode"):
		doc.currency = total.get("currencyCode")

	# ---- delivery & recipient ------------------------------------------------
	address = node.get("shippingAddress") or {}
	doc.shipping_address = ", ".join(
		cstr(address.get(key)) for key in ("address1", "address2", "zip") if address.get(key)
	)
	doc.shipping_city = address.get("city")
	doc.shipping_country = address.get("country")

	shipped_to = " ".join(
		cstr(address.get(key)) for key in ("firstName", "lastName") if address.get(key)
	).strip()
	doc.recipient_name = attributes.get(settings.attr_recipient_name) or shipped_to
	doc.recipient_phone = attributes.get(settings.attr_recipient_phone) or address.get("phone")
	doc.recipient_email = None

	buyer = cstr(shopify_customer.get("displayName")).strip().lower()
	# A gift is an order shipping to someone other than the buyer.
	doc.is_gift = 1 if (doc.recipient_name and doc.recipient_name.strip().lower() != buyer) else 0

	doc.gift_note = attributes.get(settings.attr_note)
	doc.special_requests = attributes.get(settings.attr_special_requests)

	# ---- subscription terms --------------------------------------------------
	doc.duration_boxes = _first_int(attributes.get(settings.attr_duration)) or 1
	doc.frequency = _parse_frequency(
		attributes.get(settings.attr_frequency), settings.default_frequency or "Monthly"
	)

	start = _maybe_date(attributes.get(settings.attr_start_date))
	doc.first_delivery_date = start or getdate(shopify_datetime(node.get("createdAt")))
	doc.start_date = doc.first_delivery_date
	doc.end_date = end_date_for(doc.start_date, doc.frequency, doc.duration_boxes)
	if not doc.requested_delivery_date:
		doc.requested_delivery_date = doc.first_delivery_date

	# ---- lines ---------------------------------------------------------------
	doc.set("items", [])
	needs_allocation = False
	for edge in ((node.get("lineItems") or {}).get("edges")) or []:
		line = edge.get("node") or {}
		variant_id = _gid_suffix((line.get("variant") or {}).get("id"))
		qty = flt(line.get("quantity"))
		unit_price = ((line.get("originalUnitPriceSet") or {}).get("shopMoney")) or {}
		rate = flt(unit_price.get("amount"))

		map_name, line_class, stems = resolve_line(variant_id, qty)
		if line_class == "Box":
			needs_allocation = True

		doc.append(
			"items",
			{
				"shopify_line_id": _gid_suffix(line.get("id")),
				"variant_id": variant_id,
				"product_title": line.get("title"),
				"variant_title": line.get("variantTitle"),
				"sku": line.get("sku"),
				"product_map": map_name,
				"line_class": line_class,
				"stems": stems,
				"qty": qty,
				"rate": rate,
				"amount": qty * rate,
				"currency": doc.currency,
			},
		)

	doc.needs_allocation = 1 if needs_allocation else 0

	# ---- attributes, verbatim ------------------------------------------------
	doc.set("attributes", [])
	for row in attributes.rows:
		doc.append("attributes", row)

	doc.shopify_payload = json.dumps(node, indent=2)


def _upsert_subscription(order, settings):
	"""Derive a subscription from a multi-delivery order.

	Named SHOP-SUB-ORD<order id> so it shares the Shopify Subscription doctype with
	contract-sourced records without colliding with a real contract id.
	"""
	if cint(order.duration_boxes) <= 1:
		return None

	synthetic_id = f"ORD{order.shopify_order_id}"
	name = f"SHOP-SUB-{synthetic_id}"

	if frappe.db.exists("Shopify Subscription", name):
		doc = frappe.get_doc("Shopify Subscription", name)
	else:
		doc = frappe.new_doc("Shopify Subscription")
		doc.shopify_contract_id = synthetic_id

	doc.source = "Order"
	doc.source_order = order.name
	doc.customer = order.customer
	doc.customer_email = order.customer_email
	doc.currency = order.currency
	doc.start_date = order.start_date
	doc.end_date = order.end_date
	doc.deliveries_total = cint(order.duration_boxes)
	doc.next_billing_date = order.first_delivery_date
	doc.is_gift = order.is_gift
	doc.recipient_name = order.recipient_name
	doc.recipient_phone = order.recipient_phone
	doc.gift_note = order.gift_note
	doc.special_requests = order.special_requests
	doc.last_synced_on = now_datetime()

	# Status is owned by the expiry job from here on; only set it on creation.
	if doc.is_new():
		doc.status = "Active"

	doc.set("boxes", [])
	for line in order.items:
		if line.line_class != "Box":
			continue
		doc.append(
			"boxes",
			{
				"shopify_line_id": line.shopify_line_id,
				"variant_id": line.variant_id,
				"product_title": line.product_title,
				"variant_title": line.variant_title,
				"sku": line.sku,
				"box_item": line.box_item,
				"qty": line.qty,
				"rate": line.rate,
				"currency": line.currency,
			},
		)

	doc.save(ignore_permissions=True)

	if order.shopify_subscription != doc.name:
		frappe.db.set_value(
			"Shopify Order", order.name, {"shopify_subscription": doc.name}, update_modified=False
		)
	return doc.name


@frappe.whitelist()
def sync_orders(force=False):
	"""Pull orders updated since the watermark and derive their subscriptions."""
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.ord_enabled:
			return {"skipped": True, "reason": "Order Sync is disabled"}

	if not frappe.db.count("Shopify Product Map"):
		frappe.throw(
			"The Shopify Product Map is empty, so ordered lines cannot be told apart from "
			"delivery fees. Run 'Seed / Refresh Product Map' in Shopify Settings first."
		)

	watermark_at_entry = (
		get_datetime(settings.last_order_updated_at) if settings.last_order_updated_at else None
	)
	high_watermark = watermark_at_entry

	# Unlike subscriptionContracts, the orders connection does support a
	# server-side updated_at filter, so this is a genuine incremental pull.
	# Shopify filters in UTC; the watermark is stored in system time.
	if watermark_at_entry:
		since = watermark_at_entry
	else:
		# First run has no watermark. Bound it by Lookback Days rather than pulling
		# the store's entire order history — and note plain read_orders only exposes
		# the last 60 days anyway.
		since = add_days(now_datetime(), -(cint(settings.order_lookback_days) or 30))
	order_filter = f"updated_at:>='{to_shopify_utc(since)}'"

	cursor = None
	has_next = True
	pages = created = updated = skipped = failed = subscriptions = 0
	aborted = ""

	while has_next and pages < MAX_PAGES:
		pages += 1
		try:
			data = shopify_graphql(
				ORDERS_QUERY,
				{"cursor": cursor, "pageSize": PAGE_SIZE, "filter": order_filter},
				settings=settings,
				operation="Sync Orders",
			)
		except Exception as e:
			aborted = cstr(e)
			frappe.log_error(aborted, "Shopify Order Pull: GraphQL error")
			break

		connection = data.get("orders") or {}
		page_info = connection.get("pageInfo") or {}

		for edge in connection.get("edges") or []:
			node = edge.get("node") or {}
			order_id = _gid_suffix(node.get("id"))
			if not order_id:
				continue

			remote_updated = shopify_datetime(node.get("updatedAt"))
			doc_name = f"SHOP-ORD-{order_id}"
			exists = frappe.db.exists("Shopify Order", doc_name)

			if exists:
				stored = frappe.db.get_value("Shopify Order", doc_name, "shopify_updated_at")
				if stored and remote_updated and get_datetime(stored) >= remote_updated:
					skipped += 1
					continue
				doc = frappe.get_doc("Shopify Order", doc_name)
			else:
				doc = frappe.new_doc("Shopify Order")
				doc.shopify_order_id = order_id

			try:
				attributes = _collect_attributes(node)
				_apply_order(doc, node, settings, attributes)
				doc.save(ignore_permissions=True)

				if doc.needs_allocation and _upsert_subscription(doc, settings):
					subscriptions += 1

				frappe.db.commit()
				if exists:
					updated += 1
				else:
					created += 1

				if remote_updated and (not high_watermark or remote_updated > high_watermark):
					high_watermark = remote_updated
			except Exception as e:
				failed += 1
				frappe.db.rollback()
				frappe.log_error(cstr(e), f"Shopify Order Pull: order {order_id}")

		has_next = bool(page_info.get("hasNextPage"))
		cursor = page_info.get("endCursor")

	if high_watermark and not failed and not aborted:
		settings.db_set("last_order_updated_at", high_watermark, update_modified=False)

	summary = (
		aborted[:900]
		if aborted
		else (
			f"orders created {created}, updated {updated}, unchanged {skipped}, "
			f"failed {failed}; subscriptions derived {subscriptions}"
		)
	)
	settings.db_set("last_order_sync_summary", summary, update_modified=False)
	frappe.db.commit()
	flush_api_log()

	return {
		"summary": summary,
		# See sync_subscription_contracts: an aborted query is reported, not raised.
		"aborted": bool(aborted),
		"created": created,
		"updated": updated,
		"unchanged": skipped,
		"failed": failed,
		"subscriptions": subscriptions,
	}
