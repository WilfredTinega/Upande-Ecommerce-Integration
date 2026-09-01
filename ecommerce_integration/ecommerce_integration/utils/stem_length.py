# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Generic stem-length + per-length pricing helpers.

These are channel-agnostic: they normalize stem-length labels ("52CM"/"52 cm"
/"52" -> "52cm") and read per-length Item Price rates for an item, whether the
item carries per-length `custom_length` Item Price rows or is a variant template.

They read only ERPNext Item Price / Item Attribute data (no custom-app
doctypes), and are vendored into this app so the Floriday/Biflorica integration
carries no import dependency on upande_webshop.
"""

import re

import frappe


def _normalize_stem_length(value):
	if value is None:
		return None
	m = re.search(r"\d+", str(value))
	if not m:
		return None
	return f"{int(m.group(0))}cm"


def _item_price_detail(item_code, price_list):
	"""Split one item's Item Price rows into ``(per_length_rates, flat_rate)``.

	`per_length_rates` is {canonical_stem_length: rate} from rows that name a
	`custom_length`; `flat_rate` is the single length-agnostic rate, or None.
	They are returned apart because they are not equally specific and callers
	order them differently — see `resolve_stem_length_rates`.
	"""
	if not price_list:
		return {}, None

	has_length_col = frappe.db.has_column("Item Price", "custom_length")
	fields = ["price_list_rate"] + (["custom_length"] if has_length_col else [])
	rows = frappe.get_all(
		"Item Price", filters={"item_code": item_code, "price_list": price_list}, fields=fields
	)
	if not rows:
		return {}, None

	per_length = {}
	if has_length_col:
		# `custom_length` Links to the post-harvest master, whose autoname differs
		# per farm — resolve through it rather than reading digits off a docname.
		from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
			canonical_stem_length,
		)

		for row in rows:
			stem_length = canonical_stem_length(row.custom_length)
			if not stem_length:
				continue
			per_length[stem_length] = row.price_list_rate

	flat_rate = None
	for row in rows:
		if has_length_col and row.get("custom_length"):
			continue
		flat_rate = row.price_list_rate
		break
	if flat_rate is None and not per_length:
		flat_rate = rows[0].price_list_rate

	return per_length, flat_rate


def _item_price_rates_for_list(item_code, price_list):
	"""Return {canonical_stem_length: rate} for one item on one price list.

	Non-variant items differentiate per-length Item Price rows via the
	custom_length field. On sites that ship the Custom Field, read it. On sites
	without it, fall back to the single Item Price rate and apply it to every
	Stem Length master value. Returns {} if the price list yields no usable rate.
	"""
	latest_rate, flat_rate = _item_price_detail(item_code, price_list)

	if latest_rate:
		return latest_rate
	if flat_rate is None:
		return {}

	# Imported here, not at module scope: post_harvest imports _normalize_stem_length
	# from this module, so a top-level import would be circular.
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		stem_length_master_values,
	)

	master_lengths = stem_length_master_values()
	if not master_lengths:
		return {}

	for canon in master_lengths:
		latest_rate[canon] = flat_rate
	return latest_rate


def _stem_length_rates_from_item_prices(item_code, price_list, fallback_price_list=None):
	"""Per-length rates for a non-variant item.

	`price_list` is the primary (e.g. a Customer Price List chosen in the sync
	dialog). `fallback_price_list` (typically the configured Item price list)
	fills in any stem length the primary list has no rate for — a per-length
	fallback, so each length resolves independently. When the two are the same
	(or no fallback given), this behaves exactly as before.
	"""
	primary = _item_price_rates_for_list(item_code, price_list)
	if not fallback_price_list or fallback_price_list == price_list:
		return primary

	fallback = _item_price_rates_for_list(item_code, fallback_price_list)
	if not fallback:
		return primary

	# Per-length fallback: start from fallback, override with whatever the
	# primary list provides.
	merged = dict(fallback)
	merged.update(primary)
	return merged


def _stem_length_rates_from_variants(template_item_code, price_list):
	master_lengths = frappe.get_all(
		"Item Attribute Value",
		filters={"parent": "Stem Length"},
		fields=["attribute_value"],
		order_by="idx",
	)
	if not master_lengths:
		return {}

	variants = frappe.get_all(
		"Item",
		filters={"variant_of": template_item_code, "disabled": 0},
		pluck="name",
	)
	if not variants:
		return {}

	attr_rows = frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": ["in", variants], "attribute": "Stem Length"},
		fields=["parent", "attribute_value"],
	)
	variant_by_norm_length = {}
	for r in attr_rows:
		norm = _normalize_stem_length(r.attribute_value)
		if norm:
			variant_by_norm_length[norm] = r.parent

	if not variant_by_norm_length:
		return {}

	variant_codes = list(variant_by_norm_length.values())
	price_filters = {"item_code": ["in", variant_codes]}
	if price_list:
		price_filters["price_list"] = price_list
	price_rows = frappe.get_all(
		"Item Price",
		filters=price_filters,
		fields=["item_code", "price_list_rate"],
	)
	rate_by_variant = {r.item_code: r.price_list_rate for r in price_rows}

	latest_rate = {}
	for ml in master_lengths:
		canonical = ml.attribute_value
		variant_code = variant_by_norm_length.get(_normalize_stem_length(canonical))
		if not variant_code:
			continue
		rate = rate_by_variant.get(variant_code)
		if rate is None:
			continue
		latest_rate[canonical] = rate
	return latest_rate


def resolve_stem_length_rates(item_code, price_list=None, item_group=None, customer=None):
	"""{canonical_length: rate} for one item, resolved across every source.

	Most specific source wins, so a length priced in more than one place takes
	the narrowest answer:

	  1. `Customer pricing` for this customer + variety              (packhouse)
	  2. `Customer pricing` for this customer + item group           (packhouse)
	  3. per-length `Item Price` rows (`custom_length`)              (ERPNext)
	  4. the post-harvest `Stem Length.price` master                 (harvest)
	  5. a flat `Item Price`, spread across every master length      (ERPNext)

	Note the flat Item Price sits BELOW the master: it is the only source that
	says nothing at all about stem length, so letting it win would price every
	length of an item identically and throw the ladder away.

	Nothing here writes, and each source degrades to {} when its doctype is not
	installed, so the chain simply gets shorter on a partial site rather than
	raising. `price_list` defaults to the resolved selling list.
	"""
	from ecommerce_integration.ecommerce_integration.utils import _resolve_price_list
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		customer_pricing_rates,
		stem_length_master_rates,
	)

	if not item_code:
		return {}

	# Layered least- to most-specific about the stem length, each overwriting the
	# last. A flat Item Price says nothing about length, so it seeds every master
	# length as a floor and the master's own ladder then differentiates them —
	# otherwise one length-agnostic rate would flatten the ladder entirely.
	per_length, flat_rate = _item_price_detail(item_code, price_list or _resolve_price_list())

	rates = {}
	if flat_rate is not None:
		from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
			stem_length_master_values,
		)

		for canon in stem_length_master_values():
			rates[canon] = flat_rate

	rates.update(stem_length_master_rates())
	rates.update(per_length)

	if customer:
		by_item, by_group = customer_pricing_rates(customer)
		if item_group:
			for (group, canon), rate in by_group.items():
				if group == item_group:
					rates[canon] = rate
		for (variety, canon), rate in by_item.items():
			if variety == item_code:
				rates[canon] = rate

	return {canon: rate for canon, rate in rates.items() if rate and rate > 0}


def resolve_stem_length_rate(item_code, stem_length, price_list=None, item_group=None, customer=None):
	"""One rate from `resolve_stem_length_rates`, or None when nothing prices it.

	When `stem_length` is missing or unpriced but the item has exactly one
	priced length, that rate is used — a single-length item is unambiguous and
	callers would otherwise have to skip it.
	"""
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		canonical_stem_length,
	)

	rates = resolve_stem_length_rates(
		item_code, price_list=price_list, item_group=item_group, customer=customer
	)
	if not rates:
		return None

	# `stem_length` often arrives straight out of a stored Link field.
	canon = canonical_stem_length(stem_length)
	if canon and canon in rates:
		return rates[canon]
	if len(rates) == 1:
		return next(iter(rates.values()))
	return None
