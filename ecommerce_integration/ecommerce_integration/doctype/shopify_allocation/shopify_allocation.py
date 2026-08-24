# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, nowdate


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
				frappe.throw(_("Source Warehouse and Reserve Warehouse cannot be the same."))

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
			frappe.throw(_("Allocate a quantity on at least one line before submitting."))

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
			frappe.throw(_("Not enough stock to reserve") + " — " + "; ".join(shortfalls))

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
			frappe.throw(_("Submit the allocation before marking it packed."))
		self.db_set("status", "Packed")
		self._set_order_status("Packed")
		return self.status

	@frappe.whitelist()
	def mark_shipped(self):
		if self.status != "Packed":
			frappe.throw(_("Mark the allocation packed before shipping it."))
		self.db_set("status", "Shipped")
		self._set_order_status("Shipped")
		return self.status

	@frappe.whitelist()
	def create_pick_list(self):
		"""Raise an Upande Tambuzi `Order Pick List` for what this allocation reserved.

		Only after submitting: before that the lines are a proposal, and the reservation
		Stock Entry that proves the stock is really there does not exist yet.

		The OPL is the Tambuzi app's, not ours — no doctype is defined here. Its
		`locations` table is ERPNext's `Pick List Item` carrying Tambuzi's own additions,
		so each row gets `warehouse` and `custom_source_warehouse` set to the warehouse
		the stock was actually found in, `qty` as bunches and `stock_qty` as stems, which
		is what those fields are labelled on that site.
		"""
		self._require_tambuzi_packing()

		if self.docstatus != 1:
			frappe.throw(_("Submit the allocation before raising a pick list."))
		if self.status == "Cancelled":
			frappe.throw(_("This allocation is cancelled."))

		existing = self._existing_pick_list()
		if existing:
			frappe.throw(f"{self.name} already has a pick list: {existing}.")

		ensure_packing_link_fields()

		pick = frappe.new_doc("Order Pick List")
		pick.custom_shopify_allocation = self.name
		pick.customer = self.customer
		pick.source_warehouse = self.source_warehouse
		# Stock leaves for a customer delivery rather than into production.
		pick.purpose = "Delivery"
		pick.date_created = nowdate()
		pick.custom_address = self.shipping_address
		pick.custom_comment = f"Shopify delivery {self.delivery_index} of {self.deliveries_total or '∞'}"

		total_stems = 0
		for row in self.items:
			if not flt(row.qty):
				continue
			warehouse = row.warehouse or self.source_warehouse
			stems = self._stems_for(row)
			total_stems += stems
			pick.append(
				"locations",
				{
					"item_code": row.item_code,
					"item_name": row.item_name,
					"warehouse": warehouse,
					# Where it was available, not where the allocation defaulted to.
					"custom_source_warehouse": warehouse,
					"qty": flt(row.qty),
					"stock_qty": stems,
					"uom": row.uom,
					"actual_qty": flt(row.available_qty),
				},
			)
		if not pick.locations:
			frappe.throw(_("This allocation has no allocated quantity to pick."))

		# Data field on that site, not an Int.
		pick.custom_total_stems = cstr(total_stems)
		pick.insert(ignore_permissions=True)
		return pick.name

	@frappe.whitelist()
	def create_farm_pack_list(self):
		"""Turn this allocation's submitted pick list into a `Farm Pack List`.

		`Farm Pack List.custom_order_pick_list` is mandatory there, so the pick list has
		to exist and be submitted first. Its `pack_list_item` rows are `Dispatch Form
		Item`, which is a different shape again — bunches and stems rather than qty.
		"""
		self._require_tambuzi_packing()

		pick_name = self._existing_pick_list(submitted_only=True)
		if not pick_name:
			frappe.throw(_("Raise and submit the pick list before packing it."))

		already = frappe.db.exists(
			"Farm Pack List", {"custom_order_pick_list": pick_name, "docstatus": ["<", 2]}
		)
		if already:
			frappe.throw(f"{pick_name} is already packed on {already}.")

		pick = frappe.get_doc("Order Pick List", pick_name)
		settings = frappe.get_cached_doc("Shopify Settings")

		pack = frappe.new_doc("Farm Pack List")
		pack.custom_order_pick_list = pick_name
		pack.company = settings.default_company
		# Data fields on that site rather than Links, so the names are written straight in.
		pack.custom_customer = cstr(self.customer)
		pack.custom_customer_address = cstr(self.shipping_address)
		pack.custom_total_stems = cint(pick.custom_total_stems)

		for row in pick.locations:
			pack.append(
				"pack_list_item",
				{
					"item_code": row.item_code,
					"source_warehouse": row.custom_source_warehouse or row.warehouse,
					"bunch_uom": row.uom,
					"bunch_qty": cint(flt(row.qty)),
					"stock_qty": cint(flt(row.stock_qty)),
					"custom_number_of_stems": cint(flt(row.stock_qty)),
					"no_of_boxes": 1,
					"custom_opl_id": pick_name,
					"customer_id": self.customer,
				},
			)
		if not pack.pack_list_item:
			frappe.throw(f"{pick_name} has no picked lines to pack.")

		pack.insert(ignore_permissions=True)
		return pack.name

	def _existing_pick_list(self, submitted_only=False):
		filters = {"custom_shopify_allocation": self.name}
		filters["docstatus"] = 1 if submitted_only else ["<", 2]
		return frappe.db.exists("Order Pick List", filters)

	def _require_tambuzi_packing(self):
		"""The packing chain belongs to the Upande Tambuzi app.

		Nothing here defines those doctypes, so on a site without that app this is a
		clear message rather than a stack trace about a missing table.
		"""
		missing = [dt for dt in ("Order Pick List", "Farm Pack List") if not frappe.db.exists("DocType", dt)]
		if missing:
			frappe.throw(
				f"{', '.join(missing)} is not on this site. Picking and packing live in the "
				"Upande Tambuzi app — install it here before raising a pick list."
			)

	def _stems_for(self, row):
		"""Stems this line represents, from the Product Map's stems per box.

		Read through the item rather than the variant: the allocation records what is
		being sent, and more than one variant can map to the same box item.
		"""
		if not row.item_code:
			return 0
		stems_per_box = frappe.db.get_value(
			"Shopify Product Map", {"box_item": row.item_code, "enabled": 1}, "stems_per_box"
		)
		return cint(cint(stems_per_box) * flt(row.qty))

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

	def on_trash(self):
		"""A deleted allocation must not leave the order pointing at it.

		`Shopify Order.allocation` is a Link, so a stale pointer makes every later save
		of that order fail link validation — which is exactly what a bulk clear-out with
		`force=True` causes, since that skips the outgoing link check.
		"""
		self._refresh_order_state(exclude_self=True)

	def _refresh_order_state(self, exclude_self=False):
		"""Recompute the order's status from its live allocations.

		A multi-delivery order has one allocation per delivery, so cancelling the
		second of three must not reset the whole order — the order reflects the least
		advanced delivery still standing.
		"""
		if not self.shopify_order:
			return

		filters = [["shopify_order", "=", self.shopify_order], ["docstatus", "<", 2]]
		if exclude_self:
			# on_trash runs before the row goes, so this one is still visible to the query.
			filters.append(["name", "!=", self.name])

		siblings = frappe.get_all(
			"Shopify Allocation",
			filters=filters,
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
			frappe.throw(_("Set a Company in Shopify Settings."))

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
			frappe.throw(_("Every allocation line resolves to zero quantity."))

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


def ensure_packing_link_fields():
	"""One custom field so an Order Pick List knows which allocation raised it.

	`Order Pick List` has `sales_order` and nothing else to reference, and this
	connector raises no Sales Order — without a link there is no way to find the pick
	list for an allocation, or to stop a second one being raised. A single Link field
	is the whole footprint this app leaves on the Tambuzi app's doctype; idempotent, so
	it can be called on every use.
	"""
	if not frappe.db.exists("DocType", "Order Pick List"):
		return
	if frappe.db.exists("Custom Field", {"dt": "Order Pick List", "fieldname": "custom_shopify_allocation"}):
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Order Pick List",
			"fieldname": "custom_shopify_allocation",
			"label": "Shopify Allocation",
			"fieldtype": "Link",
			"options": "Shopify Allocation",
			"read_only": 1,
			"insert_after": "sales_order",
			"description": "Set when the pick list is raised from a Shopify allocation "
			"instead of a Sales Order.",
		}
	).insert(ignore_permissions=True)
