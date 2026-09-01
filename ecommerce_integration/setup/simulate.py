# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Put believable stock on the shelves and prices on the master, for testing.

The Floriday and Biflorica screens are only interesting with data behind them: a
stock source with stem lengths, and a per-stem rate for each. On a fresh or
restored site there is neither, so both tabs render empty and there is nothing
to exercise.

This seeds that data locally. It writes ONLY to doctypes that already exist:

  * `Stem Length`   (post-harvest master) — gets a simulated `price`, and this
                     is what makes each length cost a different amount
  * `Item Price`    (ERPNext) — a rate per item on the resolved selling price
                     list, per stem length where this ERPNext allows more than
                     one row per item, otherwise a single base rate
  * `Shelf` / `Shelf Item` (post-harvest) — the stems themselves

Nothing is created that the site does not already own, and every step is a
no-op when its doctype is absent — the report says which steps were skipped and
why. Nothing here calls Floriday or Biflorica: seeding only fills the local
tables the two integrations read, so posting supply lines or offers stays a
deliberate, separate click.

Run it with::

    bench --site <site> execute ecommerce_integration.setup.simulate.simulate

and undo the shelves it made with::

    bench --site <site> execute ecommerce_integration.setup.simulate.clear_simulated_shelves
"""

import zlib

import frappe
from frappe.utils import flt, now_datetime

from ecommerce_integration.ecommerce_integration.utils import _resolve_price_list
from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
	clear_stem_length_label_cache,
)
from ecommerce_integration.ecommerce_integration.utils.stem_length import _normalize_stem_length

# Shelves this module creates are named with this prefix so `clear_simulated_shelves`
# can find exactly its own rows and never touch a real one.
SIM_SHELF_PREFIX = "SIM-SHELF"

DEFAULT_STEM_LENGTHS = ("40CM", "50CM", "60CM", "70CM", "80CM")
DEFAULT_ITEM_GROUP_LIKE = "%Roses%"
DEFAULT_ITEM_LIMIT = 8
DEFAULT_QTY_PER_LENGTH = 500

# Per-stem rate model: `base` at the shortest length, +`step` for every further
# length, and a small per-variety spread so every item is not priced identically
# (a flat price list hides bugs where the wrong item's rate is picked up).
DEFAULT_BASE_RATE = 0.20
DEFAULT_RATE_STEP = 0.05
VARIETY_SPREAD = 0.06


def _has_doctype(name):
	return bool(frappe.db.exists("DocType", name))


def _variety_offset(item_code):
	"""Stable per-item price nudge in [0, VARIETY_SPREAD).

	crc32, not hash(): Python salts str hashes per process, which would reprice
	every item on every run and make the seed non-idempotent.
	"""
	return (zlib.crc32(item_code.encode()) % 100) / 100.0 * VARIETY_SPREAD


def _allowed_stem_lengths(requested):
	"""`requested` narrowed to what the Stem Length master will actually accept.

	`Stem Length.length` is a Select on every farm's build, with a different
	option set per farm (40/50/.../120 on one, 43/53/63/73 on another). Asking
	for a value outside it fails validation, so intersect first and report the
	rest as skipped.
	"""
	if not _has_doctype("Stem Length"):
		return list(requested), []

	try:
		field = frappe.get_meta("Stem Length").get_field("length")
	except Exception:
		field = None

	if not field or field.fieldtype != "Select" or not field.options:
		return list(requested), []

	options = [o.strip() for o in field.options.split("\n") if o.strip()]
	by_canon = {_normalize_stem_length(o): o for o in options}

	allowed, rejected = [], []
	for value in requested:
		match = by_canon.get(_normalize_stem_length(value))
		if match:
			allowed.append(match)
		else:
			rejected.append(value)
	return allowed, rejected


def ensure_stem_length_master(lengths, base_rate=DEFAULT_BASE_RATE, step=DEFAULT_RATE_STEP, company=None):
	"""Make sure every length exists on the post-harvest master, priced.

	Returns {canonical_length: stem_length_docname}. An existing record keeps its
	name (the doctype is autonamed differently per farm) and only has its `price`
	filled in when it has none — a rate somebody set by hand is never overwritten.
	"""
	if not _has_doctype("Stem Length"):
		return {}, "Stem Length is not installed on this site"

	meta = frappe.get_meta("Stem Length")
	if not meta.has_field("length"):
		# A stripped-down master (a CI stub another app created, a half-installed
		# suite) cannot record a length at all — filtering on it would raise
		# "Unknown column 'length'".
		return {}, "Stem Length on this site has no `length` field"
	has_price = meta.has_field("price")
	has_company = meta.has_field("company")

	by_canon = {}
	for index, length in enumerate(lengths):
		canon = _normalize_stem_length(length)
		if not canon:
			continue
		rate = flt(base_rate + index * step, 4)

		name = frappe.db.get_value("Stem Length", {"length": length}, "name")
		if name:
			doc = frappe.get_doc("Stem Length", name)
			if has_price and not flt(doc.price):
				doc.price = rate
				doc.save(ignore_permissions=True)
		else:
			values = {"doctype": "Stem Length", "length": length}
			if has_price:
				values["price"] = rate
			if has_company and company:
				values["company"] = company
			doc = frappe.get_doc(values).insert(ignore_permissions=True)

		by_canon[canon] = doc.name

	# The label map is request-scoped; new master rows must be visible to the
	# price readers that run straight after this.
	clear_stem_length_label_cache()
	return by_canon, None


def ensure_item_attribute_values(lengths):
	"""Mirror the lengths onto the ERPNext `Stem Length` Item Attribute.

	`stem_length.py` spreads a flat Item Price across the master lengths, and on
	a variant-model site the attribute values are that master. Harmless where
	the attribute is already populated.
	"""
	if not frappe.db.exists("Item Attribute", "Stem Length"):
		return 0

	attribute = frappe.get_doc("Item Attribute", "Stem Length")
	existing = {_normalize_stem_length(row.attribute_value) for row in attribute.item_attribute_values}

	added = 0
	for length in lengths:
		if _normalize_stem_length(length) in existing:
			continue
		attribute.append("item_attribute_values", {"attribute_value": length, "abbr": length})
		added += 1

	if added:
		attribute.save(ignore_permissions=True)
	return added


def pick_items(item_group_like=DEFAULT_ITEM_GROUP_LIKE, limit=DEFAULT_ITEM_LIMIT, item_codes=None):
	"""Sellable stem items to seed, newest first, or exactly `item_codes`."""
	if item_codes:
		return frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=["name as item_code", "item_name", "item_group", "stock_uom", "sales_uom"],
		)

	return frappe.get_all(
		"Item",
		filters={
			"item_group": ["like", item_group_like],
			"disabled": 0,
			"has_variants": 0,
			"is_stock_item": 1,
		},
		fields=["name as item_code", "item_name", "item_group", "stock_uom", "sales_uom"],
		order_by="modified desc",
		limit=limit,
	)


def seed_item_prices(
	items, length_names, master=None, price_list=None, base_rate=DEFAULT_BASE_RATE, step=DEFAULT_RATE_STEP
):
	"""`Item Price` rows for the seeded items, per stem length where allowed.

	`Item Price.custom_length` is a Link to the post-harvest `Stem Length`, so
	this needs that master present and populated — `ensure_stem_length_master`
	runs first for exactly that reason. `master` is
	{canonical length: Stem Length docname}, and the DOCNAME is what goes in the
	Link: farms autoname that master differently (after the length on one site,
	off a hash on another), so the human label is not a valid link value
	everywhere.

	Newer ERPNext keys its duplicate check on (price list, party, currency, item,
	batch, UOM, qty, dates) — `custom_length` is not in it, so a second per-length
	row for the same item is rejected as a duplicate. Where that happens the seed
	falls back to a single base rate per item and lets the per-length ladder come
	from `Stem Length.price`, which is the more authoritative source anyway. The
	report says which mode was used.

	Existing rows are repriced rather than duplicated, so re-running the seed
	converges instead of piling up.
	"""
	master = master or {}
	price_list = price_list or _resolve_price_list()
	if not price_list:
		return {"created": 0, "updated": 0, "skipped": "no selling price list on this site"}
	if not frappe.db.has_column("Item Price", "custom_length"):
		return {"created": 0, "updated": 0, "skipped": "Item Price has no custom_length field"}
	if not _has_doctype("Stem Length"):
		return {
			"created": 0,
			"updated": 0,
			"skipped": "Item Price.custom_length links to Stem Length, which is not installed",
		}

	price_list_doc = frappe.db.get_value(
		"Price List", price_list, ["currency", "selling", "buying"], as_dict=True
	)
	created = updated = 0
	per_length = True

	def base_values(item):
		return {
			"doctype": "Item Price",
			"item_code": item.item_code,
			"price_list": price_list,
			"uom": item.sales_uom or item.stock_uom,
			"currency": price_list_doc.currency,
			# ERPNext rejects an Item Price that is neither; mirror the list.
			"selling": price_list_doc.selling,
			"buying": price_list_doc.buying,
		}

	def upsert(item, rate, link_value=None):
		"""Set one Item Price's rate, creating the row if it is not there yet."""
		filters = {"item_code": item.item_code, "price_list": price_list}
		if link_value:
			filters["custom_length"] = link_value

		existing = frappe.db.get_value("Item Price", filters, "name")
		if existing:
			frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
			return 0, 1

		values = base_values(item)
		values["price_list_rate"] = rate
		if link_value:
			values["custom_length"] = link_value
		frappe.get_doc(values).insert(ignore_permissions=True)
		return 1, 0

	for item in items:
		offset = _variety_offset(item.item_code)

		if per_length:
			# All of this item's lengths land or none do: a partial ladder would be
			# worse than the flat rate the fallback writes instead.
			frappe.db.savepoint("ei_seed_item_price")
			item_created = item_updated = 0
			try:
				for index, length_name in enumerate(length_names):
					link_value = master.get(_normalize_stem_length(length_name))
					if not link_value:
						continue
					c, u = upsert(item, flt(base_rate + index * step + offset, 4), link_value)
					item_created += c
					item_updated += u
			except frappe.ValidationError:
				# This ERPNext keys its duplicate check without custom_length, so it
				# will not hold a second per-length row. Undo the partial ladder.
				frappe.db.rollback(save_point="ei_seed_item_price")
				frappe.clear_last_message()
				per_length = False
			else:
				created += item_created
				updated += item_updated

		if not per_length:
			c, u = upsert(item, flt(base_rate + offset, 4))
			created += c
			updated += u

	return {
		"created": created,
		"updated": updated,
		"price_list": price_list,
		"currency": price_list_doc.currency,
		"mode": "per stem length"
		if per_length
		else "one base rate per item (per-length ladder from Stem Length.price)",
	}


