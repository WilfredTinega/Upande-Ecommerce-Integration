# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ShopifySubscription(Document):
	"""Mirror of a Shopify SubscriptionContract.

	Written only by the poller (shopify_subscription_sync). Every Shopify-owned
	field is read-only in the UI so a local edit can't silently disagree with the
	shop and then be overwritten on the next sync.
	"""

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
