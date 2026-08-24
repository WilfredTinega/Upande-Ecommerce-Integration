# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ShopifySubscription(Document):
	"""A subscription, derived from the orders that make it up.

	Written by `rebuild_subscriptions_from_orders`, which reads the Shopify Orders
	already stored — this store sells no selling plans, so Shopify holds no contract to
	mirror. Shopify-owned fields stay read-only in the UI so a local edit can't
	silently disagree with the shop and then be overwritten on the next run.
	"""

	def on_trash(self):
		"""Clear the back-references before going.

		`Shopify Order.shopify_subscription` and the same field on Shopify Allocation are
		Links, so a deleted subscription leaves them pointing at nothing — and every
		later save of those documents then dies on link validation. A force-delete skips
		the outgoing link check that would otherwise refuse the deletion, which is
		exactly how a bulk clear-out leaves the wreckage behind.
		"""
		for doctype in ("Shopify Order", "Shopify Allocation"):
			for name in frappe.get_all(doctype, filters={"shopify_subscription": self.name}, pluck="name"):
				frappe.db.set_value(doctype, name, "shopify_subscription", None, update_modified=False)

	def validate(self):
		if self.status == "Active" and not self.customer:
			# An active subscription with no customer produces allocations nobody can
			# act on, so surface it here rather than at allocation time.
			frappe.msgprint(
				"No ERPNext customer is linked. Set a Fallback Customer in Shopify Settings, "
				"or link one here before allocations are raised.",
				indicator="orange",
				alert=True,
			)
