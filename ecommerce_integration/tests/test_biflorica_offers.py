# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Biflorica offer sourcing and payload building.

Two behaviours are load-bearing and were previously untested:

  * where the offer rows come from. Rows enabled on the Stock tab
    (`Ecommerce Enabled Stock`) win; with nothing enabled the builder falls back
    to whatever is physically on the shelves, instead of returning nothing at
    all — which is what made the Offers tab permanently empty before.
  * that a zero-priced item is *reported* as skipped rather than quietly
    dropped, so an operator can see which items need a rate.

Nothing here touches the network: `post_all_items_to_biflorica` is exercised
with `requests` patched out, and the sourcing helpers make no calls at all.
"""

from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
	_enabled_offer_rows,
	get_enabled_offer_items,
	get_item_price,
	prepare_offers_payload_with_details,
	round_to_nearest_tens,
)
from ecommerce_integration.ecommerce_integration.utils.stem_length import _normalize_stem_length
from ecommerce_integration.testing import IntegrationTestCase
from ecommerce_integration.tests.fixtures import (
	ensure_enabled_stock,
	ensure_item,
	ensure_item_price,
	ensure_price_list,
	ensure_shelf,
	ensure_stem_length,
	has,
	has_stem_length_master,
)

SHELF_ID = "_TEST-EI-BIFLORICA-SHELF"


class TestBifloricaStemLengthRounding(IntegrationTestCase):
	def test_lengths_round_to_the_nearest_ten(self):
		self.assertEqual(round_to_nearest_tens(52), 50)
		self.assertEqual(round_to_nearest_tens(56), 60)
		self.assertEqual(round_to_nearest_tens(55), 60)


class TestBifloricaOfferSourcing(IntegrationTestCase):
	def setUp(self):
		if not has("Shelf", "Shelf Item"):
			self.skipTest("Shelf / Shelf Item are not installed on this site")
		self.item = ensure_item("_Test EI Biflorica Rose")
		self.price_list = ensure_price_list()

	def tearDown(self):
		if frappe.db.exists("Shelf", SHELF_ID):
			frappe.delete_doc("Shelf", SHELF_ID, ignore_permissions=True, force=True)

	def test_nothing_enabled_offers_nothing(self):
		"""The shelf is NOT a fallback.

		Post Offers used to fall back to raw shelf stock when nothing was enabled,
		which offered every variety and length on the list instead of the few an
		operator had chosen.
		"""
		ensure_shelf(SHELF_ID, [(self.item, "50cm", 500), (self.item, "60cm", 500)])

		# Scoped to this test's item: a real site has its own enabled rows, and
		# asserting the global set is empty makes the test depend on site state.
		self.assertEqual([r for r in _enabled_offer_rows() if r.item_code == self.item], [])
		self.assertEqual([o for o in get_enabled_offer_items() if o["item_code"] == self.item], [])

	def test_only_the_enabled_lengths_are_offered(self):
		"""Shelf stock that was not enabled must not be offered."""
		ensure_shelf(
			SHELF_ID,
			[(self.item, "40cm", 500), (self.item, "50cm", 500), (self.item, "60cm", 500)],
		)
		ensure_enabled_stock(self.item, [("50cm", 300, 1)])

		offers = [o for o in get_enabled_offer_items() if o["item_code"] == self.item]
		self.assertEqual([_normalize_stem_length(o["stem_length"]) for o in offers], ["50cm"])
		# The ENABLED qty, not the 500 sitting on the shelf.
		self.assertEqual(offers[0]["actual_qty"], 300)

	def test_enabled_stock_is_priced_from_the_post_harvest_chain(self):
		if not has_stem_length_master():
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")
		ensure_stem_length("50CM", price=0.25)
		ensure_enabled_stock(self.item, [("50cm", 200, 1)])

		offers = [i for i in get_enabled_offer_items() if i["item_code"] == self.item]
		self.assertEqual(len(offers), 1)
		self.assertEqual(offers[0]["actual_qty"], 200)
		self.assertEqual(offers[0]["price_per_stem"], 0.25)

	def test_disabled_rows_are_not_offered(self):
		ensure_enabled_stock(self.item, [("50cm", 25, 0)])
		self.assertEqual([r for r in _enabled_offer_rows() if r.item_code == self.item], [])

	def test_enabled_rows_feed_the_panel_status_column(self):
		"""The picker's Status column needs the flag AND the enabled qty.

		`shelf_move.js` renders "Enabled &middot; <qty>" from these two, and steps
		the qty input by bunch_size, so all three have to come back.
		"""
		from ecommerce_integration.ecommerce_integration.utils.stock_picker import (
			get_enabled_stock_rows,
		)

		ensure_enabled_stock(self.item, [("50cm", 300, 1), ("60cm", 120, 0)])
		rows = {r["stem_length"]: r for r in get_enabled_stock_rows() if r["item_code"] == self.item}

		# Only the enabled length is offered to the panel as enabled.
		self.assertEqual(sorted(rows), ["50cm"])
		self.assertEqual(rows["50cm"]["stock_qty"], 300)
		self.assertGreaterEqual(rows["50cm"]["bunch_size"], 1)

	def test_enabled_rows_carry_no_rate_of_their_own(self):
		"""Availability only — the price must come from the Item Price chain."""
		ensure_enabled_stock(self.item, [("50cm", 25, 1)])
		rows = [r for r in _enabled_offer_rows() if r.item_code == self.item]
		self.assertEqual(len(rows), 1)
		self.assertIsNone(rows[0].rate)


class TestDealDeduplication(IntegrationTestCase):
	"""One Biflorica id must never produce two Sales Orders.

	Approving a predeal does not mint a new id — predeal 25 simply moves out of
	/deals/predeal and into /deals as deal 25. Namespacing the po_no per kind and
	only checking the current kind's namespace imported it twice: once as
	BIFLORICA-PREDEAL-25, then again as BIFLORICA-25.
	"""

	def test_a_predeal_and_its_approved_deal_share_a_lookup(self):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			deal_po_ref,
			deal_po_refs,
		)

		self.assertEqual(deal_po_ref(25, "predeal"), "BIFLORICA-PREDEAL-25")
		self.assertEqual(deal_po_ref(25, "deal"), "BIFLORICA-25")
		# Whichever kind is being imported, BOTH refs are checked.
		self.assertEqual(set(deal_po_refs(25)), {"BIFLORICA-25", "BIFLORICA-PREDEAL-25"})

	def test_distinct_ids_do_not_collide(self):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			deal_po_refs,
		)

		self.assertFalse(set(deal_po_refs(25)) & set(deal_po_refs(26)))


class TestPlatformHostValidation(IntegrationTestCase):
	"""Base URL and Platform must agree, because disagreeing fails silently.

	Biflorica runs one host per platform. The wrong host authenticates, serves
	GET /offers and validates a posted payload, then discards the create and
	answers 200 with an empty body — so nothing surfaces the mistake.
	"""

	def _check(self, platform, base_url):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
			platform_host_mismatch,
		)

		return platform_host_mismatch(frappe._dict({"platform": platform, "base_url": base_url}))

	def test_matching_host_and_platform_pass(self):
		self.assertIsNone(self._check("Kenya", "https://ke.term.apitest.biflorica.com/apiv3"))
		self.assertIsNone(self._check("Ecuador", "https://ec.term.apitest.biflorica.com/apiv3"))

	def test_a_kenya_platform_on_the_ecuador_host_is_flagged(self):
		message = self._check("Kenya", "https://ec.term.apitest.biflorica.com/apiv3")
		self.assertIsNotNone(message)
		self.assertIn("Base URL", message)
		self.assertIn("Ecuador", message)
		# Names the URL it should have been.
		self.assertIn("ke.term.apitest.biflorica.com", message)

	def test_the_reverse_mismatch_is_flagged_too(self):
		self.assertIsNotNone(self._check("Ecuador", "https://ke.term.apitest.biflorica.com/apiv3"))

	def test_an_unknown_platform_is_left_alone(self):
		"""Never block a region this map has not been confirmed against."""
		self.assertIsNone(self._check("Colombia", "https://co.term.apitest.biflorica.com/apiv3"))

	def test_an_unrecognised_host_is_left_alone(self):
		"""A proxy or local mirror is not evidence of a wrong platform."""
		self.assertIsNone(self._check("Kenya", "https://proxy.internal.example/apiv3"))

	def test_incomplete_settings_are_left_alone(self):
		self.assertIsNone(self._check("Kenya", ""))
		self.assertIsNone(self._check("", "https://ec.term.apitest.biflorica.com/apiv3"))


class TestBifloricaFailureReporting(IntegrationTestCase):
	"""A rejected SETTING must be reported once, naming the setting.

	Biflorica answers an unresolvable account-level value (the farm) with
	`result: "error"` and the same `farm: Not parsed Farms` against every offer in
	the request. Echoing that per variety produced "All 5 offer(s) failed" plus
	five identical lines, none of which said what to change.
	"""

	def test_one_reason_shared_by_every_failure_is_collapsed(self):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			_shared_failure_reason,
		)

		failed = [{"variety": "Primio", "reason": "farm: Not parsed Farms"}] * 3
		self.assertEqual(_shared_failure_reason(failed, 3), "farm: Not parsed Farms")

	def test_a_partial_failure_is_left_per_offer(self):
		"""Some offers failing is genuinely per-offer; do not hide the detail."""
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			_shared_failure_reason,
		)

		failed = [{"variety": "Primio", "reason": "farm: Not parsed Farms"}]
		self.assertIsNone(_shared_failure_reason(failed, 3))

	def test_differing_reasons_are_left_per_offer(self):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			_shared_failure_reason,
		)

		failed = [
			{"variety": "Primio", "reason": "farm: Not parsed Farms"},
			{"variety": "Patz", "reason": "variety: Not parsed Variety"},
		]
		self.assertIsNone(_shared_failure_reason(failed, 2))

	def test_a_farm_rejection_names_the_setting_and_the_value(self):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			_settings_hint_for,
		)

		hint = _settings_hint_for("farm: Not parsed Farms", frappe._dict({"farm": "S4_1FLOW"}))
		self.assertIn("S4_1FLOW", hint)
		self.assertIn("Farm", hint)

	def test_an_unmapped_reason_yields_no_settings_hint(self):
		"""A per-offer field error must not be blamed on a setting."""
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting import (
			_settings_hint_for,
		)

		self.assertIsNone(
			_settings_hint_for("variety: Not parsed Variety", frappe._dict({"farm": "S4_1FLOW"}))
		)


class TestBifloricaResponseTrust(IntegrationTestCase):
	"""An accepted request is not a created offer.

	Biflorica answers 200 with a ZERO-LENGTH text/html body when the payload
	validates but the offer is not created — nothing reaches the marketplace. The
	code used to read "200, no error string" as success and reported
	"Posted N offer(s)" for offers that never existed.
	"""

	MODULE = "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer"

	def _post(self, status, body):
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
			post_to_biflorica_api,
		)

		response = MagicMock(status_code=status)
		response.text = body
		settings = frappe._dict({"base_url": "https://api.test", "access_token": "t", "platform": "Kenya"})
		payload = {"data": [{"variety": "Primio"}], "countAll": "1"}
		with patch(f"{self.MODULE}.requests") as mock_requests:
			mock_requests.post.return_value = response
			return post_to_biflorica_api(payload, settings)

	def test_an_empty_body_is_not_success(self):
		for body in ("", "   ", "\n"):
			result = self._post(200, body)
			self.assertFalse(result["success"], f"empty body {body!r} reported as success")
			self.assertIn("nothing was created", result["message"].lower())

	def test_an_empty_body_points_at_the_base_url(self):
		"""The real cause was a Base URL on the wrong platform host.

		That host authenticates, serves GET /offers and validates the payload, then
		silently discards the create — so the message has to name Base URL.
		"""
		message = self._post(200, "")["message"]
		self.assertIn("Base URL", message)
		self.assertIn("https://api.test", message)
		self.assertIn("Kenya", message)

	def test_a_confirming_body_is_success(self):
		result = self._post(200, '[{"result":"ok","id":"abc"}]')
		self.assertTrue(result["success"])

	def test_a_validation_body_is_failure(self):
		result = self._post(200, '[{"result":"not_validate","errors":{"box":["Not parsed Box type"]}}]')
		self.assertFalse(result["success"])


class TestNoWebshopSettingsDependency(IntegrationTestCase):
	"""This app must not read `Webshop Settings` at all — it belongs to upande_webshop.

	The price list and the order-as-quotation toggle live on this app's own
	channel Singles instead. Asserted against the source because the symptom of a
	regression is invisible on a webshop site and only shows up elsewhere as
	"DocType Webshop Settings not found" dialogs.
	"""

	def test_no_module_references_webshop_settings_in_code(self):
		import pathlib

		import ecommerce_integration

		root = pathlib.Path(ecommerce_integration.__file__).parent
		offenders = []
		for path in root.rglob("*.py"):
			if "__pycache__" in path.parts or path.parent.name == "tests":
				continue
			for lineno, line in enumerate(path.read_text().splitlines(), 1):
				if "Webshop Settings" not in line:
					continue
				# Prose in a docstring or comment explaining the ban is fine.
				stripped = line.strip()
				if stripped.startswith("#") or '"' not in line.split("Webshop Settings")[0][-2:]:
					continue
				offenders.append(f"{path.relative_to(root)}:{lineno}: {stripped}")

		self.assertEqual(offenders, [], "Webshop Settings is referenced in code:\n" + "\n".join(offenders))

	def test_channel_settings_own_the_configuration(self):
		"""Asserted against the shipped JSON, not the live site.

		A site that has not migrated yet is not an app defect; what matters is
		that the app declares the fields it now reads instead of Webshop Settings.
		"""
		import json
		import pathlib

		import ecommerce_integration

		root = pathlib.Path(ecommerce_integration.__file__).parent
		for folder, doctype in (
			("biflorica_setting", "Biflorica Setting"),
			("floriday_settings", "Floriday Settings"),
		):
			path = root / "ecommerce_integration" / "doctype" / folder / f"{folder}.json"
			fieldnames = {f.get("fieldname") for f in json.loads(path.read_text())["fields"]}
			self.assertIn("price_list", fieldnames, f"{doctype} declares no price_list")
			self.assertIn(
				"create_orders_as_quotation",
				fieldnames,
				f"{doctype} declares no create_orders_as_quotation",
			)


class TestNoQueuedMessages(IntegrationTestCase):
	"""Reading an absent sibling doctype must not leave a message behind.

	`frappe.get_meta` / `get_cached_doc` on a doctype the site does not have push
	"DocType <x> not found" onto `frappe.message_log` BEFORE they raise, so code
	that wraps the read in try/except swallows the exception and still leaves the
	message queued. On the Biflorica Stock tab that produced one identical dialog
	per row priced — 40 of them. Existence has to be checked, not caught.
	"""

	def _assert_silent(self, label, fn):
		frappe.message_log = []
		fn()
		messages = list(frappe.message_log or [])
		self.assertEqual(messages, [], f"{label} queued {len(messages)} message(s): {messages[:3]}")

	def test_price_list_resolution_is_silent(self):
		from ecommerce_integration.ecommerce_integration.utils import _resolve_price_list

		# Memoized per request; clear it so this exercises a real resolve.
		frappe.local._ei_price_list = None
		self._assert_silent("_resolve_price_list", _resolve_price_list)
		frappe.local._ei_price_list = None
		self._assert_silent("_resolve_price_list(channel)", lambda: _resolve_price_list("Biflorica Setting"))

	def test_quotation_toggle_is_silent(self):
		from ecommerce_integration.ecommerce_integration.utils import create_orders_as_quotation

		self._assert_silent(
			"create_orders_as_quotation", lambda: create_orders_as_quotation("Biflorica Setting")
		)

	def test_shelf_stock_flag_is_silent(self):
		from ecommerce_integration.ecommerce_integration.utils.shelf_stock import shelf_stock_enabled

		self._assert_silent("shelf_stock_enabled", lambda: shelf_stock_enabled("Biflorica Setting"))

	def test_stock_and_offer_tabs_are_silent(self):
		from ecommerce_integration.ecommerce_integration.utils.stock_picker import (
			get_enabled_stock_rows,
			get_shelf_rows,
			get_warehouse_rows,
		)

		self._assert_silent("get_shelf_rows", get_shelf_rows)
		self._assert_silent("get_warehouse_rows", get_warehouse_rows)
		self._assert_silent("get_enabled_stock_rows", get_enabled_stock_rows)
		self._assert_silent("get_enabled_offer_items", get_enabled_offer_items)


class TestBifloricaItemPrice(IntegrationTestCase):
	def setUp(self):
		self.item = ensure_item("_Test EI Biflorica Priced Rose")
		self.price_list = ensure_price_list()

	def test_any_item_price_is_better_than_no_price(self):
		"""A farm that prices only on its own list must not offer at zero."""
		ensure_item_price(self.item, self.price_list, 0.75)
		self.assertGreater(get_item_price(self.item, price_list=self.price_list), 0)

	def test_unpriced_item_reports_zero(self):
		self.assertEqual(get_item_price("_Test EI Nonexistent Item"), 0)


class TestBifloricaOfferPayload(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe._dict({"farm": "TESTFARM (KE)", "platform": "test", "warehouse": None})

	def _item(self, **overrides):
		item = {
			"item_code": "_Test EI Payload Rose",
			"item_name": "_Test EI Payload Rose",
			"actual_qty": 300,
			"stem_length": "50cm",
			"price_per_stem": 0.25,
		}
		item.update(overrides)
		return item

	def test_zero_priced_items_are_reported_not_silently_dropped(self):
		_payload, details = prepare_offers_payload_with_details(
			[self._item(price_per_stem=0)], self.settings, packrate=300
		)
		skipped = [d for d in details if d.get("status") == "skipped"]
		self.assertEqual(len(skipped), 1)
		self.assertIn("price", skipped[0]["reason"].lower())

	def test_priced_items_carry_a_per_stem_and_a_pack_price(self):
		payload, _details = prepare_offers_payload_with_details([self._item()], self.settings, packrate=300)
		rows = payload.get("data") or []
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["pricePerStem"], "0.25")
		# 300 stems at 0.25 -> 75.00 for the pack.
		self.assertEqual(float(rows[0]["price"]), 75.0)

	def test_lengths_of_one_variety_become_ONE_box_offer(self):
		"""Biflorica's model is one box spanning many lengths, not one per length.

		Mirrors live offer 74: parallel slash-separated lists, `packing` as the
		nominal stems per box, and `quantity` in BOXES.
		"""
		items = [
			self._item(stem_length="40cm", price_per_stem=0.20, actual_qty=200),
			self._item(stem_length="50cm", price_per_stem=0.25, actual_qty=200),
			self._item(stem_length="60cm", price_per_stem=0.30, actual_qty=200),
		]
		payload, _details = prepare_offers_payload_with_details(
			items, self.settings, box_type="JUM", packrate=150
		)
		rows = payload["data"]
		self.assertEqual(len(rows), 1, "three lengths of one variety must be one offer")

		offer = rows[0]
		self.assertEqual(offer["size"], "40/50/60")
		self.assertEqual(offer["pricePerStem"], "0.20/0.25/0.30")
		# 150 stems split evenly across three lengths.
		self.assertEqual(offer["sizesStems"], "50/50/50")
		self.assertEqual(offer["packing"], 150)
		# 200 stems of each length / 50 per box = 4 whole boxes.
		self.assertEqual(offer["quantity"], "4.0")
		# Box price = 50 * (0.20 + 0.25 + 0.30).
		self.assertEqual(offer["price"], "37.50")

	def test_the_scarcest_length_caps_the_box_count(self):
		"""Every box needs all its lengths, so the thinnest one is the limit."""
		items = [
			self._item(stem_length="40cm", price_per_stem=0.20, actual_qty=1000),
			self._item(stem_length="50cm", price_per_stem=0.25, actual_qty=120),
		]
		payload, _ = prepare_offers_payload_with_details(items, self.settings, box_type="JUM", packrate=100)
		# 50 stems of each per box; 120 / 50 = 2 boxes, not 1000 / 50 = 20.
		self.assertEqual(payload["data"][0]["quantity"], "2.0")

	def test_too_little_stock_for_a_whole_box_is_skipped_with_a_reason(self):
		items = [
			self._item(stem_length="40cm", price_per_stem=0.20, actual_qty=10),
			self._item(stem_length="50cm", price_per_stem=0.25, actual_qty=10),
		]
		payload, details = prepare_offers_payload_with_details(
			items, self.settings, box_type="JUM", packrate=200
		)
		self.assertEqual(payload["data"], [])
		skipped = [d for d in details if d["status"] == "skipped"]
		self.assertEqual(len(skipped), 1)
		self.assertIn("full box", skipped[0]["reason"])

	def test_the_removed_minimum_field_is_not_sent(self):
		"""The live offer carries no `minimum`, so neither may we."""
		payload, _ = prepare_offers_payload_with_details(
			[self._item()], self.settings, packrate=300, minimum=5
		)
		self.assertNotIn("minimum", payload["data"][0])

	def test_no_network_call_is_made_while_building_a_payload(self):
		with patch(
			"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting."
			"biflorica_customer_offer.requests"
		) as mock_requests:
			prepare_offers_payload_with_details([self._item()], self.settings, packrate=300)
		mock_requests.post.assert_not_called()
		mock_requests.get.assert_not_called()
