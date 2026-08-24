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

from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
	flush_api_log,
)
from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	SUBSCRIPTION_CONTRACT_SCOPE,
	get_shopify_settings,
	scope_status,
	shopify_datetime,
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


def _explain_abort(error):
	"""Attach the fix to a raw GraphQL error when it is a denied scope.

	Shopify's message names the field and the code but not what to do about it, and
	ACCESS_DENIED on this query has exactly one cause worth naming. The error is
	trimmed first so the remedy survives the summary field's own truncation.
	"""
	if "ACCESS_DENIED" not in error:
		return error

	return (
		f"{error[:500]}\n\n"
		f"The token does not carry {SUBSCRIPTION_CONTRACT_SCOPE}, which this query "
		"requires. It is a protected scope: Shopify has to approve it, then it goes on "
		"a new app version which must be released and re-approved by the store, before "
		"Refresh Access Token can mint a token holding it."
	)


def _gid_suffix(gid):
	"""gid://shopify/SubscriptionContract/12345 -> '12345'"""
	return (gid or "").rsplit("/", 1)[-1]


def _resolve_customer(email, display_name, settings):
	"""Match a Shopify subscriber to an ERPNext Customer.

	ERPNext keeps customer email on the linked Contact rather than on Customer, so
	the match goes through Contact Email -> Dynamic Link.
	"""
	# Single-customer mode. When one Customer is configured, every order books to it and
	# no subscriber becomes a Customer of their own — the subscriber is an Address and a
	# Contact attached to that one account instead. Matching on email is skipped
	# deliberately: it could only ever return a different customer.
	if settings.default_customer:
		return settings.default_customer

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

	if settings.create_missing_customer and (display_name or email):
		try:
			return _create_subscriber(email, display_name, settings)
		except Exception as e:
			# Creation failing must not also cost the order. Fall through to the
			# fallback customer and leave the reason on record.
			frappe.log_error(cstr(e), f"Shopify: could not create a customer for {email or display_name}")

	return settings.default_customer


def _create_subscriber(email, display_name, settings):
	"""Create a Customer for a subscriber, and the Contact that makes them findable.

	The Contact is not decoration. Resolution matches on Contact Email, so a Customer
	created without one cannot be found again and the subscriber's next order creates
	another — silently, because ERPNext's default naming (`cust_master_name` =
	"Customer Name") appends a suffix to a colliding name instead of refusing it. You
	would get "RICHARD HOBBS", "RICHARD HOBBS - 1", "- 2", one per order, and nothing
	would look broken until someone counted.
	"""
	customer = frappe.new_doc("Customer")
	# An order can carry an email with no name; the address is a poor label but a
	# far better one than dropping the subscriber into a shared bucket.
	customer.customer_name = cstr(display_name or email).strip()
	if settings.default_customer_group:
		customer.customer_group = settings.default_customer_group
	if settings.default_territory:
		customer.territory = settings.default_territory
	customer.insert(ignore_permissions=True)

	if email:
		names = cstr(display_name or email).strip().split(" ", 1)
		contact = frappe.new_doc("Contact")
		contact.first_name = names[0]
		if len(names) > 1:
			contact.last_name = names[1]
		contact.append("email_ids", {"email_id": email, "is_primary": 1})
		contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})
		# Contact.autoname already de-duplicates its own name, so a second subscriber
		# sharing a display name is not a collision here.
		contact.insert(ignore_permissions=True)

	return customer.name


