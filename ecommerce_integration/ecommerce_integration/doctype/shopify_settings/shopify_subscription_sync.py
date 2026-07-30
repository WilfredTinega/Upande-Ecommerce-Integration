# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Pull Shopify SubscriptionContracts into the Shopify Subscription doctype.

Polling rather than webhooks. Now that this lives in an app, webhook signature
verification (base64 HMAC-SHA256) is technically possible — it was not when this
ran as a Server Script, since that sandbox exposes no `hmac` or `base64`. Polling
is kept because subscription contracts change slowly and it needs no public
endpoint; a webhook receiver can be added alongside it later without disturbing
this path.
"""

import json

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, get_weekday, getdate, now_datetime

from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	get_shopify_settings,
	shopify_graphql,
)

PAGE_SIZE = 25
MAX_PAGES = 200

STATUS_MAP = {
	"ACTIVE": "Active",
	"PAUSED": "Paused",
	"CANCELLED": "Cancelled",
	"EXPIRED": "Expired",
	"FAILED": "Failed",
}

# `subscriptionContracts` takes no server-side updated_at filter, so the whole
# connection is paged and unchanged contracts are skipped client-side.
CONTRACTS_QUERY = """
query ShopifyContracts($cursor: String, $pageSize: Int!) {
  subscriptionContracts(first: $pageSize, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        createdAt
        updatedAt
        status
        nextBillingDate
        currencyCode
        customer { id email displayName }
        billingPolicy { interval intervalCount }
        deliveryPolicy { interval intervalCount }
        deliveryMethod {
          kind: __typename
          ... on SubscriptionDeliveryMethodShipping {
            address { address1 address2 city province country zip }
          }
        }
        lines(first: 50) {
          edges {
            node {
              id
              title
              variantTitle
              sku
              quantity
              currentPrice { amount currencyCode }
            }
          }
        }
      }
    }
  }
}
"""


def _gid_suffix(gid):
	"""gid://shopify/SubscriptionContract/12345 -> '12345'"""
	return (gid or "").rsplit("/", 1)[-1]


def _resolve_customer(email, display_name, settings):
	"""Match a Shopify subscriber to an ERPNext Customer.

	ERPNext keeps customer email on the linked Contact rather than on Customer, so
	the match goes through Contact Email -> Dynamic Link.
	"""
	if email:
		matched = frappe.db.sql(
			"""
			select dl.link_name
			from `tabContact Email` ce
			inner join `tabDynamic Link` dl
				on dl.parent = ce.parent and dl.parenttype = 'Contact'
			where ce.email_id = %s and dl.link_doctype = 'Customer'
			limit 1
			""",
			(email,),
		)
		if matched:
			return matched[0][0]

	if settings.create_missing_customer and display_name:
		customer = frappe.new_doc("Customer")
		customer.customer_name = display_name
		if settings.default_customer_group:
			customer.customer_group = settings.default_customer_group
		if settings.default_territory:
			customer.territory = settings.default_territory
		customer.insert(ignore_permissions=True)
		return customer.name

	return settings.default_customer


def _apply_contract(doc, node, settings):
	doc.status = STATUS_MAP.get(node.get("status") or "", "")
	doc.currency = node.get("currencyCode")
	doc.shopify_created_at = node.get("createdAt")
	doc.shopify_updated_at = node.get("updatedAt")
	doc.last_synced_on = now_datetime()
	doc.sync_error = None

	# ---- subscriber ----------------------------------------------------------
	shopify_customer = node.get("customer") or {}
	doc.shopify_customer_id = _gid_suffix(shopify_customer.get("id"))
	doc.customer_email = shopify_customer.get("email")
	doc.customer_name_shopify = shopify_customer.get("displayName")
	doc.customer = _resolve_customer(doc.customer_email, doc.customer_name_shopify, settings)

	# ---- billing cycle -------------------------------------------------------
	billing = node.get("billingPolicy") or {}
	delivery_policy = node.get("deliveryPolicy") or {}
	doc.interval = billing.get("interval") or delivery_policy.get("interval")
	doc.interval_count = cint(billing.get("intervalCount") or delivery_policy.get("intervalCount"))

	if node.get("nextBillingDate"):
		next_billing = get_datetime(node.get("nextBillingDate"))
		doc.next_billing_date = getdate(next_billing)
		doc.delivery_day = get_weekday(next_billing)

	# ---- delivery ------------------------------------------------------------
	method = node.get("deliveryMethod") or {}
	doc.delivery_method = method.get("kind")
	address = method.get("address") or {}
	doc.shipping_address = ", ".join(
		cstr(address.get(key)) for key in ("address1", "address2", "zip") if address.get(key)
	)
	doc.shipping_city = address.get("city")
	doc.shipping_country = address.get("country")

	# ---- boxes ---------------------------------------------------------------
	# One row per box product as Shopify sells it. Which varieties fill the box is
	# decided downstream, on the allocation.
	doc.set("boxes", [])
	cycle_amount = 0.0
	for edge in ((node.get("lines") or {}).get("edges")) or []:
		line = edge.get("node") or {}
		price = line.get("currentPrice") or {}
		qty = flt(line.get("quantity"))
		rate = flt(price.get("amount"))
		cycle_amount += qty * rate

		doc.append(
			"boxes",
			{
				"shopify_line_id": _gid_suffix(line.get("id")),
				"product_title": line.get("title"),
				"variant_title": line.get("variantTitle"),
				"sku": line.get("sku"),
				"qty": qty,
				"rate": rate,
				"currency": price.get("currencyCode") or doc.currency,
				"box_item": line.get("sku") if frappe.db.exists("Item", line.get("sku")) else None,
			},
		)

	doc.amount = cycle_amount
	doc.shopify_payload = json.dumps(node, indent=2)


