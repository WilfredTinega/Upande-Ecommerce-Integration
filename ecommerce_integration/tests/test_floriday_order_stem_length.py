# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""An imported Floriday order has to carry its stem length.

A Floriday sales order states only its `tradeItemId`; the length lives on the
trade item's mapping row. Reading just the item code off that row left
`Sales Order Item.custom_length` blank on every imported order.

Resolving it is not a straight lookup:

* the mapping holds text ("60cm") while `custom_length` links the post-harvest
  `Stem Length` master, whose records are autonamed per farm — a hash on this
  bench — so the text is not the docname;
* Floriday grades a length by rounding DOWN to the nearest 10, so one trade item
  can cover two master lengths (60cm and 63cm both sit in grade 60).

These tests pin the grade choice and, above all, that nothing rounds: this master
holds 43cm and 63cm next to 40cm and 60cm, and a near miss files an order under
the wrong length while looking perfectly correct.
"""

from unittest.mock import patch

from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order import (
	_length_digits,
	get_stem_length_for_trade_item,
)
from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
	resolve_stem_length_name,
	stem_length_label_by_name,
	stem_length_name_by_label,
)
from ecommerce_integration.testing import IntegrationTestCase

SALES_ORDER_MODULE = (
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order"
)
ITEMS_MODULE = "ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items"
TRADE_ITEM = "22977ebf-60bb-4953-aaa6-3d68fe6bbc22"


def _rows(*lengths):
	return [{"item_code": "Tropical Amazon", "stem_length": length} for length in lengths]


class TestLengthDigits(IntegrationTestCase):
	def test_it_reads_the_number_out_of_a_length(self):
		self.assertEqual(_length_digits("63cm"), 63)
		self.assertEqual(_length_digits("60CM"), 60)
		self.assertEqual(_length_digits("60"), 60)

	def test_a_length_with_no_number_is_zero_not_an_error(self):
		for value in ("", None, "cm", "Long"):
			with self.subTest(value=value):
				self.assertEqual(_length_digits(value), 0)


class TestStemLengthForTradeItem(IntegrationTestCase):
	"""Choosing a length from the trade item's mapping rows."""

	def _resolve(self, rows, resolved="LEN-X"):
		with (
			patch(f"{ITEMS_MODULE}.get_item_lengths_for_trade_item", return_value=rows),
			patch(
				"ecommerce_integration.ecommerce_integration.utils.post_harvest.resolve_stem_length_name",
				side_effect=lambda value: f"{resolved}:{value}" if value else None,
			),
		):
			return get_stem_length_for_trade_item(TRADE_ITEM)

	def test_a_single_mapping_is_used_as_is(self):
		self.assertEqual(self._resolve(_rows("63cm")), "LEN-X:63cm")

	def test_a_grade_covering_two_lengths_uses_the_grade_itself(self):
		"""60cm and 63cm share Floriday grade 60; 60 is what Floriday stated."""
		self.assertEqual(self._resolve(_rows("63cm", "60cm")), "LEN-X:60cm")
		# Row order must not decide it.
		self.assertEqual(self._resolve(_rows("60cm", "63cm")), "LEN-X:60cm")

	def test_a_grade_with_no_round_number_falls_back_to_the_shortest(self):
		self.assertEqual(self._resolve(_rows("63cm", "65cm")), "LEN-X:63cm")

	def test_an_unmapped_trade_item_yields_no_length(self):
		self.assertIsNone(self._resolve([]))
		self.assertIsNone(get_stem_length_for_trade_item(None))

	def test_a_blank_length_on_the_row_is_ignored(self):
		self.assertIsNone(self._resolve(_rows("", None)))

	def test_a_lookup_failure_never_blocks_the_import(self):
		"""A missing length is a gap on the order, not a refused order."""
		with patch(f"{ITEMS_MODULE}.get_item_lengths_for_trade_item", side_effect=RuntimeError("boom")):
			self.assertIsNone(get_stem_length_for_trade_item(TRADE_ITEM))


class TestStemLengthResolutionIsExact(IntegrationTestCase):
	"""The resolver must never round — 63cm is not 60cm.

	Read against whatever master the site already holds rather than inserting
	fixtures: each farm autonames `Stem Length` its own way (a hash here, a
	series there, the length itself elsewhere) and validates its own fields, so a
	record made up by the test would be testing the fixture, not the lookup.
	"""

	def setUp(self):
		self.labels = stem_length_name_by_label()
		if not self.labels:
			self.skipTest("this site has no Stem Length master")

	def test_every_master_length_resolves_to_a_record_of_that_length(self):
		"""Assert the LENGTH, not the docname.

		A master can hold two records for one length — kaitet has both "57" and
		"57cm" — so which docname comes back is that site's data problem. What
		must hold is that the record it comes back with really is that length.
		"""
		by_name = stem_length_label_by_name()
		for label in sorted(self.labels):
			with self.subTest(label=label):
				resolved = resolve_stem_length_name(label)
				self.assertIsNotNone(resolved)
				self.assertEqual(by_name.get(resolved), label)

	def test_distinct_lengths_never_collapse_onto_one_record(self):
		"""63cm and 60cm are separate records; rounding would merge them."""
		names = [self.labels[label] for label in sorted(self.labels)]
		self.assertEqual(len(names), len(set(names)), f"labels share a record: {self.labels}")

	def test_it_accepts_the_shapes_the_mapping_actually_stores(self):
		"""The mapping stores "60cm"; a Link needs the real docname either way.

		MySQL collates case-insensitively, so an uppercase "37CM" matches the
		record named "37cm" — the resolver has to return the record's own name,
		not the text it was handed, or the Link points at nothing.
		"""
		by_name = stem_length_label_by_name()
		label = sorted(self.labels)[0]
		digits = label.replace("cm", "")
		for value in (label, label.upper(), digits, f" {label} "):
			with self.subTest(value=value):
				resolved = resolve_stem_length_name(value)
				self.assertIn(resolved, by_name, f"{value!r} resolved to a non-existent record")
				self.assertEqual(by_name[resolved], label)

	def test_a_length_the_master_does_not_have_returns_nothing(self):
		"""A blank Link is a visible gap; a near miss is a silent wrong answer."""
		self.assertNotIn("999cm", self.labels)
		self.assertIsNone(resolve_stem_length_name("999cm"))

	def test_nothing_in_never_guesses_something_out(self):
		for value in (None, "", "   "):
			with self.subTest(value=value):
				self.assertIsNone(resolve_stem_length_name(value))
