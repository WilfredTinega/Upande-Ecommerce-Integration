# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_url, nowdate

STEM_LENGTH = "Stem Length"

# The packhouse's own bunch UOMs. A grading label carries this string verbatim and
# the pick list has to match it character for character, because the scanner
# compares `Pick List Item.uom` to it as plain text.
BUNCH_UOM_PATTERN = re.compile(r"\((\d+)\)")


def _stems_in_uom_name(uom):
	"""Stems a UOM name declares, e.g. `Bunch (12)` -> 12, else 0.

	The packing scanner reads the count out of the NAME rather than out of the
	item's UOM Conversion Detail, so this is the number that decides whether a
	pack list ever reaches 100%.
	"""
	if not uom:
		return 0
	found = BUNCH_UOM_PATTERN.search(cstr(uom))
	return cint(found.group(1)) if found else 0


def bunch_uom_for(item_code):
	"""`(uom, stems_per_bunch)` - how the packhouse handles this variety.

	The allocation stores stems, because that is how availability is counted. The
	packhouse handles BUNCHES, and the scanner matches a scanned label's bunch
	size against `Pick List Item.uom` as a literal string. So a pick list written
	in the stock UOM ("Stems") matches no label that will ever be scanned at it -
	which is exactly why Shopify pick lists could not be packed. Write the item's
	own `sales_uom`, the same UOM the allocation board counts bunches in.

	An item with no bunch UOM falls back to its stock UOM at one stem each, so a
	variety that genuinely sells by the stem stays packable rather than becoming
	unpackable.

	Raises when the item's declared conversion factor and its UOM name disagree:
	the pick list would then be written with one number and credited by the
	scanner with the other, and the pack list could never reach 100%.
	"""
	stock_uom, sales_uom = frappe.db.get_value("Item", item_code, ["stock_uom", "sales_uom"]) or (
		None,
		None,
	)
	if not sales_uom or sales_uom == stock_uom:
		return stock_uom, 1

	named = _stems_in_uom_name(sales_uom)
	declared = cint(
		flt(
			frappe.db.get_value(
				"UOM Conversion Detail", {"parent": item_code, "uom": sales_uom}, "conversion_factor"
			)
		)
	)
	if named and declared and named != declared:
		frappe.throw(
			_(
				"{0} is declared as {1} stems per {2} but its name says {3}. "
				"The packing scanner counts the name, so these have to agree."
			).format(item_code, declared, sales_uom, named)
		)

	stems = named or declared
	if stems < 1:
		# A bunch UOM nobody can count is worse than no bunch UOM: it would be
		# written on the pick list and then credited as zero stems per scan.
		return stock_uom, 1
	return sales_uom, stems


def _pick_list_qr(pick_name):
	"""Attach a QR of the pick list's own desk URL, or None if it cannot be made.

	Reuses the Upande Tambuzi generator so the code a scanner reads off a Shopify
	pick list is the same one every other pick list on that site carries.

	A nicety, not the point of the document: on a site without that app, or
	without the `qrcode` library, the pick list is still raised and submitted.
	"""
	try:
		from upande_tambuzi.server_scripts.opl_qr_code_gen import generate_qr_code
	except ImportError:
		return None

	try:
		return generate_qr_code(f"{get_url()}/app/order-pick-list/{pick_name}", pick_name)
	except Exception:
		frappe.log_error(
			title=f"Order Pick List {pick_name}: QR code not generated",
			message=frappe.get_traceback(),
		)
		return None


def _packing_state(allocation):
	"""{fpl, percent, complete} for an allocation, read straight off the pack list.

	`custom_complete` is the packhouse's own flag; the percentage is the fallback
	for a pack list where it has not been set. A missing pack list is 0% - not
	packed, rather than unknown, because nothing has been boxed yet.
	"""
	blank = {"fpl": None, "percent": 0, "complete": False}
	if not frappe.db.exists("DocType", "Farm Pack List"):
		return blank

	pick = frappe.db.get_value(
		"Order Pick List",
		{"custom_shopify_allocation": allocation, "docstatus": ["<", 2]},
		"name",
	)
	if not pick:
		return blank

	packs = frappe.get_all(
		"Farm Pack List",
		filters={"custom_order_pick_list": pick, "docstatus": ["<", 2]},
		fields=["name", "custom_completion_percentage", "custom_complete"],
		order_by="docstatus desc, creation desc",
		limit_page_length=1,
	)
	if not packs:
		return blank

	pack = packs[0]
	percent = flt(pack.custom_completion_percentage)
	return {
		"fpl": pack.name,
		"percent": percent,
		"complete": bool(cint(pack.custom_complete) or percent >= 100),
	}