@frappe.whitelist()
def sync_subscription_contracts(force=False):
	"""Page every subscription contract and upsert the changed ones.

	`force` is the manual "Sync Now" path and bypasses the enable switches; the
	scheduled path respects them.
	"""
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.sub_enabled:
			return {"skipped": True, "reason": "Subscription Contract Sync is disabled"}

	watermark_at_entry = (
		get_datetime(settings.last_sync_updated_at) if settings.last_sync_updated_at else None
	)
	high_watermark = watermark_at_entry

	cursor = None
	has_next = True
	pages = created = updated = skipped = failed = 0
	aborted = ""

	while has_next and pages < MAX_PAGES:
		pages += 1
		try:
			data = shopify_graphql(
				CONTRACTS_QUERY, {"cursor": cursor, "pageSize": PAGE_SIZE}, settings=settings
			)
		except Exception as e:
			# A mistyped field or a missing access scope lands here. Record it verbatim
			# rather than quietly syncing nothing.
			aborted = cstr(e)
			frappe.log_error(aborted, "Shopify Sync: GraphQL error")
			break

		connection = data.get("subscriptionContracts") or {}
		page_info = connection.get("pageInfo") or {}

		for edge in connection.get("edges") or []:
			node = edge.get("node") or {}
			contract_id = _gid_suffix(node.get("id"))
			if not contract_id:
				continue

			remote_updated = get_datetime(node.get("updatedAt")) if node.get("updatedAt") else None

			# Watermark is forward-only: anything older was handled on an earlier run.
			if watermark_at_entry and remote_updated and remote_updated < watermark_at_entry:
				skipped += 1
				continue

			doc_name = f"SHOP-SUB-{contract_id}"
			exists = frappe.db.exists("Shopify Subscription", doc_name)

			if exists:
				stored = frappe.db.get_value("Shopify Subscription", doc_name, "shopify_updated_at")
				if stored and remote_updated and get_datetime(stored) >= remote_updated:
					skipped += 1
					continue
				doc = frappe.get_doc("Shopify Subscription", doc_name)
			else:
				doc = frappe.new_doc("Shopify Subscription")
				doc.shopify_contract_id = contract_id

			try:
				_apply_contract(doc, node, settings)
				doc.save(ignore_permissions=True)
				# Commit per contract so one bad payload can't roll back a whole page.
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
				frappe.log_error(cstr(e), f"Shopify Sync: contract {contract_id}")

		has_next = bool(page_info.get("hasNextPage"))
		cursor = page_info.get("endCursor")

	# Only advance the watermark on a clean run — otherwise a contract that failed
	# would be skipped forever afterwards.
	if high_watermark and not failed and not aborted:
		settings.db_set("last_sync_updated_at", high_watermark, update_modified=False)

	summary = (
		aborted[:900]
		if aborted
		else f"created {created}, updated {updated}, unchanged {skipped}, failed {failed}, pages {pages}"
	)
	settings.db_set("last_sync_summary", summary, update_modified=False)
	frappe.db.commit()

	return {
		"summary": summary,
		"created": created,
		"updated": updated,
		"unchanged": skipped,
		"failed": failed,
	}
