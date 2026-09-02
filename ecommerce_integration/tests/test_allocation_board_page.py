# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The allocation board ships with the app, not as a Web Page record.

It was first built straight onto tambuzi as a Web Page whose CSS and JS lived in
doctype fields. Nothing linted those fields, nothing diffed them, and a
re-escaped backslash in one of them turned the bunch-size regex into "a literal
backslash followed by digits" — it matched nothing, so every bunch of 12 was
counted as a single stem and a 24-stem order read as covered by 2.

These tests hold the page in the app: the assets exist, the template loads them,
and the handful of rules that were expensive to learn are still in the code.
"""

import pathlib
import re

import frappe

from ecommerce_integration.testing import IntegrationTestCase

APP = pathlib.Path(frappe.get_app_path("ecommerce_integration"))
PAGE = APP / "www" / "shopify-order-allocation"
JS = APP / "public" / "js" / "shopify_order_allocation.js"
CSS = APP / "public" / "css" / "shopify_order_allocation.css"


class TestAllocationBoardShipsWithTheApp(IntegrationTestCase):
	def test_the_page_and_its_assets_are_in_the_app(self):
		for path in (PAGE / "index.html", PAGE / "index.py", JS, CSS):
			with self.subTest(path=path.name):
				self.assertTrue(path.is_file(), f"{path} is missing")

	def test_there_is_a_way_back_to_the_desk(self):
		"""The page hides the navbar, so without this the only way out is the URL."""
		html = (PAGE / "index.html").read_text()
		self.assertIn('href="/app"', html)
		self.assertIn("sb-home", html)
		self.assertIn(".sb-home {", CSS.read_text())

	def test_the_template_loads_both_assets(self):
		html = (PAGE / "index.html").read_text()
		self.assertIn("/assets/ecommerce_integration/css/shopify_order_allocation.css", html)
		self.assertIn("/assets/ecommerce_integration/js/shopify_order_allocation.js", html)

	def test_every_element_the_script_drives_exists_in_the_template(self):
		"""A renamed id breaks the page silently — the script just finds nothing."""
		html = (PAGE / "index.html").read_text()
		js = JS.read_text()
		in_template = set(re.findall(r'id="([^"]+)"', html))
		# Ids the script creates inside the drawer and modal at runtime.
		created = {
			"dr-close",
			"dr-save",
			"dr-submit",
			"dr-fill",
			"dr-count",
			"dr-warn",
			"dr-pick",
			"dr-pack",
			"dr-packed",
			"dr-shipped",
			"dr-cancel",
			"md-no",
			"md-yes",
		}
		wanted = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
		self.assertFalse(wanted - in_template - created)

	def test_the_page_guards_its_door(self):
		context = (PAGE / "index.py").read_text()
		self.assertIn("PermissionError", context)
		self.assertIn("Shopify Allocation", context)


class TestBoardRulesSurvivedTheMove(IntegrationTestCase):
	"""The rules that cost something to get right, asserted against the source."""

	def setUp(self):
		self.js = JS.read_text()

	def test_bunch_size_is_read_with_a_character_class(self):
		r"""Not \d: this source used to travel through a JSON field, and the
		escape was mangled into a literal backslash. A character class cannot be
		broken the same way."""
		self.assertIn("match(/[0-9]+/)", self.js)
		self.assertNotIn(r"match(/(\\d+)/)", self.js)

	def test_a_bouquet_is_capped_at_two_bunches_per_variety(self):
		self.assertIn("var PER_VARIETY = 2", self.js)

	def test_an_order_cannot_be_filled_from_one_variety(self):
		self.assertIn("at least <b>two varieties</b>", self.js)

	def test_an_order_cannot_be_over_allocated(self):
		self.assertIn("function clampQty(", self.js)
		self.assertIn("The order is full", self.js)

	def test_stock_comes_from_the_apps_own_endpoint(self):
		"""Not the live site's `csr_shop_age` Server Script.

		The board read that while it was a Web Page on tambuzi. A site-local
		script is the one dependency that would keep an app-shipped page working
		nowhere else, so the rule was ported into
		`utils/shop_stock.aged_shop_stock` and the page calls that.
		"""
		self.assertIn("utils.shop_stock.aged_shop_stock", self.js)
		self.assertNotIn('method: "csr_shop_age"', self.js)

	def test_an_empty_board_repeats_the_endpoints_reason(self):
		"""A bare empty table hides a fixable cause, such as a group warehouse."""
		self.assertIn("REASON", self.js)

	def test_quantities_are_written_in_stems(self):
		"""`Shopify Allocation Item.qty` feeds a Stock Entry in the stock UOM."""
		self.assertIn("qty: b * f", self.js)

	def test_the_pipeline_runs_the_doctypes_own_methods(self):
		# `mark_packed` is deliberately absent: packed is read off the pack list,
		# not asserted through a button. See TestPackedIsWhatThePackListSays.
		for method in ("create_pick_list", "create_farm_pack_list", "mark_shipped"):
			with self.subTest(method=method):
				self.assertIn(method, self.js)
		self.assertIn('method: "run_doc_method"', self.js)

	def test_submitting_and_cancelling_both_ask_first(self):
		self.assertIn("function confirmModal(", self.js)
		self.assertIn("Are you sure you want to submit this allocation?", self.js)
		self.assertIn("Cancel this allocation and return the stock?", self.js)

	def test_orders_due_today_or_tomorrow_are_surfaced(self):
		self.assertIn("function dueBanner(", self.js)
		self.assertIn("delivered today or tomorrow", self.js)


class TestFailuresAreReadableAndLogged(IntegrationTestCase):
	"""A traceback is the right thing to keep and the wrong thing to show.

	The board used to render Frappe's `exc` straight into the page, so a missing
	Server Script filled the header with handler.py frames and a KeyError. That
	tells an operator nothing they can act on, and buries the one sentence that
	does. Failures now surface as a short toast and the detail goes to the Error
	Log.
	"""

	def setUp(self):
		self.js = JS.read_text()

	def test_no_failure_path_renders_a_raw_exception(self):
		"""`exc` is the traceback; putting it on screen is the bug being fixed."""
		self.assertNotIn("r.message || r.exc", self.js)
		self.assertNotIn("e.message || e.exc", self.js)

	def test_a_human_sentence_is_pulled_out_of_the_response(self):
		self.assertIn("function briefError(", self.js)
		# `_server_messages` is what frappe.throw was given: text meant for a person.
		self.assertIn("_server_messages", self.js)
		# And a traceback is explicitly refused rather than shown.
		self.assertIn('indexOf("Traceback")', self.js)

	def test_failures_are_shown_as_a_toast(self):
		self.assertIn("function toast(", self.js)
		self.assertIn('id="sb-toasts"', (PAGE / "index.html").read_text())
		# Selector only: prettier normalises the brace spacing in this file.
		self.assertIn(".sb-toasts", CSS.read_text())
		self.assertIn(".tst.err", CSS.read_text())

	def test_the_detail_reaches_the_error_log(self):
		self.assertIn("utils.client_log.log_client_error", self.js)

	def test_every_failure_path_goes_through_one_reporter(self):
		"""Seven of them: stock, items, allocations, pipeline, cancel, save, submit."""
		self.assertIn("function reportError(", self.js)
		self.assertGreaterEqual(self.js.count("reportError("), 7)


class TestAssetsAreCacheBusted(IntegrationTestCase):
	def test_both_asset_urls_carry_a_version(self):
		"""`public/` files have no hash in the name, so a browser keeps the old
		copy across a deploy — which is how this page went on showing a failure
		the code had already stopped producing."""
		html = (PAGE / "index.html").read_text()
		self.assertIn("shopify_order_allocation.css?v={{ asset_version }}", html)
		self.assertIn("shopify_order_allocation.js?v={{ asset_version }}", html)

	def test_the_version_moves_when_the_assets_do(self):
		import importlib.util

		spec = importlib.util.spec_from_file_location("_board_ctx", PAGE / "index.py")
		mod = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(mod)
		version = mod._asset_version()
		self.assertTrue(version)
		# It is the newest asset mtime, so deploying either file changes it.
		newest = max(int((APP / rel).stat().st_mtime) for rel in mod.ASSETS)
		self.assertEqual(version, str(newest))


class TestSubmittedOrdersMoveToTraceability(IntegrationTestCase):
	"""A submitted allocation is finished work, so it leaves the allocation list.

	Its stock is reserved and there is nothing left to allocate. What matters from
	then on is whether it is actually moving — pick list raised and submitted,
	packed, box labels printed, loaded, delivered — which is a different question
	and now a different tab.
	"""

	def setUp(self):
		self.js = JS.read_text()
		self.html = (PAGE / "index.html").read_text()

	def test_the_allocation_list_drops_submitted_orders(self):
		self.assertIn("return a.docstatus !== 1;", self.js)
		self.assertIn("function submittedAllocs()", self.js)
		self.assertIn("return a.docstatus === 1;", self.js)

	def test_the_submitted_stage_is_gone_from_the_allocation_rail(self):
		rail = self.js[self.js.index("var STAGES = [") : self.js.index("// The pipeline a")]
		self.assertNotIn("submitted", rail)
		self.assertIn("Ready to submit", rail)

	def test_both_tabs_exist(self):
		for marker in ('data-v="alloc"', 'data-v="trace"', 'id="sb-pane-trace"'):
			with self.subTest(marker=marker):
				self.assertIn(marker, self.html)

	def test_the_pipeline_covers_every_step_that_was_asked_for(self):
		steps = self.js[self.js.index("var STEPS = [") : self.js.index("// Named by what")]
		for label in (
			"Allocated",
			"Pick list",
			"Picked",
			"Packed",
			"Box labels",
			"Dispatched",
			"Delivered",
		):
			with self.subTest(label=label):
				self.assertIn(f'label: "{label}"', steps)

	def test_the_chain_is_walked_by_the_real_link_fields(self):
		"""Allocation -> Order Pick List -> Farm Pack List -> Box Label."""
		self.assertIn('["custom_shopify_allocation", "in", names]', self.js)
		self.assertIn('["custom_order_pick_list", "in", opls]', self.js)
		self.assertIn('["farm_pack_list_link", "in", fpls]', self.js)

	def test_a_read_we_are_not_allowed_reports_no_access_not_not_done(self):
		"""Farm Pack List carries its own Custom DocPerms.

		Somebody who may see this board is not necessarily allowed to see the pack
		lists, and printing "not packed" for a document we were never allowed to
		open would be a lie of exactly the kind this page exists to remove.
		"""
		self.assertIn("function blockTrace(", self.js)
		self.assertIn('blockTrace(names, "Order Pick List")', self.js)
		self.assertIn('blockTrace(Object.keys(TRACE), "Farm Pack List")', self.js)
		self.assertIn('blockTrace(Object.keys(TRACE), "Box Label")', self.js)
		self.assertIn('{ on: "unk", detail: "no access" }', self.js)

	def test_dispatch_counts_all_three_ways_stock_leaves_the_farm(self):
		"""Own truck, internal transfer and Wells Fargo are separate flags."""
		self.assertIn(
			"if (b.loaded || b.loaded_internal_transfer || b.custom_loaded_wells_fargo)",
			self.js,
		)

	def test_delivered_means_every_box_and_not_merely_one(self):
		self.assertIn("if (t.delivered >= t.labels)", self.js)
		self.assertIn("if (t.loaded >= t.labels)", self.js)

	def test_a_stalled_order_is_one_click_from_what_is_holding_it_up(self):
		self.assertIn("function docHref(", self.js)
		self.assertIn('target="_blank"'.replace('"', "'"), self.js)

	def test_every_summary_tile_has_the_same_background(self):
		"""One filled dark tile made the other four read as an afterthought.

		Only the 3px bar down the left marks a tile out now, so no variant may
		set a background on the card itself.
		"""
		css = CSS.read_text()
		for banned in (
			".sb-card.ok {",
			".sb-card.bad {",
			".sb-card.ok::after {",
			".sb-card.bad::after {",
		):
			with self.subTest(rule=banned):
				self.assertNotIn(banned, css)
		card = css[css.index(".sb-card {") :]
		self.assertIn("background: var(--wr-surface)", card[: card.index("}")])

	def test_the_pipeline_sits_beside_the_rail_not_under_it(self):
		"""`flex-basis: 100%` in a wrapping row puts the pane on its own line.

		That left the rail next to a column of blank page with the table pushed
		underneath it.
		"""
		css = CSS.read_text()
		block = css[css.index(".sb-pane.full {") :]
		block = block[: block.index("}")]
		self.assertIn("flex: 1 1 0", block)
		self.assertNotIn("100%", block)

	def test_the_chips_never_scroll_the_page_sideways(self):
		"""Seven chips stack into a grid instead of widening the row."""
		css = CSS.read_text()
		rail = css[css.index(".tk-rail {") :]
		self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", rail)
		self.assertIn("text-overflow: ellipsis", rail)


class TestPickListIsRaisedReadyToPick(IntegrationTestCase):
	"""A pick list nobody submitted is one the packhouse will not pick.

	`Farm Pack List` will not accept a draft either — `_existing_pick_list`
	demands `submitted_only`. Submitting is safe: `Order Pick List` has an empty
	controller on that site (no stock movement, no eTIMS), which is why the
	farm's own creator does `insert()` then `submit()` too.
	"""

	def setUp(self):
		self.js = JS.read_text()
		self.py = (
			APP / "ecommerce_integration" / "doctype" / "shopify_allocation" / "shopify_allocation.py"
		).read_text()

	def test_the_pick_list_is_submitted_not_left_a_draft(self):
		body = self.py[self.py.index("def create_pick_list") :]
		body = body[: body.index("\n\t@frappe.whitelist()")]
		self.assertIn("pick.submit()", body)

	def test_the_qr_is_generated_from_the_pick_lists_own_desk_url(self):
		"""Same payload the farm's own generator writes, so one scanner reads both."""
		self.assertIn("/app/order-pick-list/", self.py)
		self.assertIn("upande_tambuzi.server_scripts.opl_qr_code_gen", self.py)

	def test_a_missing_qr_library_does_not_stop_the_pick_list(self):
		"""The QR is a nicety; the document is the point."""
		helper = self.py[self.py.index("def _pick_list_qr") :]
		helper = helper[: helper.index("\nclass ")]
		self.assertIn("except ImportError:", helper)
		self.assertIn("return None", helper)

	def test_the_generator_result_is_put_back_on_the_in_memory_doc(self):
		"""It writes straight to the row, so submit() would save the stale None."""
		self.assertIn("pick.custom_qr_code = qr", self.py)

	def test_the_board_only_draws_a_qr_when_one_is_missing(self):
		"""Otherwise it would overwrite the server-drawn code after the deploy."""
		self.assertIn("if (!opl || opl.custom_qr_code) return;", self.js)

	def test_a_failed_qr_is_reported_without_losing_the_pick_list(self):
		self.assertIn("was raised, but its QR code was not generated", self.js)
		self.assertIn('reportError("generate the pick list QR code"', self.js)