def _allocation_of_pack_list(doc):
	"""The Shopify Allocation a Farm Pack List belongs to, or None.

	Every hook below is fenced on this: a farm pack list must behave exactly as it
	did before, so anything that cannot be traced back to an allocation is left
	completely alone.
	"""
	pick = doc.get("custom_order_pick_list")
	if not pick:
		return None
	allocation = frappe.db.get_value("Order Pick List", pick, "custom_shopify_allocation")
	if not allocation or not frappe.db.exists("Shopify Allocation", allocation):
		return None
	return allocation


def name_shopify_pack_list(doc, method=None):
	"""Name a Shopify pack list without a Sales Order.

	`Farm Pack List` is named on that site by a Property Setter,
	`format: {custom_abbreviation}-{custom_sales_order}`, and a Shopify pack list
	has no Sales Order - so every one of them would be named `BUR-` and the second
	would collide, inside the packing scanner, on the packhouse's second scan.

	Hooked on `autoname` rather than `before_naming` deliberately: `set_new_name`
	blanks `doc.name` after `before_naming` runs and before `autoname`, so a name
	set any earlier is thrown away. Setting it here also stops the format being
	applied at all, while a farm pack list - where this returns without setting a
	name - still goes through that format exactly as before.
	"""
	if doc.get("name") or doc.get("custom_sales_order"):
		return
	allocation = _allocation_of_pack_list(doc)
	if not allocation:
		return

	order, index = frappe.db.get_value("Shopify Allocation", allocation, ["shopify_order", "delivery_index"])
	abbr = cstr(doc.get("custom_abbreviation") or "").strip()
	stem = "-".join(part for part in (abbr, cstr(order or allocation), cstr(cint(index) or 1)) if part)
	doc.name = stem


def apply_shopify_pack_list_defaults(doc, method=None):
	"""Fill what a Shopify pack list cannot fetch from a Sales Order.

	`custom_customer`, `custom_customer_address`, `custom_comment` and
	`custom_currency` are all `fetch_from` a Sales Order on that site. With no
	Sales Order the fetch never runs - frappe skips a link field that is empty -
	so they stay blank and the printed pack list and box label say nothing about
	who the flowers are for. They are written from the allocation instead.

	`custom_farm` and `custom_abbreviation` come off the source warehouse, which is
	how the scanner already derives the farm.
	"""
	allocation = _allocation_of_pack_list(doc)
	if not allocation:
		return

	alloc = frappe.get_doc("Shopify Allocation", allocation)
	if not doc.get("custom_customer"):
		# The buying Customer, not the recipient. This lands on `Box Label.customer`,
		# which is a Link - a gift recipient is not a Customer record and writing
		# their name here fails link validation and stops the label being made. The
		# recipient travels as the consignee, which is what a consignee is.
		doc.custom_customer = cstr(alloc.customer)
	if not doc.get("custom_customer_address"):
		who = cstr(alloc.recipient_name or "").strip()
		where = cstr(alloc.shipping_address or "").strip()
		doc.custom_customer_address = " - ".join(p for p in (who, where) if p)
	if not doc.get("custom_comment"):
		doc.custom_comment = alloc.delivery_label()

	if not doc.get("custom_farm"):
		warehouse = alloc.source_warehouse or ""
		farm = warehouse.split(" ")[0] if warehouse else ""
		if farm and frappe.db.exists("Farm", farm):
			doc.custom_farm = farm
	if doc.get("custom_farm") and not doc.get("custom_abbreviation"):
		doc.custom_abbreviation = frappe.db.get_value("Farm", doc.custom_farm, "abbreviation")

	if not doc.get("custom_currency"):
		# Named in the docstring above but never actually written. A Shopify order
		# settles in whatever Shopify charged, and the pack list prints it.
		order = frappe.db.get_value("Shopify Allocation", allocation, "shopify_order")
		if order:
			doc.custom_currency = cstr(frappe.db.get_value("Shopify Order", order, "currency"))

	if not cint(doc.get("custom_picked_total_stems")):
		# What the pack list is measured against: that site computes completion as
		# `custom_total_stems / custom_picked_total_stems`, and the script that fills
		# the divisor sums a Sales Order's lines. A Shopify pack list has none, so the
		# divisor stayed empty and every packing scan died on it - either dividing by
		# zero or, when the raw Data string reached it, "unsupported operand type(s)
		# for /: 'int' and 'str'". The pick list already carries the figure.
		picked = frappe.db.get_value(
			"Order Pick List", doc.custom_order_pick_list, "custom_total_stems"
		)
		doc.custom_picked_total_stems = cint(flt(picked))

	if not doc.get("custom_delivery_point") and frappe.db.exists("DocType", "Delivery Points"):
		# A Link to a controlled list of freight agents and destinations, so a street
		# address cannot go in it; the city is the only part that can match a record.
		# The address itself travels on custom_customer_address and on each row.
		city = cstr(alloc.shipping_city or "").strip().upper()
		if city and frappe.db.exists("Delivery Points", city):
			doc.custom_delivery_point = city

	# Rows the packing scanner appends are built from the pick list, so they carry no
	# consignee, no delivery point and no farm - all three are Sales Order fetches on
	# that site. Fill them the same way, without touching a row someone edited.
	for row in doc.get("pack_list_item") or []:
		if not row.get("customer_id"):
			row.customer_id = cstr(alloc.customer)
		if not row.get("custom_consignee"):
			row.custom_consignee = cstr(alloc.recipient_name)
		if not row.get("delivery_point"):
			row.delivery_point = cstr(alloc.shipping_address)
		if not row.get("custom_source_farm") and doc.get("custom_farm"):
			row.custom_source_farm = doc.custom_farm


