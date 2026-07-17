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


def _item_price_rates_for_list(item_code, price_list):
	"""Return {canonical_stem_length: rate} for one item on one price list.

	Non-variant items differentiate per-length Item Price rows via the
	custom_length field. On sites that ship the Custom Field, read it. On sites
	without it, fall back to the single Item Price rate and apply it to every
	Stem Length master value. Returns {} if the price list yields no usable rate.
	"""
	if not price_list:
		return {}
	filters = {"item_code": item_code, "price_list": price_list}

	has_length_col = frappe.db.has_column("Item Price", "custom_length")
	fields = ["price_list_rate"] + (["custom_length"] if has_length_col else [])
	rows = frappe.get_all("Item Price", filters=filters, fields=fields)
	if not rows:
		return {}

	latest_rate = {}
	if has_length_col:
		for row in rows:
			stem_length = _normalize_stem_length(row.custom_length)
			if not stem_length:
				continue
			latest_rate[stem_length] = row.price_list_rate

	if latest_rate:
		return latest_rate

	# No per-length rows usable. Apply the single Item Price rate (prefer one
	# with no custom_length) across every master Stem Length.
	flat_rate = None
	for row in rows:
		if has_length_col and row.get("custom_length"):
			continue
		flat_rate = row.price_list_rate
		break
	if flat_rate is None:
		flat_rate = rows[0].price_list_rate
	if flat_rate is None:
		return {}

	master_lengths = frappe.get_all(
		"Item Attribute Value",
		filters={"parent": "Stem Length"},
		fields=["attribute_value"],
		order_by="idx",
	)
	if not master_lengths:
		return {}

	for ml in master_lengths:
		norm = _normalize_stem_length(ml.attribute_value)
		if norm:
			latest_rate[norm] = flat_rate
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
