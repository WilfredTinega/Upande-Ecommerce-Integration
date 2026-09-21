# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Shopify-aware override of the Upande Tambuzi `Farm Pack List` controller.

Nothing in `upande_tambuzi` is edited. Its controller is SUBCLASSED, so a pack
list that cannot be traced back to a Shopify Allocation runs the original code
unchanged — `super()` — and only a Shopify one takes a different branch. That
way the farm's own picking, packing, box labels and dispatch behave exactly as
they did before this app was installed.

What differs for a Shopify pack list, and why:

* **No Consolidated Pack List.** `process_consolidated_pack_list` exists to raise
  a Sales Invoice against a Sales Order. A Shopify delivery was paid for on
  Shopify and has no Sales Order, so there is nothing to consolidate and the
  upstream call throws *"Sales Order ID is required to process the CPL"* on the
  Reviewed transition. The stock transfer that the same branch performs is the
  part that matters, and it still runs — through upstream's own function.
"""

import frappe

from upande_tambuzi.upande_tambuzi.doctype.farm_pack_list.farm_pack_list import (
	FarmPackList as TambuziFarmPackList,
)
from upande_tambuzi.upande_tambuzi.doctype.farm_pack_list.farm_pack_list import (
	transfer_stock_on_submit,
)


def is_shopify_pack_list(doc):
	"""True when this pack list came from a Shopify Allocation.

	Traced through the pick list rather than off the missing Sales Order: a farm
	pack list raised by hand also has no allocation, and must not be caught here.
	"""
	pick = doc.get("custom_order_pick_list")
	if not pick:
		return False
	allocation = frappe.db.get_value("Order Pick List", pick, "custom_shopify_allocation")
	return bool(allocation) and bool(frappe.db.exists("Shopify Allocation", allocation))


class ShopifyAwareFarmPackList(TambuziFarmPackList):
	def validate(self):
		if not is_shopify_pack_list(self):
			return super().validate()

		# Upstream's Reviewed branch, minus the consolidated pack list. Kept as a
		# call into upstream's own function so a change to how stock moves on
		# Review reaches Shopify pack lists too.
		if self.workflow_state == "Reviewed" and not self.is_new():
			transfer_stock_on_submit(self)