def seed_shelf_stock(
	items, length_names, master=None, qty_per_length=DEFAULT_QTY_PER_LENGTH, warehouse=None, farm=None
):
	"""Put `qty_per_length` stems of every (item, length) onto a simulated shelf.

	One `Shelf` per run target, rebuilt from scratch each time so the quantities
	are what was asked for rather than an accumulation of previous runs.
	"""
	if not (_has_doctype("Shelf") and _has_doctype("Shelf Item")):
		return {"rows": 0, "skipped": "Shelf / Shelf Item are not installed on this site"}

	shelf_id = f"{SIM_SHELF_PREFIX}-{(warehouse or 'DEFAULT').replace(' ', '-').upper()}"[:140]

	if frappe.db.exists("Shelf", shelf_id):
		shelf = frappe.get_doc("Shelf", shelf_id)
		shelf.set("items", [])
	else:
		shelf = frappe.get_doc({"doctype": "Shelf", "shelf_id": shelf_id})

	shelf_meta = frappe.get_meta("Shelf")
	if farm and shelf_meta.has_field("farm"):
		shelf.farm = farm

	item_meta = frappe.get_meta("Shelf Item")
	stamp = now_datetime()

	# Free text on some farms' builds, a Link to the stem-length master on others.
	length_field = item_meta.get_field("stem_length")
	links_to_master = bool(length_field) and length_field.fieldtype == "Link"
	master = master or {}

	rows = 0
	for item in items:
		for length_name in length_names:
			value = master.get(_normalize_stem_length(length_name)) if links_to_master else length_name
			if not value:
				continue
			row = {"variety": item.item_code, "stem_length": value, "stem_qty": int(qty_per_length)}
			if warehouse and item_meta.has_field("warehouse"):
				row["warehouse"] = warehouse
			if item_meta.has_field("date_added"):
				row["date_added"] = stamp
			shelf.append("items", row)
			rows += 1

	shelf.save(ignore_permissions=True)
	return {"rows": rows, "shelf": shelf.name}


