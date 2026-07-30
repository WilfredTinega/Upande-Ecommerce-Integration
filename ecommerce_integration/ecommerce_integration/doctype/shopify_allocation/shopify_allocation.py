# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class ShopifyAllocation(Document):
	"""Fills a stored Shopify Order from whatever stock is actually available.

	Submitting reserves that stock with a Material Transfer into the reserve
	warehouse, so the same stock cannot be promised to a second order. ERPNext's
	native Stock Reservation Entry is not usable here because it is bound to a
	Sales Order, and this flow deliberately creates none.
	"""

	def validate(self):
		if self.source_warehouse and self.reserve_warehouse:
			if self.source_warehouse == self.reserve_warehouse:
				frappe.throw("Source Warehouse and Reserve Warehouse cannot be the same.")

		self.total_qty = 0
		for row in self.items or []:
			row.available_qty = self._available_qty(row)
			self.total_qty += flt(row.qty)

	def _available_qty(self, row):
		warehouse = row.warehouse or self.source_warehouse
		if not (row.item_code and warehouse):
			return 0
		return flt(
			frappe.db.get_value("Bin", {"item_code": row.item_code, "warehouse": warehouse}, "actual_qty")
		)

	def before_submit(self):
		# Drafts are raised empty for the team to fill in, so the "something must be
		# allocated" rule belongs here rather than in validate().
		if not self.items or not flt(self.total_qty):
			frappe.throw("Allocate a quantity on at least one line before submitting.")

		# Cheaper and clearer to reject here than to let the Stock Entry fail with a
		# negative-stock error that doesn't name the allocation line.
		shortfalls = []
		for row in self.items:
			available = self._available_qty(row)
			if flt(row.qty) > available:
				shortfalls.append(
					f"row {row.idx}: {row.item_code} needs {flt(row.qty)} but "
					f"{available} is available in {row.warehouse or self.source_warehouse}"
				)
		if shortfalls:
			frappe.throw("Not enough stock to reserve — " + "; ".join(shortfalls))

	def on_submit(self):
		self._create_reservation()
		self._set_order_status("Allocated")
		self.db_set("status", "Allocated")

	def on_cancel(self):
		self.ignore_linked_doctypes = ("Stock Entry",)
		self._cancel_reservation()
		self.db_set("status", "Cancelled")
		self._refresh_order_state()

	# ------------------------------------------------------------------ pipeline

	@frappe.whitelist()
	def mark_packed(self):
		if self.docstatus != 1:
			frappe.throw("Submit the allocation before marking it packed.")
		self.db_set("status", "Packed")
		self._set_order_status("Packed")
		return self.status

	@frappe.whitelist()
	def mark_shipped(self):
		if self.status != "Packed":
			frappe.throw("Mark the allocation packed before shipping it.")
		self.db_set("status", "Shipped")
		self._set_order_status("Shipped")
		return self.status

	# ------------------------------------------------------------------ internals

	def _set_order_status(self, status):
		if not self.shopify_order:
			return
		frappe.db.set_value(
			"Shopify Order",
			self.shopify_order,
			{"allocation_status": status, "allocation": self.name},
			update_modified=False,
		)

	def _refresh_order_state(self):
		"""Recompute the order's status from its live allocations.

		A multi-delivery order has one allocation per delivery, so cancelling the
		second of three must not reset the whole order — the order reflects the least
		advanced delivery still standing.
		"""
		if not self.shopify_order:
			return

		siblings = frappe.get_all(
			"Shopify Allocation",
			filters=[["shopify_order", "=", self.shopify_order], ["docstatus", "<", 2]],
			fields=["name", "status", "delivery_index"],
			order_by="delivery_index asc",
		)

		live = [s for s in siblings if s.status != "Cancelled"]
		if not live:
			status, latest = "Not Allocated", None
		else:
			# Order-level status is the least advanced delivery still in play.
			ranking = ["Draft", "Allocated", "Packed", "Shipped"]
			least = min(live, key=lambda s: ranking.index(s.status) if s.status in ranking else 0)
			status = "Not Allocated" if least.status == "Draft" else least.status
			latest = live[-1].name

		frappe.db.set_value(
			"Shopify Order",
			self.shopify_order,
			{
				"allocation_status": status,
				"allocation": latest,
				"allocations_raised": len(siblings),
			},
			update_modified=False,
		)

	def _create_reservation(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
			get_shopify_settings,
		)

		if self.stock_entry:
			frappe.throw(f"This allocation already has reservation Stock Entry {self.stock_entry}.")

		settings = get_shopify_settings()
		company = settings.default_company or frappe.db.get_single_value("Global Defaults", "default_company")
		if not company:
			frappe.throw("Set a Company in Shopify Settings.")

		entry = frappe.new_doc("Stock Entry")
		entry.stock_entry_type = "Material Transfer"
		entry.purpose = "Material Transfer"
		entry.company = company
		entry.remarks = f"Subscription reservation for {self.name} ({self.delivery_date})"

		for row in self.items:
			if flt(row.qty) <= 0:
				continue
			entry.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": flt(row.qty),
					"s_warehouse": row.warehouse or self.source_warehouse,
					"t_warehouse": self.reserve_warehouse,
				},
			)

		if not entry.items:
			frappe.throw("Every allocation line resolves to zero quantity.")

		entry.insert(ignore_permissions=True)
		entry.submit()
		self.db_set("stock_entry", entry.name)

	def _cancel_reservation(self):
		if not self.stock_entry:
			return
		if not frappe.db.exists("Stock Entry", self.stock_entry):
			return
		entry = frappe.get_doc("Stock Entry", self.stock_entry)
		if entry.docstatus == 1:
			entry.flags.ignore_permissions = True
			entry.cancel()
