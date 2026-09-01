# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Floriday supply-line pricing and payload, with the network patched out.

The rate a supply line carries used to come only from the trade item's own
`Floriday Item Length` row. A mapped trade item whose row had a blank rate was
silently skipped — no supply line, no error, nothing to look at. The chain now
falls through to the post-harvest price sources, and these tests pin both the
precedence and the skip that still happens when genuinely nothing prices it.

`create_single_supply_line` is exercised against a patched `requests`, so the
payload is asserted without ever reaching Floriday.
"""

from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_supplyline import (
	create_single_supply_line,
	filter_batches_by_date_eat,
	get_item_price_from_erpnext,
	sort_batches_newest_first,
)
from ecommerce_integration.testing import IntegrationTestCase
from ecommerce_integration.tests.fixtures import (
	ensure_item,
	ensure_item_price,
	ensure_price_list,
	ensure_stem_length,
	has,
	has_per_length_item_prices,
	master_stem_lengths,
)

SUPPLY_LINE_MODULE = (
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_supplyline"
)


class TestFloridaySupplyLinePricing(IntegrationTestCase):
	def setUp(self):
		if not has("Stem Length"):
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")
		self.item = ensure_item("_Test EI Floriday Rose")
		self.price_list = ensure_price_list()
		(self.length,) = master_stem_lengths(1)

	def test_unmapped_and_unidentified_trade_item_has_no_price(self):
		self.assertIsNone(get_item_price_from_erpnext("no-such-trade-item"))

	def test_falls_back_to_the_post_harvest_master(self):
		"""An unmapped trade item can still be priced when the caller knows the item."""
		ensure_stem_length(self.length, price=0.25)
		self.assertEqual(
			get_item_price_from_erpnext("no-such-trade-item", item_code=self.item, stem_length=self.length),
			0.25,
		)

	def test_item_price_beats_the_master_for_the_same_length(self):
		if not has_per_length_item_prices():
			self.skipTest("Item Price has no custom_length field, so per-length rates cannot be set")
		ensure_stem_length(self.length, price=0.25)
		ensure_item_price(self.item, self.price_list, 0.80, stem_length=self.length)
		with patch(
			"ecommerce_integration.ecommerce_integration.utils._resolve_price_list",
			return_value=self.price_list,
		):
			rate = get_item_price_from_erpnext(
				"no-such-trade-item", item_code=self.item, stem_length=self.length
			)
		self.assertEqual(rate, 0.80)

	def test_the_trade_items_own_rate_wins_over_everything(self):
		ensure_stem_length(self.length, price=0.25)

		doc = frappe.get_doc(
			{
				"doctype": "Floriday Items",
				"item_code": self.item,
				"item_name": self.item,
				"item_group": "All Item Groups",
			}
		)
		doc.append(
			"table_ppvq",
			{"stem_length": self.length, "rate": 1.75, "trade_item_id": "_test-trade-item"},
		)
		doc.insert(ignore_permissions=True)

		self.assertEqual(get_item_price_from_erpnext("_test-trade-item"), 1.75)


class TestFloridayBatchFiltering(IntegrationTestCase):
	def test_batches_are_filtered_to_one_eat_date(self):
		"""EAT is UTC+3, so late-evening UTC already belongs to the next EAT day."""
		batches = [
			{"batchId": "same-day-utc", "batchDate": "2026-09-01T06:00:00Z"},
			{"batchId": "late-utc-yesterday", "batchDate": "2026-08-31T22:00:00Z"},
			{"batchId": "genuinely-yesterday", "batchDate": "2026-08-31T06:00:00Z"},
		]
		kept = {b["batchId"] for b in filter_batches_by_date_eat(batches, "2026-09-01")}
		self.assertEqual(kept, {"same-day-utc", "late-utc-yesterday"})

	def test_newest_batch_sorts_first(self):
		batches = [
			{"batchId": "old", "batchDate": "2026-08-30T06:00:00Z", "sequenceNumber": 1},
			{"batchId": "new", "batchDate": "2026-09-01T06:00:00Z", "sequenceNumber": 2},
		]
		self.assertEqual(sort_batches_newest_first(batches)[0]["batchId"], "new")


class TestFloridaySupplyLinePayload(IntegrationTestCase):
	def _batch(self, **overrides):
		batch = {
			"batchId": "_test-batch",
			"tradeItemId": "_test-trade-item",
			"available_pieces": 400,
			"warehouseId": "_test-warehouse",
		}
		batch.update(overrides)
		return batch

	def test_an_unpriced_batch_is_skipped_without_calling_floriday(self):
		with (
			patch(f"{SUPPLY_LINE_MODULE}.get_item_price_from_erpnext", return_value=None),
			patch(f"{SUPPLY_LINE_MODULE}.requests") as mock_requests,
		):
			result = create_single_supply_line("https://api.test", "key", "token", self._batch())

		self.assertEqual(result["status"], "skipped")
		self.assertIn("No price found", result["message"])
		mock_requests.post.assert_not_called()

	def test_a_batch_with_no_pieces_is_skipped(self):
		with patch(f"{SUPPLY_LINE_MODULE}.requests") as mock_requests:
			result = create_single_supply_line(
				"https://api.test", "key", "token", self._batch(available_pieces=0)
			)
		self.assertEqual(result["status"], "failed")
		mock_requests.post.assert_not_called()

	def test_the_posted_payload_carries_the_resolved_rate_in_eur(self):
		response = MagicMock(status_code=201)
		response.json.return_value = {}

		with (
			patch(f"{SUPPLY_LINE_MODULE}.get_item_price_from_erpnext", return_value=0.42),
			patch(f"{SUPPLY_LINE_MODULE}.requests") as mock_requests,
		):
			mock_requests.post.return_value = response
			result = create_single_supply_line("https://api.test", "key", "token", self._batch())

		self.assertEqual(result["status"], "success")
		mock_requests.post.assert_called_once()

		url = mock_requests.post.call_args[0][0]
		payload = mock_requests.post.call_args[1]["json"]
		self.assertEqual(url, "https://api.test/supply-lines")
		self.assertEqual(payload["tradeItemId"], "_test-trade-item")
		self.assertEqual(payload["numberOfPieces"], 400)
		self.assertEqual(payload["pricePerPiece"], {"currency": "EUR", "value": 0.42})