def _ensure_subscriber_address(customer, address, title, phone, email):
	"""Attach the subscriber's delivery address to the customer, once.

	Matched on line one plus city rather than inserted blindly: Address has no natural
	key and its own naming quietly appends a suffix on collision, so a repeat order
	would add another near-identical row to the same account every time.
	"""
	line1 = cstr((address or {}).get("address1")).strip()
	if not customer or not line1:
		return None

	city = cstr(address.get("city")).strip() or "Unknown"
	existing = frappe.db.sql(
		"""
		select a.name
		from tabAddress a
		inner join `tabDynamic Link` dl
			on dl.parent = a.name and dl.parenttype = 'Address'
		where dl.link_doctype = 'Customer' and dl.link_name = %s
			and a.address_line1 = %s and ifnull(a.city, '') = %s
		limit 1
		""",
		(customer, line1, city),
	)
	if existing:
		return existing[0][0]

	doc = frappe.new_doc("Address")
	doc.address_title = cstr(title or line1)[:140]
	doc.address_type = "Shipping"
	doc.address_line1 = line1
	doc.address_line2 = address.get("address2")
	doc.city = city
	doc.state = address.get("province")
	doc.pincode = address.get("zip")
	doc.phone = phone
	doc.email_id = email
	# Country is mandatory on Address, and Shopify sends a display name that may not be
	# a Country record at all. Fall back to the site default rather than lose the address.
	country = cstr(address.get("country")).strip()
	doc.country = (
		country if country and frappe.db.exists("Country", country) else frappe.db.get_default("country")
	)
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_subscriber_contact(customer, display_name, email, phone):
	"""Attach the subscriber as a Contact under the customer, once, keyed on email."""
	if not customer or not (email or display_name):
		return None

	if email:
		matched = frappe.db.sql(
			"""
			select ce.parent
			from `tabContact Email` ce
			inner join `tabDynamic Link` dl
				on dl.parent = ce.parent and dl.parenttype = 'Contact'
			where ce.email_id = %s and dl.link_doctype = 'Customer' and dl.link_name = %s
			limit 1
			""",
			(email, customer),
		)
		if matched:
			return matched[0][0]

	names = cstr(display_name or email).strip().split(" ", 1)
	doc = frappe.new_doc("Contact")
	doc.first_name = names[0]
	if len(names) > 1:
		doc.last_name = names[1]
	if email:
		doc.append("email_ids", {"email_id": email, "is_primary": 1})
	if phone:
		doc.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	# Contact.autoname de-duplicates its own name, so two subscribers sharing a display
	# name is not a collision here.
	doc.insert(ignore_permissions=True)
	return doc.name


def _apply_contract(doc, node, settings):
	doc.status = STATUS_MAP.get(node.get("status") or "", "")
	doc.currency = node.get("currencyCode")
	doc.shopify_created_at = shopify_datetime(node.get("createdAt"))
	doc.shopify_updated_at = shopify_datetime(node.get("updatedAt"))
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
		next_billing = shopify_datetime(node.get("nextBillingDate"))
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
def sync_subscription_contracts(force: bool = False):
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

	# Without the scope every page of this query is denied, so a run that cannot
	# succeed makes no request at all — otherwise the hourly job spends a call and an
	# error log per tick to learn the same thing. Only a positively-known absence
	# blocks; "unknown" still tries, since Shopify is the authority on the token.
	if scope_status(SUBSCRIPTION_CONTRACT_SCOPE, settings) == "missing":
		reason = (
			f"the Shopify app does not hold {SUBSCRIPTION_CONTRACT_SCOPE}, which "
			"subscriptionContracts requires. This store sells no selling plans, so there "
			"are no contracts to read either — leave Subscription Contract Sync off "
			"unless a subscriptions app is installed."
		)
		settings.db_set("last_sync_summary", f"Skipped: {reason}"[:900], update_modified=False)
		# Background sync: persist the records written above and the summary field
		# the form reads, so a later failure cannot discard a completed run.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return {"skipped": True, "reason": reason, "summary": f"Skipped: {reason}"}

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
				CONTRACTS_QUERY,
				{"cursor": cursor, "pageSize": PAGE_SIZE},
				settings=settings,
				operation="Sync Subscription Contracts",
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

			remote_updated = shopify_datetime(node.get("updatedAt"))

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
		_explain_abort(aborted)[:900]
		if aborted
		else f"created {created}, updated {updated}, unchanged {skipped}, failed {failed}, pages {pages}"
	)
	settings.db_set("last_sync_summary", summary, update_modified=False)
	# Background sync: persist the records written above and the summary field
	# the form reads, so a later failure cannot discard a completed run.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	flush_api_log()

	return {
		"summary": summary,
		# A GraphQL-level abort is swallowed above so a scheduled run doesn't crash;
		# flag it so callers can still tell success from a query that never ran.
		"aborted": bool(aborted),
		"created": created,
		"updated": updated,
		"unchanged": skipped,
		"failed": failed,
	}