def seed_floriday_items(items, length_names, base_rate=DEFAULT_BASE_RATE, step=DEFAULT_RATE_STEP):
	"""Map the seeded items into `Floriday Items`, with a rate per stem length.

	Rows go in `Floriday Items.table_ppvq`, a `Floriday Item Length` table this
	app owns. `trade_item_id` is left empty — only Floriday itself can issue
	those, via Fetch Trade Item IDs.
	"""
	if not _has_doctype("Floriday Items"):
		return {"items": 0, "skipped": "Floriday Items is not installed on this site"}

	touched = 0
	for item in items:
		name = frappe.db.get_value("Floriday Items", {"item_code": item.item_code}, "name")
		if name:
			doc = frappe.get_doc("Floriday Items", name)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Floriday Items",
					"item_code": item.item_code,
					"item_name": item.item_name,
					"item_group": item.item_group,
				}
			)

		offset = _variety_offset(item.item_code)
		by_length = {_normalize_stem_length(row.stem_length): row for row in doc.get("table_ppvq") or []}
		for index, length_name in enumerate(length_names):
			rate = flt(base_rate + index * step + offset, 4)
			row = by_length.get(_normalize_stem_length(length_name))
			if row:
				row.rate = rate
			else:
				doc.append("table_ppvq", {"stem_length": length_name, "rate": rate})

		doc.save(ignore_permissions=True)
		touched += 1

	return {"items": touched}


