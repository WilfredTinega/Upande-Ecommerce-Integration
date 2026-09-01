# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The shared per-stem price chain both integrations resolve rates through.

Floriday supply lines and Biflorica offers used to price from `Stem Length
Price` alone — a table upande_webshop owns — and simply had no rate on a site
without that app. `resolve_stem_length_rates` widens that to the sources the
post-harvest suite already keeps, most specific winning:

    Customer pricing  >  per-length Item Price  >  flat Item Price
                      >  Stem Length.price

These tests pin that ordering, because getting it backwards does not fail
loudly: it just publishes the wrong price.
"""

import frappe

from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
	customer_pricing_rates,
	stem_length_master_rates,
	stem_length_master_values,
)
from ecommerce_integration.ecommerce_integration.utils.stem_length import (
	_normalize_stem_length,
	resolve_stem_length_rate,
	resolve_stem_length_rates,
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


class TestStemLengthNormalisation(IntegrationTestCase):
	def test_labels_collapse_to_one_canonical_form(self):
		for value in ("52cm", "52CM", "52 cm", "52", " 52CM "):
			self.assertEqual(_normalize_stem_length(value), "52cm")

	def test_unparseable_labels_are_none(self):
		self.assertIsNone(_normalize_stem_length(None))
		self.assertIsNone(_normalize_stem_length(""))
		self.assertIsNone(_normalize_stem_length("no digits here"))


class TestPostHarvestMaster(IntegrationTestCase):
	def setUp(self):
		if not has("Stem Length"):
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")
		self.short, self.long = master_stem_lengths(2)

	def test_master_rates_are_keyed_by_canonical_length(self):
		ensure_stem_length(self.short, price=0.25)
		self.assertEqual(stem_length_master_rates().get(_normalize_stem_length(self.short)), 0.25)

	def test_unpriced_lengths_are_not_reported_as_free(self):
		"""A 0 rate means "nobody priced this", never "give it away"."""
		ensure_stem_length(self.long, price=0)
		self.assertNotIn(_normalize_stem_length(self.long), stem_length_master_rates())

	def test_master_values_include_every_length_once(self):
		ensure_stem_length(self.long, price=0.35)
		values = stem_length_master_values()
		self.assertIn(_normalize_stem_length(self.long), values)
		self.assertEqual(len(values), len(set(values)))


class TestCustomerPricingRates(IntegrationTestCase):
	def test_absent_customer_yields_empty_maps(self):
		self.assertEqual(customer_pricing_rates(None), ({}, {}))

	def test_absent_doctype_degrades_instead_of_raising(self):
		if has("Customer pricing"):
			self.skipTest("Customer pricing is installed; the degradation path needs it absent")
		self.assertEqual(customer_pricing_rates("Any Customer"), ({}, {}))


class TestResolveStemLengthRates(IntegrationTestCase):
	def setUp(self):
		if not has("Stem Length"):
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")
		self.item = ensure_item("_Test EI Rose")
		self.price_list = ensure_price_list()

		lengths = master_stem_lengths(3)
		if len(lengths) < 3:
			self.skipTest("this site's Stem Length master offers fewer than three lengths")
		self.short, self.long, self.unpriced = lengths
		self.canon_short = _normalize_stem_length(self.short)
		self.canon_long = _normalize_stem_length(self.long)

		ensure_stem_length(self.short, price=0.20)
		ensure_stem_length(self.long, price=0.25)
		# Pinned to 0 explicitly: on a seeded site the master may already price
		# this length, and these tests assert what happens when it does not.
		ensure_stem_length(self.unpriced, price=0)

	def test_master_prices_every_length_when_nothing_else_does(self):
		rates = resolve_stem_length_rates(self.item, price_list=self.price_list)
		self.assertEqual(rates.get(self.canon_short), 0.20)
		self.assertEqual(rates.get(self.canon_long), 0.25)

	def test_per_length_item_price_beats_the_master(self):
		if not has_per_length_item_prices():
			self.skipTest("Item Price has no custom_length field, so per-length rates cannot be set")
		ensure_item_price(self.item, self.price_list, 0.90, stem_length=self.long)
		rates = resolve_stem_length_rates(self.item, price_list=self.price_list)
		self.assertEqual(rates.get(self.canon_long), 0.90)
		# The length the Item Price says nothing about still falls back.
		self.assertEqual(rates.get(self.canon_short), 0.20)

	def test_unpriced_lengths_are_dropped_from_the_result(self):
		self.assertNotIn(
			_normalize_stem_length(self.unpriced),
			resolve_stem_length_rates(self.item, price_list=self.price_list),
		)

	def test_a_flat_item_price_does_not_flatten_the_ladder(self):
		"""A length-agnostic rate must not price every length the same.

		It is the least specific source there is, so it fills the lengths the
		master has no rate for and yields to the master everywhere else.
		"""
		ensure_item_price(self.item, self.price_list, 0.55)
		rates = resolve_stem_length_rates(self.item, price_list=self.price_list)

		self.assertEqual(rates.get(self.canon_short), 0.20)
		self.assertEqual(rates.get(self.canon_long), 0.25)
		# The master says nothing about this one, so the flat rate fills it.
		self.assertEqual(rates.get(_normalize_stem_length(self.unpriced)), 0.55)

	def test_no_item_code_resolves_to_nothing(self):
		self.assertEqual(resolve_stem_length_rates(None), {})


class TestResolveSingleRate(IntegrationTestCase):
	def setUp(self):
		if not has("Stem Length"):
			self.skipTest("Stem Length (post-harvest master) is not installed on this site")
		self.item = ensure_item("_Test EI Single Length Rose")
		self.price_list = ensure_price_list()
		self.short, self.long = master_stem_lengths(2)

	def test_matching_length_wins(self):
		ensure_stem_length(self.short, price=0.20)
		ensure_stem_length(self.long, price=0.25)
		# Deliberately spelled differently from the master: the lookup is canonical.
		spelled_differently = f"{_normalize_stem_length(self.long)[:-2]} cm"
		self.assertEqual(
			resolve_stem_length_rate(self.item, spelled_differently, price_list=self.price_list),
			0.25,
		)

	def test_a_single_priced_length_answers_an_unknown_length(self):
		"""Bin stock carries no stem length; a one-length item is still unambiguous."""
		frappe.db.delete("Stem Length")
		ensure_stem_length(self.short, price=0.42)
		self.assertEqual(resolve_stem_length_rate(self.item, None, price_list=self.price_list), 0.42)

	def test_an_ambiguous_unknown_length_returns_none(self):
		frappe.db.delete("Stem Length")
		ensure_stem_length(self.short, price=0.20)
		ensure_stem_length(self.long, price=0.25)
		self.assertIsNone(resolve_stem_length_rate(self.item, None, price_list=self.price_list))
