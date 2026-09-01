# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Small idempotent builders for the Floriday / Biflorica test data.

The tests run inside a transaction that is rolled back afterwards, so these are
free to insert. They are written get-or-create anyway: the CI site is reused
across modules, and a half-built fixture from another module must not turn into
a DuplicateEntryError here.

Everything a sibling app owns (`Stem Length`, `Shelf`, `Shelf Item`) is guarded
— `has(...)` tells a test to skip rather than fail on a site where that doctype
is not installed.
"""

import frappe

TEST_ITEM_GROUP = "All Item Groups"
TEST_PRICE_LIST = "_Test EI Selling"

# Used only where the site has no Stem Length master to read options from.
FALLBACK_STEM_LENGTHS = ("40CM", "50CM", "60CM", "70CM", "80CM")


def has(*doctypes):
	"""True only when every named DocType is on this site."""
	return all(frappe.db.exists("DocType", doctype) for doctype in doctypes)


def has_stem_length_master():
	"""True when `Stem Length` exists AND can actually hold a length and a price.

	CI stubs this doctype, and a sibling app on the bench may get there first with only
	a title field — so the doctype existing is not enough; querying `length` on
	that stub raises "Unknown column 'length' in 'WHERE'".
	"""
	if not has("Stem Length"):
		return False
	meta = frappe.get_meta("Stem Length")
	return meta.has_field("length") and meta.has_field("price")


def ensure_item(item_code, item_group=TEST_ITEM_GROUP, stock_uom="Nos"):
	if frappe.db.exists("Item", item_code):
		return item_code
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 1,
		}
	).insert(ignore_permissions=True)
	return item_code


def ensure_price_list(name=TEST_PRICE_LIST, currency="USD"):
	if frappe.db.exists("Price List", name):
		return name
	frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": name,
			"currency": currency,
			"enabled": 1,
			"selling": 1,
		}
	).insert(ignore_permissions=True)
	return name


def ensure_item_price(item_code, price_list, rate, stem_length=None, uom="Nos"):
	"""An Item Price row, optionally pinned to one stem length.

	`custom_length` Links to the post-harvest `Stem Length`, whose autoname
	varies per farm, so a `stem_length` label is resolved to (or created as) the
	master record and the DOCNAME is what gets stored.
	"""
	if stem_length is not None:
		stem_length = ensure_stem_length(stem_length)

	filters = {"item_code": item_code, "price_list": price_list}
	if stem_length is not None:
		filters["custom_length"] = stem_length

	existing = frappe.db.get_value("Item Price", filters, "name")
	if existing:
		frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
		return existing

	values = {
		"doctype": "Item Price",
		"item_code": item_code,
		"price_list": price_list,
		"uom": uom,
		"price_list_rate": rate,
		"selling": 1,
	}
	if stem_length is not None:
		values["custom_length"] = stem_length
	return frappe.get_doc(values).insert(ignore_permissions=True).name


def has_per_length_item_prices():
	"""True when Item Price can hold one rate per stem length on this site.

	`custom_length` is a custom field the farms' suites add; without it an item
	has a single rate and the per-length half of the price chain cannot be set up.
	"""
	return frappe.db.has_column("Item Price", "custom_length")


def master_stem_lengths(count=2):
	"""`count` stem-length labels this site's `Stem Length` master will accept.

	Each farm's build pins its own Select options — 40/50/…/120 on one, 43/53/63/73
	on another — so a test that hardcodes "50CM" passes on one site and dies with
	a select-validation error on the next. Asking the master keeps them portable.
	"""
	options = None
	if has("Stem Length"):
		field = frappe.get_meta("Stem Length").get_field("length")
		if field and field.fieldtype == "Select" and field.options:
			options = [option.strip() for option in field.options.split("\n") if option.strip()]

	return list(options or FALLBACK_STEM_LENGTHS)[:count]


def ensure_stem_length(length, price=None):
	"""A post-harvest `Stem Length` master record, priced. Returns its docname."""
	name = frappe.db.get_value("Stem Length", {"length": length}, "name")
	if name:
		if price is not None:
			frappe.db.set_value("Stem Length", name, "price", price)
		return name

	values = {"doctype": "Stem Length", "length": length}
	if price is not None:
		values["price"] = price
	return frappe.get_doc(values).insert(ignore_permissions=True).name


def clear_enabled_stock(item_code):
	"""Drop every `Ecommerce Enabled Stock` row for an item.

	Tests must not inherit each other's enabled rows. Frappe's IntegrationTestCase
	does not undo a `doc.save()` between tests in a class, so a row one test
	enables is still there for the next one — which is how
	"nothing enabled offers nothing" ended up seeing 200 stems enabled by a test
	that had run before it.
	"""
	for name in frappe.get_all("Ecommerce Enabled Stock", filters={"item_code": item_code}, pluck="name"):
		frappe.delete_doc("Ecommerce Enabled Stock", name, ignore_permissions=True, force=True)


def clear_item_prices(item_code, price_list=None):
	"""Drop every `Item Price` for an item (optionally on one price list).

	Same reason as `clear_enabled_stock`: a flat rate one test creates is the
	least specific source in the pricing chain, so it silently fills in for the
	next test's deliberately unpriced length.
	"""
	filters = {"item_code": item_code}
	if price_list:
		filters["price_list"] = price_list
	for name in frappe.get_all("Item Price", filters=filters, pluck="name"):
		frappe.delete_doc("Item Price", name, ignore_permissions=True, force=True)


def ensure_shelf(shelf_id, rows):
	"""A `Shelf` holding exactly `rows`: [(item_code, stem_length, stem_qty), ...].

	`Shelf Item.stem_length` is free text on some farms' builds and a Link to the
	post-harvest `Stem Length` on others, so every length used here is made sure
	of on the master first. It is left unpriced — these rows are stock, and a
	price they did not ask for would quietly satisfy the pricing tests.
	"""
	master = {}
	if has_stem_length_master():
		for _item_code, stem_length, _qty in rows:
			master[stem_length] = ensure_stem_length(stem_length)

	if frappe.db.exists("Shelf", shelf_id):
		shelf = frappe.get_doc("Shelf", shelf_id)
		shelf.set("items", [])
	else:
		shelf = frappe.get_doc({"doctype": "Shelf", "shelf_id": shelf_id})

	length_field = frappe.get_meta("Shelf Item").get_field("stem_length")
	links_to_master = bool(length_field) and length_field.fieldtype == "Link"

	for item_code, stem_length, stem_qty in rows:
		value = master.get(stem_length, stem_length) if links_to_master else stem_length
		shelf.append("items", {"variety": item_code, "stem_length": value, "stem_qty": stem_qty})

	shelf.save(ignore_permissions=True)
	return shelf.name


def ensure_enabled_stock(item_code, rows):
	"""`Ecommerce Enabled Stock` rows: [(stem_length, qty, enabled), ...].

	This is the app's own store for what the sales channels may offer. It holds
	availability only — no rate, which comes from Item Price / the post-harvest
	Stem Length master.
	"""
	names = []
	for stem_length, qty, enabled in rows:
		existing = frappe.db.get_value(
			"Ecommerce Enabled Stock",
			{"item_code": item_code, "stem_length": stem_length},
			"name",
		)
		if existing:
			doc = frappe.get_doc("Ecommerce Enabled Stock", existing)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Ecommerce Enabled Stock",
					"item_code": item_code,
					"stem_length": stem_length,
				}
			)
		doc.stock_qty = qty
		doc.enabled = enabled
		doc.save(ignore_permissions=True)
		names.append(doc.name)
	return names