@frappe.whitelist()
def simulate(
	item_group_like: str = DEFAULT_ITEM_GROUP_LIKE,
	limit: int = DEFAULT_ITEM_LIMIT,
	qty_per_length: int = DEFAULT_QTY_PER_LENGTH,
	warehouse: str | None = None,
	farm: str | None = None,
	price_list: str | None = None,
	stem_lengths: str | list | None = None,
	base_rate: float = DEFAULT_BASE_RATE,
	step: float = DEFAULT_RATE_STEP,
):
	"""Seed stem lengths, prices and shelf stock, and report what landed.

	Local only — no Floriday or Biflorica call is made. Safe to re-run: prices
	converge on the same values and the simulated shelf is rebuilt rather than
	appended to.
	"""
	frappe.only_for("System Manager")

	if isinstance(stem_lengths, str):
		stem_lengths = [part.strip() for part in stem_lengths.split(",") if part.strip()]
	requested = list(stem_lengths or DEFAULT_STEM_LENGTHS)

	length_names, rejected = _allowed_stem_lengths(requested)
	if not length_names:
		frappe.throw(
			f"None of the requested stem lengths {requested} are valid on this site's Stem Length master."
		)

	items = pick_items(item_group_like=item_group_like, limit=int(limit))
	if not items:
		frappe.throw(f"No stock items matched item group like '{item_group_like}'.")

	warehouse = warehouse or frappe.db.get_single_value("Biflorica Setting", "warehouse")

	master, master_error = ensure_stem_length_master(length_names, base_rate=base_rate, step=step)
	attribute_values = ensure_item_attribute_values(length_names)
	prices = seed_item_prices(
		items, length_names, master=master, price_list=price_list, base_rate=base_rate, step=step
	)
	shelf = seed_shelf_stock(
		items,
		length_names,
		master=master,
		qty_per_length=int(qty_per_length),
		warehouse=warehouse,
		farm=farm,
	)
	floriday = seed_floriday_items(items, length_names, base_rate=base_rate, step=step)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	report = {
		"items": [item.item_code for item in items],
		"stem_lengths": length_names,
		"rejected_stem_lengths": rejected,
		"warehouse": warehouse,
		"stem_length_master": {"priced": len(master), "error": master_error},
		"item_attribute_values_added": attribute_values,
		"item_prices": prices,
		"shelf_stock": shelf,
		"floriday_items": floriday,
	}
	print(frappe.as_json(report))
	return report


@frappe.whitelist()
def clear_simulated_shelves():
	"""Delete only the shelves this module created (the SIM-SHELF-* ones).

	Prices are deliberately left alone: an `Item Price` row is ordinary data that
	something else may since have relied on, and there is no way to prove the
	seed rather than a person wrote it.
	"""
	frappe.only_for("System Manager")

	if not frappe.db.exists("DocType", "Shelf"):
		return {"deleted": []}

	names = frappe.get_all("Shelf", filters={"name": ["like", f"{SIM_SHELF_PREFIX}%"]}, pluck="name")
	for name in names:
		frappe.delete_doc("Shelf", name, ignore_permissions=True, force=True)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return {"deleted": names}