class TestSubmittedAllocationIsARecord(IntegrationTestCase):
	"""Once submitted, the lines are what the Stock Entry actually moved.

	Changing a variety, length or quantity there would disagree with the
	reservation and with the pick list the packhouse is holding, so the drawer
	shows them rather than offering them.
	"""

	def setUp(self):
		self.js = JS.read_text()
		self.css = CSS.read_text()

	def test_a_submitted_allocation_is_locked(self):
		self.assertIn("var locked = a.docstatus === 1;", self.js)

	def test_the_length_picker_becomes_a_value(self):
		self.assertIn("\"<span class='dr-ro'>\" + esc(r.len", self.js)
		self.assertIn(".dr-ro", self.css)

	def test_the_quantity_input_becomes_a_value(self):
		self.assertIn("\"<td class='q'><span class='dr-ro'>\" + n(r.take)", self.js)

	def test_the_bulk_length_control_is_hidden_entirely(self):
		self.assertIn('(locked ? "" : lenSetAll(pool))', self.js)

	def test_only_the_reserved_lines_are_listed(self):
		"""The rest of the shelf is beside the point once the stock is reserved."""
		self.assertIn("if (locked) {", self.js)
		self.assertIn("return r.take > 0;", self.js)


class TestTheDrawerTableFitsItsDrawer(IntegrationTestCase):
	"""560px and five columns, so the widths are shares, not content."""

	def setUp(self):
		self.css = CSS.read_text()
		block = self.css[self.css.index(".dr-scroll {") :]
		self.block = block[: block.index("\n.sb-drawer input.qty")]

	def test_the_drawer_table_never_scrolls_sideways(self):
		self.assertIn("overflow-x: hidden", self.block)

	def test_the_columns_are_fixed_shares_of_the_drawer(self):
		self.assertIn("table-layout: fixed", self.block)
		# One share per column, and they have to add up to the drawer exactly —
		# 101% is a horizontal scrollbar.
		shares = [int(pct) for pct in re.findall(r"nth-child\(\d\)[^}]*?width: (\d+)%", self.block, re.S)]
		self.assertEqual(len(shares), 5, f"one share per column, got {shares}")
		self.assertEqual(sum(shares), 100, f"shares must add up to 100, got {shares}")

	def test_long_values_wrap_instead_of_widening_the_table(self):
		self.assertIn("overflow-wrap: anywhere", self.block)
		self.assertIn("white-space: normal", self.block)

	def test_the_quantity_input_no_longer_claims_a_fixed_width(self):
		qty = self.css[self.css.index(".sb-drawer input.qty {") :]
		qty = qty[: qty.index("}")]
		# Comments still explain the old 80px, so judge the declarations only.
		rules = re.sub(r"/\*.*?\*/", "", qty, flags=re.S)
		self.assertIn("width: 100%", rules)
		self.assertNotIn("80px", rules)
