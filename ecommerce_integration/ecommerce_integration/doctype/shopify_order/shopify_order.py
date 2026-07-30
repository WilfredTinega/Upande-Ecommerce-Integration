# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class ShopifyOrder(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from ecommerce_integration.ecommerce_integration.doctype.shopify_order_attribute.shopify_order_attribute import (
			ShopifyOrderAttribute,
		)
		from ecommerce_integration.ecommerce_integration.doctype.shopify_order_item.shopify_order_item import (
			ShopifyOrderItem,
		)

		allocation: DF.Link | None
		allocation_status: DF.Literal["Not Allocated", "Allocated", "Packed", "Shipped", "Cancelled"]
		allocations_raised: DF.Int
		attributes: DF.Table[ShopifyOrderAttribute]
		currency: DF.Link | None
		customer: DF.Link | None
		customer_email: DF.Data | None
		duration_boxes: DF.Int
		end_date: DF.Date | None
		financial_status: DF.Data | None
		first_delivery_date: DF.Date | None
		frequency: DF.Literal["", "Weekly", "Fortnightly", "Monthly"]
		fulfillment_status: DF.Data | None
		gift_note: DF.SmallText | None
		is_gift: DF.Check
		items: DF.Table[ShopifyOrderItem]
		last_synced_on: DF.Datetime | None
		needs_allocation: DF.Check
		order_date: DF.Datetime | None
		order_name: DF.Data | None
		recipient_email: DF.Data | None
		recipient_name: DF.Data | None
		recipient_phone: DF.Data | None
		requested_delivery_date: DF.Date | None
		shipping_address: DF.SmallText | None
		shipping_city: DF.Data | None
		shipping_country: DF.Data | None
		shopify_created_at: DF.Datetime | None
		shopify_order_id: DF.Data
		shopify_payload: DF.Code | None
		shopify_subscription: DF.Link | None
		shopify_updated_at: DF.Datetime | None
		special_requests: DF.SmallText | None
		start_date: DF.Date | None
		total_amount: DF.Currency
		total_qty: DF.Float
	# end: auto-generated types

	"""A single subscription billing cycle, stored as received from Shopify.

	Deliberately not a Sales Order or Quotation: the order is held here and
	fulfilled through Shopify Allocation, which decides what available stock fills
	it before packing and shipping.
	"""

	def validate(self):
		self.total_qty = sum(flt(row.qty) for row in self.items or [])
		self.total_amount = sum(flt(row.amount) for row in self.items or [])

		if not self.requested_delivery_date and self.shopify_subscription:
			# Falls back to the contract's billing date so allocations have a date to
			# group by without someone typing it in per order.
			self.requested_delivery_date = frappe.db.get_value(
				"Shopify Subscription", self.shopify_subscription, "next_billing_date"
			)

	@frappe.whitelist()
	def create_allocations(self):
		"""Raise every allocation this order still needs — one per delivery."""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			create_allocations_for_order,
		)

		return create_allocations_for_order(self.name)
