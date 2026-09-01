# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Read side for the post-harvest masters this integration prices against.

Floriday and Biflorica both need a per-stem rate for every (item, stem length)
they publish. That used to come from a table upande_webshop owned, so the
Floriday supply lines had no rate to send and the Biflorica offer builder
returned nothing at all on any other site — even though the post-harvest suite
already holds exactly that data:

  * `Stem Length` (upande_harvest / upande_kaitet) is the per-length master and
    carries a `price`: the standing per-stem rate for that length.
  * `Customer pricing` (upande_packhouse, a child table on Customer) overrides
    it per customer, per variety, per length.

Both are read here, never written and never created. Every function returns an
empty result when its doctype is not on the site, so this module is safe on a
site running only part of the suite.
"""

import frappe
from frappe.utils import flt

from ecommerce_integration.ecommerce_integration.utils.stem_length import _normalize_stem_length

STEM_LENGTH = "Stem Length"
CUSTOMER_PRICING = "Customer pricing"

# Where the packhouse hangs its per-customer rate table off Customer.
CUSTOMER_PRICING_FIELD = "custom_customer_pricing"


def _has_doctype(name):
	return bool(frappe.db.exists("DocType", name))


def _has_field(doctype, fieldname):
	"""True when `doctype` is on the site AND carries `fieldname`.

	The CI stubs (and any half-installed suite) create a `Stem Length` with only
	a title, so presence of the doctype alone is not enough to read `price`.

	The existence check is deliberate rather than a caught `get_meta` failure:
	`get_meta` on an absent doctype queues "DocType <x> not found" on
	`frappe.message_log` before it raises, and a swallowed exception leaves that
	message to surface as a dialog later.
	"""
	if not _has_doctype(doctype):
		return False
	return frappe.get_meta(doctype).has_field(fieldname)


def stem_length_master_values():
	"""Canonical "<n>cm" labels for every stem length the site knows about.

	Prefers the post-harvest `Stem Length` master; falls back to the ERPNext
	`Stem Length` Item Attribute values, which is all a variant-model site has.
	Returns [] when neither is populated — callers must treat that as "no master"
	rather than "no lengths exist".
	"""
	values = []

	if _has_doctype(STEM_LENGTH):
		field = "length" if _has_field(STEM_LENGTH, "length") else "name"
		for row in frappe.get_all(STEM_LENGTH, fields=[f"{field} as length"]):
			canon = _normalize_stem_length(row.length)
			if canon and canon not in values:
				values.append(canon)

	if values:
		return values

	for row in frappe.get_all(
		"Item Attribute Value",
		filters={"parent": "Stem Length"},
		fields=["attribute_value"],
		order_by="idx",
	):
		canon = _normalize_stem_length(row.attribute_value)
		if canon and canon not in values:
			values.append(canon)
	return values


def stem_length_label_by_name():
	"""{docname: canonical length} for the post-harvest master.

	Each farm autonames `Stem Length` differently — after the length itself on
	one site, off a naming series or a plain hash on another. A Link field
	storing "o2ji099qjk" therefore cannot be read by pulling digits out of it;
	it has to be resolved through the master. Request-scoped so a page that
	prices a few hundred rows reads the (small) table once.
	"""
	cached = getattr(frappe.local, "_ei_stem_length_labels", None)
	if cached is not None:
		return cached

	mapping = {}
	if _has_doctype(STEM_LENGTH):
		field = "length" if _has_field(STEM_LENGTH, "length") else "name"
		for row in frappe.get_all(STEM_LENGTH, fields=["name", f"{field} as length"]):
			canon = _normalize_stem_length(row.length)
			if canon:
				mapping[row.name] = canon

	frappe.local._ei_stem_length_labels = mapping
	return mapping


def clear_stem_length_label_cache():
	"""Drop the request-scoped label map after writing to the master."""
	frappe.local._ei_stem_length_labels = None


def stem_length_name_by_label():
	"""{canonical length: docname} — the inverse of `stem_length_label_by_name`.

	For writing a Link to `Stem Length` from a length that arrives as text
	("60cm" off a Floriday trade item). Built from the same map, so it inherits
	the request-scoped cache and the per-farm naming quirks.

	Where several master records share a length, the first by docname wins;
	picking deterministically matters more than which one, since duplicates in
	the master are a data problem, not something to resolve by guessing here.
	"""
	inverse = {}
	for name, label in sorted(stem_length_label_by_name().items()):
		inverse.setdefault(label, name)
	return inverse


def resolve_stem_length_name(value):
	"""The `Stem Length` docname for `value`, or None — EXACT match only.

	Deliberately does no rounding. Biflorica posts sizes rounded to the nearest
	ten and has to round back (see `_resolve_stem_length`); Floriday states the
	real length, and this master genuinely holds 43cm and 63cm alongside 40cm and
	60cm. Rounding here would file a 63cm order under 60cm — the same mistake
	that once folded 83CM into 53CM.

	Returns None rather than a near miss when the master has no such length: a
	blank Link is a visible gap, a wrong one is not.
	"""
	if value in (None, ""):
		return None
	value = str(value).strip()
	if not value:
		return None
	if _has_doctype(STEM_LENGTH):
		# Take the name `exists` GIVES BACK, never the value passed in. MySQL
		# collates case-insensitively, so "37CM" matches the record named "37cm" —
		# returning the input would write "37CM" into a Link field and leave it
		# pointing at nothing.
		name = frappe.db.exists(STEM_LENGTH, value)
		if name:
			return name
	canon = canonical_stem_length(value)
	return stem_length_name_by_label().get(canon) if canon else None


def canonical_stem_length(value):
	"""Canonical "<n>cm" for `value`, resolving a master docname when it is one.

	Use this wherever the value came out of a *stored* field, since that field
	may be a Link to `Stem Length`; `_normalize_stem_length` alone is right only
	for free text that already contains the number.
	"""
	if value is None:
		return None
	label = stem_length_label_by_name().get(str(value))
	return label or _normalize_stem_length(value)


def stem_length_master_rates(company=None):
	"""{canonical_length: price} from the post-harvest `Stem Length` master.

	`company` narrows to that company's rows when the master is company-scoped;
	rows with no company apply everywhere, and a company-specific row wins over
	a global one for the same length. Lengths priced at 0 are dropped — a zero
	rate is "unpriced" here, not "free", and letting it through would publish a
	0.00 supply line.
	"""
	if not (_has_doctype(STEM_LENGTH) and _has_field(STEM_LENGTH, "price")):
		return {}

	fields = ["price"]
	fields.append("length" if _has_field(STEM_LENGTH, "length") else "name as length")
	has_company = _has_field(STEM_LENGTH, "company")
	if has_company:
		fields.append("company")

	rows = frappe.get_all(STEM_LENGTH, fields=fields)

	rates = {}
	for row in rows:
		canon = _normalize_stem_length(row.get("length"))
		rate = flt(row.get("price"))
		if not canon or rate <= 0:
			continue
		row_company = row.get("company") if has_company else None
		if company and row_company and row_company != company:
			continue
		# A company-specific row outranks the global one for the same length.
		if canon in rates and not row_company:
			continue
		rates[canon] = rate
	return rates


def customer_pricing_rates(customer):
	"""Per-customer overrides from the packhouse `Customer pricing` table.

	Returns ``(by_item, by_group)``:

	  * ``by_item``  — {(item_code, canonical_length): rate}
	  * ``by_group`` — {(item_group, canonical_length): rate}

	A row names either a `variety` (an Item) or a `type` (an Item Group), so the
	caller checks the item-specific map first and the group map second. Both are
	empty when the doctype or the Customer field is absent.
	"""
	empty = ({}, {})
	if not customer or not _has_doctype(CUSTOMER_PRICING):
		return empty
	if not _has_field("Customer", CUSTOMER_PRICING_FIELD):
		return empty

	rows = frappe.get_all(
		CUSTOMER_PRICING,
		filters={"parenttype": "Customer", "parent": customer},
		fields=["variety", "type", "stem_length", "rate"],
	)

	by_item = {}
	by_group = {}
	for row in rows:
		# `Customer pricing.stem_length` Links to the master, so the stored value
		# is a docname, not necessarily a readable length.
		canon = canonical_stem_length(row.stem_length)
		rate = flt(row.rate)
		if not canon or rate <= 0:
			continue
		if row.variety:
			by_item[(row.variety, canon)] = rate
		elif row.type:
			by_group[(row.type, canon)] = rate
	return by_item, by_group