def carry_stem_length_to_pack_list(doc, method=None):
	"""Copy each row's stem length down from the pick list when it is missing.

	The packhouse packs TO a length, and a row that lost it cannot be graded
	against. Only ever fills an empty one, never overwrites a choice, and only on
	a pack list that belongs to an allocation.
	"""
	if not _allocation_of_pack_list(doc):
		return
	picked = {}
	for loc in frappe.get_all(
		"Pick List Item",
		filters={"parent": doc.custom_order_pick_list},
		fields=["item_code", "custom_lgth"],
	):
		if loc.custom_lgth:
			picked.setdefault(loc.item_code, loc.custom_lgth)
	for row in doc.get("pack_list_item") or []:
		if not row.get("stem_length"):
			want = picked.get(row.item_code)
			if want:
				row.stem_length = want


def sync_allocation_packed_status(doc, method=None):
	"""Farm Pack List hook: keep the allocation's status in step with packing.

	Only for pack lists that belong to a Shopify allocation - the farm's own pack
	lists have nothing to do with this doctype.
	"""
	pick = doc.get("custom_order_pick_list")
	if not pick:
		return
	allocation = frappe.db.get_value("Order Pick List", pick, "custom_shopify_allocation")
	if not allocation or not frappe.db.exists("Shopify Allocation", allocation):
		return
	frappe.get_doc("Shopify Allocation", allocation).sync_packed_status()


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

		# The packhouse packs to the length on the pick list, so a line with no
		# length is a line nobody can pack. It is asked for here rather than in
		# validate() because a draft is deliberately allowed to be half-filled.
		if frappe.db.exists("DocType", STEM_LENGTH):
			missing = [
				f"row {row.idx}: {row.item_code}"
				for row in self.items
				if flt(row.qty) > 0 and not row.get("stem_length")
			]
			if missing:
				frappe.throw(
					_("Every allocated line needs a stem length — packing works off it")
					+ ": "
					+ "; ".join(missing)
				)

		# Same rule the pick list enforces, applied where the person allocating can
		# still act on it: after submit the reservation is posted and the pick list
		# is raised in a guarded block that only logs.
		if frappe.db.exists("DocType", "Order Pick List"):
			for row in self.items:
				if not flt(row.qty):
					continue
				uom, per_bunch = bunch_uom_for(row.item_code)
				self._bunches_for(row, self._stems_for(row), uom, per_bunch)

	def on_submit(self):
		self._create_reservation()
		self._set_order_status("Allocated")
		self.db_set("status", "Allocated")
		self._auto_create_pick_list()

	def on_cancel(self):
		# Everything named here is taken down by _unwind_packing below, so the
		# back-link check must not refuse the cancel on their account.
		self.ignore_linked_doctypes = (
			"Stock Entry",
			"Order Pick List",
			"Farm Pack List",
			"Box Label",
		)
		self._unwind_packing()
		self._cancel_reservation()
		self.db_set("status", "Cancelled")
		self._refresh_order_state()
		self._reopen_for_allocation()

	# ------------------------------------------------------------------ pipeline

	@frappe.whitelist()
	def packing_state(self):
		"""How far the pack list has got: {fpl, percent, complete}.

		Read-only. Packed is not something anybody asserts on the allocation - it
		is what the Farm Pack List says, and that carries its own
		`custom_completion_percentage` / `custom_complete`, maintained as boxes
		are actually filled. A button that let someone declare an order packed
		could only ever disagree with the packhouse.
		"""
		return _packing_state(self.name)

	def sync_packed_status(self):
		"""Move the allocation to Packed once, and only once, packing is complete.

		Driven from the pack list rather than from a button - see `packing_state`.
		Called by the Farm Pack List hook, so it lands the moment the packhouse
		finishes rather than whenever someone next opens the board.
		"""
		if self.docstatus != 1 or self.status == "Cancelled":
			return self.status

		state = _packing_state(self.name)
		want = "Packed" if state["complete"] else "Allocated"

		# Shipped is further along than either; nothing here walks it back.
		if self.status in ("Shipped",):
			return self.status
		if self.status != want:
			self.db_set("status", want)
			self._set_order_status(want)
		return want

	@frappe.whitelist()
	def mark_shipped(self):
		if not _packing_state(self.name)["complete"]:
			frappe.throw(
				_("This order is not fully packed yet, so it cannot be dispatched.")
				+ " "
				+ _("Packing is {0}% done on the pack list.").format(
					cint(_packing_state(self.name)["percent"])
				)
			)
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

		pick_meta = frappe.get_meta("Pick List Item")
		pick_has_link = pick_meta.has_field("custom_lgth")
		pick_has_data = pick_meta.has_field("custom_length")

		pick = frappe.new_doc("Order Pick List")
		pick.custom_shopify_allocation = self.name
		pick.customer = self.customer
		pick.source_warehouse = self.source_warehouse
		# Stock leaves for a customer delivery rather than into production.
		pick.purpose = "Delivery"
		pick.date_created = nowdate()
		pick.custom_address = self.shipping_address
		pick.custom_comment = f"Shopify delivery {self.delivery_index} of {self.deliveries_total or '∞'}"
		# Box Label falls back to the Sales Order for these, and a Shopify order has
		# none, so the recipient is written on explicitly here and carried down.
		if frappe.get_meta("Order Pick List").has_field("custom_consignee"):
			pick.custom_consignee = cstr(self.recipient_name or self.customer)

		label = self.delivery_label()
		total_stems = 0
		for row in self.items:
			if not flt(row.qty):
				continue
			warehouse = row.warehouse or self.source_warehouse
			stems = self._stems_for(row)
			uom, per_bunch = bunch_uom_for(row.item_code)
			bunches = self._bunches_for(row, stems, uom, per_bunch)
			total_stems += stems
			pick.append(
				"locations",
				{
					"item_code": row.item_code,
					"item_name": row.item_name,
					"warehouse": warehouse,
					# Where it was available, not where the allocation defaulted to.
					"custom_source_warehouse": warehouse,
					# qty is BUNCHES and stock_qty is STEMS - what those two fields
					# mean on that site, and what the scanner counts a scan against.
					"qty": bunches,
					"stock_qty": stems,
					"uom": uom,
					"stock_uom": frappe.db.get_value("Item", row.item_code, "stock_uom"),
					# Left at 0 the row reads back a stock_qty of 0 whatever is
					# written above it, because Pick List Item redoes the maths.
					"conversion_factor": per_bunch,
					"actual_qty": flt(row.available_qty),
					# A shop order is one box. Box 1, never 0: the scanner treats a
					# falsy box id as "no box" and refuses every scan after the first.
					"custom_box_id": 1,
					"custom_box_label": label,
				},
			)
			# Two fields for the one fact on that site: `custom_lgth` is the Link
			# the pack list reads, `custom_length` the Data the printed sheet shows.
			if row.get("stem_length"):
				picked = pick.locations[-1]
				if pick_has_link:
					picked.custom_lgth = row.stem_length
				if pick_has_data:
					picked.custom_length = cstr(row.stem_length)
		if not pick.locations:
			frappe.throw(_("This allocation has no allocated quantity to pick."))

		# Data field on that site, not an Int.
		pick.custom_total_stems = cstr(total_stems)
		pick.insert(ignore_permissions=True)

		qr = _pick_list_qr(pick.name)
		if qr:
			# The generator writes `custom_qr_code` straight to the row, which bumps
			# `modified` in the database. Submitting the copy held here would then
			# fail the concurrency check outright - a TimestampMismatchError inside
			# a guarded block, so the pick list is silently left in Draft and the
			# packhouse cannot pick it. Re-read before submitting, which also picks
			# the QR up rather than saving the stale empty value back over it.
			pick.reload()

		# Submitting only flips docstatus. Order Pick List has an empty controller
		# on that site - no stock movement, no eTIMS - which is why the farm's own
		# pick-list creator submits it the same way. A draft pick list is one the
		# packhouse will not pick, and Farm Pack List will not accept it either.
		pick.submit()
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
					"stem_length": row.get("custom_lgth"),
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

	def _auto_create_pick_list(self):
		"""Raise the pick list as part of submitting, so packing can start at once.

		Guarded rather than blocking. By the time this runs the reservation Stock
		Entry is already posted, and letting a pick-list problem abort the submit
		would roll that transfer back too — a worse outcome than an allocation
		whose pick list has to be raised by hand. A site without the Upande
		Tambuzi packing doctypes simply gets nothing.

		Returns the pick list name, or None.
		"""
		if not frappe.db.exists("DocType", "Order Pick List"):
			return None
		if self._existing_pick_list():
			return None

		try:
			return self.create_pick_list()
		except Exception:
			frappe.log_error(
				title=f"Shopify Allocation {self.name}: pick list not raised",
				message=frappe.get_traceback(),
			)
			return None

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

	@frappe.whitelist()
	def delivery_label(self):
		"""Which delivery of the run this box is, e.g. `#1043 2/6`.

		A subscriber gets the same order over months, so the box has to say which
		one it is; the packhouse and the courier both work off that. It rides on
		`Pick List Item.custom_box_label`, which is the one field that survives the
		whole chain untouched - the scanner copies it onto the pack list row, the
		box label builder onto `Box Label.box_label`, and the printed label shows
		it as BOX LABEL - so nothing new has to be threaded through to carry it.

		An open-ended subscription has no total, so it is numbered without one
		rather than being labelled a fraction of a number nobody knows yet.
		"""
		ref = ""
		if self.shopify_order:
			ref = cstr(frappe.db.get_value("Shopify Order", self.shopify_order, "order_name") or "")
		ref = ref or cstr(self.shopify_order or self.name)
		index = cint(self.delivery_index) or 1
		total = cint(self.deliveries_total)
		return f"{ref} {index}/{total}" if total else f"{ref} {index}"

	def _bunches_for(self, row, stems, uom, per_bunch):
		"""Stems as whole bunches, refusing a part bunch.

		The packhouse picks bunches off a shelf; there is no half bunch to pick and
		the scanner credits a whole one per scan. A pick list asking for a part
		bunch could never be completed, so it is refused here with the line named
		rather than left to fail as an unexplained 90%-packed pack list.
		"""
		if per_bunch <= 1:
			return cint(stems)
		if cint(stems) % cint(per_bunch):
			frappe.throw(
				_(
					"Row {0}: {1} is {2} stems, which is not a whole number of {3}. "
					"The packhouse picks whole bunches - allocate a multiple of {4}."
				).format(row.idx, row.item_code, cint(stems), uom, cint(per_bunch))
			)
		return cint(cint(stems) / cint(per_bunch))

	def _stems_for(self, row):
		"""Stems this line represents.

		`row.qty` is in the line's own `uom` — whatever the allocation was made in —
		and three shapes reach here:

		- the item's own stock UOM, which is how the allocation board allocates
		  (`Stems`), so the qty already IS stems;
		- a bunch UOM like `Bunch (12)`, converted through the item's UOM
		  Conversion Detail;
		- a box item from an enabled `Shopify Product Map`, whose stems come from
		  `stems_per_box` because a box is not a UOM of the flower at all.

		Read the box through the item rather than the variant: the allocation records
		what is being sent, and more than one variant can map to the same box item.
		"""
		if not row.item_code:
			return 0

		stems_per_box = frappe.db.get_value(
			"Shopify Product Map", {"box_item": row.item_code, "enabled": 1}, "stems_per_box"
		)
		if cint(stems_per_box):
			return cint(cint(stems_per_box) * flt(row.qty))

		return cint(flt(row.qty) * self._stem_factor(row))

	def _stem_factor(self, row):
		"""Stems per unit of `row.uom`, from the item's UOM Conversion Detail.

		Falls back to 1 — never 0 — when the line is already in the stock UOM or the
		UOM is not declared on the item. A 0 here would silently zero the pick list
		and everything packed from it.
		"""
		if not row.uom:
			return 1
		if row.uom == frappe.db.get_value("Item", row.item_code, "stock_uom"):
			return 1
		factor = frappe.db.get_value(
			"UOM Conversion Detail", {"parent": row.item_code, "uom": row.uom}, "conversion_factor"
		)
		return flt(factor) or 1

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

		detail_has_length = frappe.get_meta("Stock Entry Detail").has_field("custom_stem_length")

		entry = frappe.new_doc("Stock Entry")
		entry.stock_entry_type = "Material Transfer"
		entry.purpose = "Material Transfer"
		entry.company = company
		entry.remarks = f"Subscription reservation for {self.name} ({self.delivery_date})"

		for row in self.items:
			if flt(row.qty) <= 0:
				continue
			line = {
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"s_warehouse": row.warehouse or self.source_warehouse,
				"t_warehouse": self.reserve_warehouse,
			}
			if row.get("stem_length") and detail_has_length:
				line["custom_stem_length"] = row.stem_length
			entry.append("items", line)

		if not entry.items:
			frappe.throw(_("Every allocation line resolves to zero quantity."))

		# Mark the reservation SOLD. The Tambuzi availability views read the flags
		# on the Stock Entry (`custom_sold`, `custom_moved_to_shop`, ...) to decide
		# what is still sellable, so stems committed to a Shopify order have to
		# carry it or they keep showing up as available to sell again.
		#
		# Set before insert on purpose: the field is not allow_on_submit, so it
		# cannot be written once the entry is submitted a line later.
		if frappe.get_meta("Stock Entry").has_field("custom_sold"):
			entry.custom_sold = 1

		# The header length is only true when the whole entry is one length. A
		# bouquet drawn from 53CM and 63CM stock has no single header length, and
		# writing one of them there would misreport the other.
		if frappe.get_meta("Stock Entry").has_field("custom_stem_length"):
			used = {row.stem_length for row in self.items if flt(row.qty) > 0 and row.get("stem_length")}
			if len(used) == 1:
				entry.custom_stem_length = used.pop()

		entry.insert(ignore_permissions=True)
		entry.submit()
		self.db_set("stock_entry", entry.name)

	def _unwind_packing(self):
		"""Take the whole packing trail down with the allocation.

		Cancelling returns the stems to the shop they came from, so anything
		downstream still claiming they are on their way out has to come down too -
		otherwise a box label goes on being scanned onto a truck for stock that is
		back on the shelf.

		Unwound OUTSIDE-IN, in the same order the farm's own Sales Order cascade
		uses, so each delete is already unblocked by the time it runs:
		dispatch rows -> loading sheet rows -> box labels -> packing scan logs ->
		pack list -> pick list.

		A SUBMITTED pack list is cancelled rather than refused. That is a change
		of policy, asked for deliberately: the alternative left an allocation that
		could not be cancelled at all once packing had started.
		"""
		if not frappe.db.exists("DocType", "Order Pick List"):
			return
		pick_name = self._existing_pick_list()
		if not pick_name:
			return

		packs = self._pack_lists(pick_name)
		pack_names = [p.name for p in packs]
		labels = self._box_labels(pick_name, pack_names)

		# 1. dispatch rows booked against this pick list, and the per-box rows that
		#    point at its labels - both block what follows.
		self._delete_rows("Dispatch Form Item", {"custom_opl_id": pick_name})
		if labels:
			self._delete_rows("Box", {"box_id": ["in", labels]})

		# 2. loading sheet rows carrying these boxes.
		if pack_names:
			self._delete_rows("Loading Sheet Item", {"farm_pack_list": ["in", pack_names]})
		if labels:
			self._delete_rows("Loading Sheet Item", {"box_label_link": ["in", labels]})

		# 3. the labels themselves.
		for label in labels:
			frappe.delete_doc("Box Label", label, ignore_permissions=True, force=True)

		# 4. scan logs, which link to the pack list and would block deleting it.
		self._delete_rows("Packing Scan Log", {"opl": pick_name})
		if pack_names:
			self._delete_rows("Packing Scan Log", {"fpl": ["in", pack_names]})

		# 5. pack lists: submitted cancelled, drafts removed.
		for pack in packs:
			if pack.docstatus == 1:
				doc = frappe.get_doc("Farm Pack List", pack.name)
				doc.flags.ignore_permissions = True
				doc.cancel()
			else:
				frappe.delete_doc("Farm Pack List", pack.name, ignore_permissions=True, force=True)

		# 6. and the pick list last.
		pick = frappe.get_doc("Order Pick List", pick_name)
		if pick.docstatus == 1:
			pick.flags.ignore_permissions = True
			pick.cancel()
		elif pick.docstatus == 0:
			# A draft pick list has no ledger behind it, so it is removed rather
			# than left pointing at a cancelled allocation.
			frappe.delete_doc("Order Pick List", pick_name, ignore_permissions=True, force=True)

	def _pack_lists(self, pick_name):
		if not frappe.db.exists("DocType", "Farm Pack List"):
			return []
		return frappe.get_all(
			"Farm Pack List",
			filters={"custom_order_pick_list": pick_name, "docstatus": ["<", 2]},
			fields=["name", "docstatus"],
		)

	def _box_labels(self, pick_name, pack_names):
		"""Labels reached both ways: older ones do not always carry the pack list."""
		if not frappe.db.exists("DocType", "Box Label"):
			return []
		found = []
		queries = [{"order_pick_list": pick_name}]
		if pack_names:
			queries.append({"farm_pack_list_link": ["in", pack_names]})
		for query in queries:
			for row in frappe.get_all("Box Label", filters=query, fields=["name"]):
				if row.name not in found:
					found.append(row.name)
		return found

	def _delete_rows(self, doctype, filters):
		"""Remove child rows that would otherwise block the cancel.

		Silent on a doctype this site does not have: the packing chain belongs to
		the Upande Tambuzi app and only some of it exists elsewhere.
		"""
		if not frappe.db.exists("DocType", doctype):
			return
		for row in frappe.get_all(doctype, filters=filters, fields=["name"]):
			frappe.delete_doc(doctype, row.name, ignore_permissions=True, force=True)

	def _reopen_for_allocation(self):
		"""Put the delivery back in the queue as a fresh draft.

		Cancelling means the stems went back on the shelf and the order still has
		to go out, so it belongs in the allocation list again. Raised through the
		same generator every other allocation comes from, so the replacement is
		built identically, and carried as an AMENDMENT of this one so the record of
		what was cancelled survives.

		Guarded: a cancel must not be lost because the replacement could not be
		raised - a delivery nobody re-allocated is a far smaller problem than stock
		stuck in the reserve warehouse.
		"""
		if not self.shopify_order:
			return
		try:
			from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
				create_allocations_for_order,
			)

			create_allocations_for_order(self.shopify_order)
		except Exception:
			frappe.log_error(
				title=f"{self.name}: cancelled, but not re-opened for allocation",
				message=frappe.get_traceback(),
			)

	def _cancel_reservation(self):
		"""Return the reserved stems to the warehouse they came from.

		Cancelling the Material Transfer is the reversal: ERPNext writes the
		opposite ledger entries, so the stems go back from the reserve warehouse
		to the shop they were taken from.
		"""
		if not self.stock_entry:
			return
		if not frappe.db.exists("Stock Entry", self.stock_entry):
			return
		entry = frappe.get_doc("Stock Entry", self.stock_entry)
		if entry.docstatus != 1:
			return

		entry.flags.ignore_permissions = True
		try:
			entry.cancel()
		except Exception as e:
			# This cancel IS the return of the stock, so a failure must not pass
			# quietly and leave the allocation cancelled with the stems still
			# sitting in the reserve warehouse. Typically the reserve warehouse no
			# longer holds them because something downstream already moved them.
			frappe.throw(
				_("Could not return the stock to {0}: Stock Entry {1} would not cancel. {2}").format(
					self.source_warehouse or "the shop", self.stock_entry, str(e)[:200]
				)
			)


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
