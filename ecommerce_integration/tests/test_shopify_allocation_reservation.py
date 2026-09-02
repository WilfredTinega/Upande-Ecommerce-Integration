# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Submitting an allocation reserves the stems AND marks them sold.

`_create_reservation` raises a Material Transfer from the shop warehouse to the
reserve warehouse. That alone is not enough: the Tambuzi availability views
decide what is still sellable by reading the flags on the Stock Entry
(`custom_sold`, `custom_moved_to_shop`, ...), so stems committed to a Shopify
order have to carry `custom_sold` or they keep being offered for sale again.

The flag has to be set BEFORE the entry is inserted and submitted, because the
field is not allow_on_submit — once the entry is submitted a line later it can no
longer be written through the ORM.
"""

import pathlib
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integration.ecommerce_integration.doctype.shopify_allocation.shopify_allocation import (
	ShopifyAllocation,
)
from ecommerce_integration.testing import IntegrationTestCase

ALLOCATION_MODULE = (
	"ecommerce_integration.ecommerce_integration.doctype.shopify_allocation.shopify_allocation"
)


class _FakeEntry:
	"""Records what the reservation builder set, and when."""

	def __init__(self):
		self.name = "SE-TEST-0001"
		self.items = []
		self.custom_sold = None
		self.sold_at_insert = None
		self.submitted = False

	def append(self, _field, row):
		self.items.append(row)

	def insert(self, **_kwargs):
		# Capture the flag as it stood at insert time: setting it afterwards
		# would not survive, which is the whole point.
		self.sold_at_insert = self.custom_sold

	def submit(self):
		self.submitted = True


class TestReservationMarksStockSold(IntegrationTestCase):
	def _run_reservation(self, has_sold_field=True):
		alloc = ShopifyAllocation(
			{
				"doctype": "Shopify Allocation",
				"name": "SHOP-ALL-TEST-1",
				"delivery_date": "2026-09-30",
				"source_warehouse": "Burguret Shop Available for Sale - TL",
				"reserve_warehouse": "Burguret Graded Sold - TL",
			}
		)
		alloc.stock_entry = None
		alloc.items = [frappe._dict({"item_code": "Gelatto", "qty": 12, "warehouse": None})]

		entry = _FakeEntry()
		meta = MagicMock()
		meta.has_field.return_value = has_sold_field

		with (
			patch(f"{ALLOCATION_MODULE}.frappe.new_doc", return_value=entry),
			patch(f"{ALLOCATION_MODULE}.frappe.get_meta", return_value=meta),
			patch.object(alloc, "db_set"),
			patch(
				"ecommerce_integration.ecommerce_integration.doctype.shopify_settings."
				"shopify_settings.get_shopify_settings",
				return_value=frappe._dict({"default_company": "Tambuzi Limited"}),
			),
		):
			alloc._create_reservation()
		return entry

	def test_the_reservation_entry_is_flagged_sold(self):
		entry = self._run_reservation()
		self.assertEqual(entry.custom_sold, 1)
		self.assertTrue(entry.submitted)

	def test_the_flag_is_set_before_the_entry_is_inserted(self):
		"""custom_sold is not allow_on_submit, so afterwards is too late."""
		entry = self._run_reservation()
		self.assertEqual(entry.sold_at_insert, 1)

	def test_a_site_without_the_flag_still_reserves(self):
		"""The field belongs to the Tambuzi build; elsewhere it simply is absent."""
		entry = self._run_reservation(has_sold_field=False)
		self.assertIsNone(entry.custom_sold)
		self.assertTrue(entry.submitted)

	def test_the_transfer_runs_shop_to_reserve(self):
		entry = self._run_reservation()
		self.assertEqual(len(entry.items), 1)
		row = entry.items[0]
		self.assertEqual(row["s_warehouse"], "Burguret Shop Available for Sale - TL")
		self.assertEqual(row["t_warehouse"], "Burguret Graded Sold - TL")
		self.assertEqual(row["qty"], 12)


class TestCancelReturnsTheStock(IntegrationTestCase):
	"""Cancelling an allocation must put the stems back where they came from.

	The reversal is the Material Transfer being cancelled: ERPNext writes the
	opposite ledger entries, so the stems move back from the reserve warehouse to
	the shop. Two things around that matter:

	* if that cancel will not go through, the allocation must NOT quietly end up
	  cancelled with the stock still sitting in the reserve warehouse;
	* the pick list raised on submit has to come down too, or it is left claiming
	  flowers that are back on the shelf.

	How far down the trail that goes is covered by
	`TestCancelTakesTheWholeTrailDown`.
	"""

	def _alloc(self):
		alloc = ShopifyAllocation(
			{
				"doctype": "Shopify Allocation",
				"name": "SHOP-ALL-TEST-CANCEL",
				"source_warehouse": "Burguret Shop Available for Sale - TL",
				"reserve_warehouse": "Burguret Graded Sold - TL",
			}
		)
		alloc.stock_entry = "SE-TEST-0001"
		return alloc

	def test_the_transfer_is_cancelled_so_the_stems_go_back(self):
		alloc = self._alloc()
		entry = MagicMock()
		entry.docstatus = 1
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=entry),
		):
			alloc._cancel_reservation()
		entry.cancel.assert_called_once()

	def test_a_reversal_that_will_not_go_through_is_refused_loudly(self):
		"""Otherwise the allocation reads cancelled while the stock never moved."""
		alloc = self._alloc()
		entry = MagicMock()
		entry.docstatus = 1
		entry.cancel.side_effect = Exception("Negative stock error")
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=entry),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			alloc._cancel_reservation()
		self.assertIn("Could not return the stock", str(caught.exception))

	def test_an_allocation_never_submitted_cancels_without_a_reversal(self):
		alloc = self._alloc()
		alloc.stock_entry = None
		# No stock entry means nothing was ever reserved, so nothing to give back.
		alloc._cancel_reservation()

	def test_a_draft_pick_list_is_removed(self):
		alloc = self._alloc()
		pick = MagicMock()
		pick.docstatus = 0
		removed = []
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch.object(alloc, "_existing_pick_list", return_value="OPL-TEST-1"),
			patch(f"{ALLOCATION_MODULE}.frappe.get_all", return_value=[]),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=pick),
			patch(
				f"{ALLOCATION_MODULE}.frappe.delete_doc",
				side_effect=lambda dt, dn, **k: removed.append((dt, dn)),
			),
		):
			alloc._unwind_packing()
		self.assertIn(("Order Pick List", "OPL-TEST-1"), removed)

	def test_a_submitted_pick_list_is_cancelled_not_deleted(self):
		alloc = self._alloc()
		pick = MagicMock()
		pick.docstatus = 1
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch.object(alloc, "_existing_pick_list", return_value="OPL-TEST-1"),
			patch(f"{ALLOCATION_MODULE}.frappe.get_all", return_value=[]),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=pick),
			patch(f"{ALLOCATION_MODULE}.frappe.delete_doc") as deleted,
		):
			alloc._unwind_packing()
		pick.cancel.assert_called_once()
		deleted.assert_not_called()

	def test_nothing_to_unwind_when_no_pick_list_was_raised(self):
		alloc = self._alloc()
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch.object(alloc, "_existing_pick_list", return_value=None),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc") as got,
		):
			alloc._unwind_packing()
		got.assert_not_called()


class TestStemsPerPickedLine(IntegrationTestCase):
	"""A picked line has to carry the stems it really represents.

	`row.qty` is in the allocation line's own UOM, and the pick list is what the
	packhouse works from — `stock_qty` there, and the `custom_total_stems` summed
	from it, is what ends up on the Farm Pack List. Reading the stems from the
	Shopify Product Map alone was wrong: that map is keyed on `box_item`, and the
	allocation board allocates VARIETIES, so nothing matched and every line came
	back as 0 stems — a pick list that packs as nothing at all.
	"""

	def _row(self, **overrides):
		row = {"item_code": "Gelatto", "qty": 12, "uom": "Stems"}
		row.update(overrides)
		return frappe._dict(row)

	def _alloc(self):
		return ShopifyAllocation({"doctype": "Shopify Allocation", "name": "SHOP-ALL-TEST-STEMS"})

	def _stems(self, row, *, stems_per_box=None, stock_uom="Stems", factor=None):
		"""Run `_stems_for` against a site whose lookups return these values."""

		def lookup(doctype, filters, field, *args, **kwargs):
			if doctype == "Shopify Product Map":
				return stems_per_box
			if doctype == "Item":
				return stock_uom
			if doctype == "UOM Conversion Detail":
				return factor
			raise AssertionError(f"unexpected lookup on {doctype}")

		with patch(f"{ALLOCATION_MODULE}.frappe.db.get_value", side_effect=lookup):
			return self._alloc()._stems_for(row)

	def test_a_line_already_in_stems_keeps_its_quantity(self):
		"""How the board allocates. This is the case that came back as 0."""
		self.assertEqual(self._stems(self._row()), 12)

	def test_a_bunch_line_is_multiplied_out(self):
		row = self._row(uom="Bunch (12)", qty=4)
		self.assertEqual(self._stems(row, factor=12), 48)

	def test_a_box_item_uses_its_stems_per_box(self):
		"""A box is not a UOM of the flower, so the map still wins where it applies."""
		row = self._row(item_code="SHOPIFY-BOX-A", qty=2, uom="Nos")
		self.assertEqual(self._stems(row, stems_per_box=30), 60)

	def test_an_undeclared_uom_falls_back_to_one_stem_each(self):
		"""Never 0 — that silently empties the pick list and everything packed from it."""
		row = self._row(uom="Bunch (12)")
		self.assertEqual(self._stems(row, factor=None), 12)

	def test_a_line_with_no_item_is_no_stems(self):
		self.assertEqual(self._stems(self._row(item_code=None)), 0)


class TestStemLengthReachesThePackhouse(IntegrationTestCase):
	"""The length is packed to, so it has to travel with the allocation.

	It cannot be derived: on this farm 170 of 231 variety-and-shop holdings carry
	more than one length at once (Gelatto in Burguret Shop is graded to 53CM,
	63CM and 73CM), so which length is being packed is a decision someone makes,
	not a lookup. Stem length is also a HEADER attribute of the Stock Entry that
	moved the stems in — `Stock Entry Detail.custom_stem_length` is never
	populated on that site — so Bin cannot break a holding down by length either.
	"""

	def _alloc(self, lines):
		alloc = ShopifyAllocation(
			{
				"doctype": "Shopify Allocation",
				"name": "SHOP-ALL-TEST-LEN",
				"source_warehouse": "Burguret Shop Available for Sale - TL",
				"reserve_warehouse": "Burguret Graded Sold - TL",
			}
		)
		alloc.stock_entry = None
		alloc.items = [frappe._dict(row) for row in lines]
		return alloc

	def _reserve(self, alloc, *, header_field=True):
		entry = _FakeEntry()
		entry.custom_stem_length = None
		meta = MagicMock()
		meta.has_field.return_value = header_field
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.new_doc", return_value=entry),
			patch(f"{ALLOCATION_MODULE}.frappe.get_meta", return_value=meta),
			patch.object(alloc, "db_set"),
			patch(
				"ecommerce_integration.ecommerce_integration.doctype.shopify_settings."
				"shopify_settings.get_shopify_settings",
				return_value=frappe._dict({"default_company": "Tambuzi Limited"}),
			),
		):
			alloc._create_reservation()
		return entry

	def test_each_reserved_line_carries_its_own_length(self):
		alloc = self._alloc(
			[
				{"item_code": "Gelatto", "qty": 12, "warehouse": None, "stem_length": "53CM"},
				{"item_code": "Femke", "qty": 6, "warehouse": None, "stem_length": "63CM"},
			]
		)
		entry = self._reserve(alloc)
		self.assertEqual([r.get("custom_stem_length") for r in entry.items], ["53CM", "63CM"])

	def test_the_header_length_is_set_only_when_the_whole_entry_agrees(self):
		alloc = self._alloc(
			[
				{"item_code": "Gelatto", "qty": 12, "warehouse": None, "stem_length": "53CM"},
				{"item_code": "Femke", "qty": 6, "warehouse": None, "stem_length": "53CM"},
			]
		)
		self.assertEqual(self._reserve(alloc).custom_stem_length, "53CM")

	def test_a_mixed_length_entry_gets_no_header_length(self):
		"""Writing one of two lengths there would misreport the other."""
		alloc = self._alloc(
			[
				{"item_code": "Gelatto", "qty": 12, "warehouse": None, "stem_length": "53CM"},
				{"item_code": "Femke", "qty": 6, "warehouse": None, "stem_length": "63CM"},
			]
		)
		self.assertIsNone(self._reserve(alloc).custom_stem_length)

	def test_submitting_a_line_with_no_length_is_refused(self):
		alloc = self._alloc([{"item_code": "Gelatto", "qty": 12, "warehouse": None, "stem_length": None}])
		alloc.total_qty = 12
		for row in alloc.items:
			row.idx = 1
		with (
			patch.object(alloc, "_available_qty", return_value=999),
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			alloc.before_submit()
		self.assertIn("stem length", str(caught.exception).lower())


class TestPackedIsWhatThePackListSays(IntegrationTestCase):
	"""Nobody declares an order packed; the pack list reports it.

	`Farm Pack List.custom_completion_percentage` / `custom_complete` are kept up
	to date as boxes are actually filled, so a button that let someone mark an
	allocation packed could only ever disagree with the packhouse.
	"""

	def _state(self, percent=None, complete=None, pack=True, pick=True):
		module = ALLOCATION_MODULE
		rows = (
			[
				frappe._dict(
					{
						"name": "FPL-TEST-1",
						"custom_completion_percentage": percent,
						"custom_complete": complete,
					}
				)
			]
			if pack
			else []
		)
		with (
			patch(f"{module}.frappe.db.exists", return_value=True),
			patch(f"{module}.frappe.db.get_value", return_value="OPL-TEST-1" if pick else None),
			patch(f"{module}.frappe.get_all", return_value=rows),
		):
			from ecommerce_integration.ecommerce_integration.doctype.shopify_allocation.shopify_allocation import (
				_packing_state,
			)

			return _packing_state("SHOP-ALL-TEST-PACK")

	def test_a_half_packed_order_is_not_packed(self):
		state = self._state(percent=50)
		self.assertEqual(state["percent"], 50)
		self.assertFalse(state["complete"])

	def test_a_hundred_percent_order_is_packed(self):
		self.assertTrue(self._state(percent=100)["complete"])

	def test_the_packhouses_own_flag_is_believed(self):
		"""`custom_complete` is set by the packhouse; it wins over the percentage."""
		self.assertTrue(self._state(percent=0, complete=1)["complete"])

	def test_no_pack_list_means_not_packed_rather_than_unknown(self):
		state = self._state(pack=False)
		self.assertIsNone(state["fpl"])
		self.assertFalse(state["complete"])

	def test_no_pick_list_means_nothing_has_been_packed(self):
		self.assertFalse(self._state(pick=False)["complete"])

	def test_nothing_can_declare_an_allocation_packed(self):
		"""The old free-setter is gone from both the controller and the board."""
		py = pathlib.Path(
			frappe.get_app_path(
				"ecommerce_integration",
				"ecommerce_integration",
				"doctype",
				"shopify_allocation",
				"shopify_allocation.py",
			)
		).read_text()
		self.assertNotIn("def mark_packed", py)
		js = pathlib.Path(
			frappe.get_app_path("ecommerce_integration", "public", "js", "shopify_order_allocation.js")
		).read_text()
		self.assertNotIn("mark_packed", js)
		self.assertNotIn("Mark packed", js)

	def test_dispatch_is_refused_until_packing_is_finished(self):
		alloc = ShopifyAllocation({"doctype": "Shopify Allocation", "name": "SHOP-ALL-TEST-SHIP"})
		with (
			patch(
				f"{ALLOCATION_MODULE}._packing_state",
				return_value={"fpl": "FPL-1", "percent": 40, "complete": False},
			),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			alloc.mark_shipped()
		self.assertIn("not fully packed", str(caught.exception))


class TestCancelTakesTheWholeTrailDown(IntegrationTestCase):
	"""A cancelled allocation must not leave a label being scanned onto a truck.

	The stems go back to the shop, so the dispatch rows, loading-sheet rows, box
	labels, scan logs, pack list and pick list all have to come down with it —
	the same outside-in order the farm's own Sales Order cascade uses.
	"""

	def _alloc(self):
		alloc = ShopifyAllocation(
			{
				"doctype": "Shopify Allocation",
				"name": "SHOP-ALL-TEST-CASCADE",
				"source_warehouse": "Burguret Shop Available for Sale - TL",
			}
		)
		alloc.stock_entry = "SE-TEST-1"
		return alloc

	def test_the_back_link_check_is_told_about_everything_it_unwinds(self):
		alloc = self._alloc()
		with (
			patch.object(alloc, "_unwind_packing"),
			patch.object(alloc, "_cancel_reservation"),
			patch.object(alloc, "_refresh_order_state"),
			patch.object(alloc, "db_set"),
		):
			alloc.on_cancel()
		for dt in ("Stock Entry", "Order Pick List", "Farm Pack List", "Box Label"):
			with self.subTest(doctype=dt):
				self.assertIn(dt, alloc.ignore_linked_doctypes)

	def test_a_submitted_pack_list_is_cancelled_not_refused(self):
		"""Asked for deliberately: refusing left allocations that could never cancel."""
		alloc = self._alloc()
		pack = MagicMock()
		pack.docstatus = 1
		removed = []
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch.object(alloc, "_existing_pick_list", return_value="OPL-TEST-1"),
			patch.object(
				alloc,
				"_pack_lists",
				return_value=[frappe._dict({"name": "FPL-TEST-1", "docstatus": 1})],
			),
			patch.object(alloc, "_box_labels", return_value=["BL-1", "BL-2"]),
			patch.object(alloc, "_delete_rows"),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=pack),
			patch(
				f"{ALLOCATION_MODULE}.frappe.delete_doc",
				side_effect=lambda dt, dn, **k: removed.append((dt, dn)),
			),
		):
			alloc._unwind_packing()
		pack.cancel.assert_called()
		self.assertIn(("Box Label", "BL-1"), removed)
		self.assertIn(("Box Label", "BL-2"), removed)

	def test_the_trail_is_unwound_outside_in(self):
		"""Each delete has to be unblocked by the one before it."""
		alloc = self._alloc()
		order = []
		with (
			patch(f"{ALLOCATION_MODULE}.frappe.db.exists", return_value=True),
			patch.object(alloc, "_existing_pick_list", return_value="OPL-TEST-1"),
			patch.object(alloc, "_pack_lists", return_value=[]),
			patch.object(alloc, "_box_labels", return_value=["BL-1"]),
			patch.object(alloc, "_delete_rows", side_effect=lambda dt, f: order.append(dt)),
			patch(f"{ALLOCATION_MODULE}.frappe.get_doc", return_value=MagicMock(docstatus=1)),
			patch(f"{ALLOCATION_MODULE}.frappe.delete_doc"),
		):
			alloc._unwind_packing()
		self.assertEqual(
			order,
			["Dispatch Form Item", "Box", "Loading Sheet Item", "Packing Scan Log"],
		)


class TestACancelledDeliveryComesBack(IntegrationTestCase):
	"""Cancelling puts the stems back on the shelf, so the delivery still has to go.

	It used to disappear for good: allocation names are derived from the order and
	delivery number, and the generator skipped a slot whenever that NAME existed —
	which a cancelled allocation keeps. So the order could never be allocated again.
	"""

	def setUp(self):
		self.mod = (
			"ecommerce_integration.ecommerce_integration.doctype.shopify_settings."
			"shopify_allocation_generator"
		)

	def _standing(self, rows):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			_standing_allocation,
		)

		with patch(f"{self.mod}.frappe.get_all", return_value=rows) as got:
			result = _standing_allocation("SHOP-ORD-1", 2)
		return result, got.call_args.kwargs.get("filters", {})

	def test_a_cancelled_allocation_does_not_count_as_covering_the_delivery(self):
		_result, filters = self._standing([])
		self.assertEqual(filters.get("status"), ["!=", "Cancelled"])
		self.assertEqual(filters.get("docstatus"), ["<", 2])

	def test_a_standing_allocation_still_blocks_a_duplicate(self):
		result, _filters = self._standing([frappe._dict({"name": "SHOP-ALL-X-2"})])
		self.assertEqual(result, "SHOP-ALL-X-2")

	def test_the_replacement_is_named_off_the_one_it_replaces(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			_replacement_name,
		)

		with (
			patch(f"{self.mod}.frappe.db.exists", return_value=False),
			# Not itself an amendment, so its trailing -2 is the DELIVERY number.
			patch(f"{self.mod}.frappe.db.get_value", return_value=None),
		):
			self.assertEqual(_replacement_name("SHOP-ALL-SHOP-ORD-1-2"), "SHOP-ALL-SHOP-ORD-1-2-1")

	def test_the_delivery_number_is_never_mistaken_for_a_counter(self):
		"""Stripping it would put delivery 2's replacement in delivery 1's slot."""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			_replacement_name,
		)

		with (
			patch(f"{self.mod}.frappe.db.exists", return_value=False),
			patch(f"{self.mod}.frappe.db.get_value", return_value=None),
		):
			name = _replacement_name("SHOP-ALL-SHOP-ORD-1-2")
		self.assertTrue(name.startswith("SHOP-ALL-SHOP-ORD-1-2"), name)

	def test_a_second_cancellation_does_not_stack_counters(self):
		"""`...-2-1` cancelled again gives `...-2-2`, never `...-2-1-1`."""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			_replacement_name,
		)

		taken = {"SHOP-ALL-SHOP-ORD-1-2-1"}
		with (
			patch(f"{self.mod}.frappe.db.exists", side_effect=lambda dt, n: n in taken),
			# This one IS an amendment, so its trailing -1 is a counter.
			patch(f"{self.mod}.frappe.db.get_value", return_value="SHOP-ALL-SHOP-ORD-1-2"),
		):
			self.assertEqual(_replacement_name("SHOP-ALL-SHOP-ORD-1-2-1"), "SHOP-ALL-SHOP-ORD-1-2-2")

	def test_cancelling_reopens_the_delivery_through_the_normal_generator(self):
		"""Built by the same code every other allocation comes from."""
		alloc = ShopifyAllocation({"doctype": "Shopify Allocation", "name": "SHOP-ALL-TEST-REOPEN"})
		alloc.shopify_order = "SHOP-ORD-1"
		with patch(f"{self.mod}.create_allocations_for_order") as raised:
			alloc._reopen_for_allocation()
		raised.assert_called_once_with("SHOP-ORD-1")

	def test_a_failed_reopen_never_loses_the_cancel(self):
		"""Stock stuck in the reserve warehouse is worse than a delivery to re-raise."""
		alloc = ShopifyAllocation({"doctype": "Shopify Allocation", "name": "SHOP-ALL-TEST-R2"})
		alloc.shopify_order = "SHOP-ORD-1"
		with (
			patch(
				f"{self.mod}.create_allocations_for_order",
				side_effect=Exception("generator exploded"),
			),
			patch(f"{ALLOCATION_MODULE}.frappe.log_error") as logged,
		):
			alloc._reopen_for_allocation()
		logged.assert_called_once()
