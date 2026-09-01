import json
import math
from datetime import datetime, timedelta

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt

_logger = frappe.logger("biflorica", allow_site=True)


# Biflorica runs ONE HOST PER PLATFORM. The wrong one is not an error: it
# authenticates, serves GET /offers, and validates a posted payload — then
# discards the create and answers 200 with an empty body. Only codes confirmed
# against a live host belong here; an unknown code must stay silent rather than
# block a platform this map has not seen.
PLATFORM_HOST_CODES = {
	"kenya": "ke",
	"ecuador": "ec",
}


def platform_host_mismatch(settings):
	"""Message naming a confident platform/base-URL mismatch, else None.

	Flags only the high-confidence case: the platform is one we know the host code
	for, AND the configured host carries a *different* known code. An unrecognised
	host prefix (a new region, a proxy, a local mirror) is left alone.
	"""
	from urllib.parse import urlparse

	platform = (getattr(settings, "platform", "") or "").strip().lower()
	base_url = (getattr(settings, "base_url", "") or "").strip()
	if not platform or not base_url:
		return None

	expected = PLATFORM_HOST_CODES.get(platform)
	if not expected:
		return None

	host = (urlparse(base_url).hostname or "").lower()
	actual = host.split(".", 1)[0] if host else ""
	if not actual or actual == expected:
		return None
	# Only complain when the host names a platform we recognise as a DIFFERENT one.
	if actual not in PLATFORM_HOST_CODES.values():
		return None

	other = next(name for name, code in PLATFORM_HOST_CODES.items() if code == actual)
	return (
		f"Biflorica Setting > Base URL points at the '{actual}' host ({other.title()}) "
		f"but Platform is '{settings.platform}'. Each platform has its own host, and "
		f"the wrong one still authenticates and reads — it just discards new offers "
		f"silently. Expected a '{expected}.' host, e.g. "
		f"{base_url.replace(actual + '.', expected + '.', 1)}"
	)


def _clean_farm_code(farm):
	if not farm:
		return farm
	return str(farm).split("(", 1)[0].strip()


@frappe.whitelist()
def post_all_items_to_biflorica(
	box_type: str | None = None,
	packrate: str | int | float | None = None,
	minimum: str | int | float | None = None,
):
	try:
		if not frappe.db.exists("Biflorica Setting", "Biflorica Setting"):
			frappe.throw(_("Biflorica Setting not found. Please create the document first."))

		settings = frappe.get_doc("Biflorica Setting", "Biflorica Setting")
		_logger.info(f"[Biflorica Sync] Starting Biflorica sync for warehouse: {settings.warehouse}")

		required_fields = {
			"warehouse": settings.warehouse,
			"access_token": settings.access_token,
			"base_url": settings.base_url,
			"platform": settings.platform,
			"farm": settings.farm,
		}

		missing_fields = [field for field, value in required_fields.items() if not value]
		if missing_fields:
			frappe.throw(f"Missing required fields in Biflorica Setting: {', '.join(missing_fields)}")

		# Refuse to post into the void: the wrong platform host returns 200 and
		# creates nothing, so failing here is far kinder than a silent no-op.
		mismatch = platform_host_mismatch(settings)
		if mismatch:
			frappe.throw(mismatch, title=_("Wrong Biflorica host"))

		token_valid = validate_access_token(settings)
		if not token_valid:
			frappe.throw(_("Invalid or expired access token. Please check your Biflorica credentials."))

		items_data = get_enabled_offer_items(settings.warehouse)
		if not items_data:
			_logger.info(f"[Biflorica Sync] No enabled items to offer for warehouse: {settings.warehouse}")
			return {
				"success": True,
				"message": "No enabled items available to create offers.",
				"offers_payload": {"data": [], "countAll": "0"},
				"individual_offers": [],
			}

		_logger.info(f"[Biflorica Enabled Items] FOUND {len(items_data)} ENABLED OFFER ROWS:")
		for i, item in enumerate(items_data, 1):
			_logger.info(
				f"[Biflorica Enabled Items] Item {i}: {item.get('item_code')} - {item.get('item_name')} - Qty: {item.get('actual_qty')} - Price: {item.get('price_per_stem')} - Stem Length: {item.get('stem_length')}"
			)

		_logger.info(f"[Biflorica Sync] Processing {len(items_data)} enabled items")

		offers_payload, individual_offers = prepare_offers_payload_with_details(
			items_data, settings, box_type=box_type, packrate=packrate, minimum=minimum
		)

		_logger.info("[Biflorica Payload] FINAL PAYLOAD BEING SENT TO BIFLORICA:")
		_logger.info(f"[Biflorica Payload] {json.dumps(offers_payload, indent=2)}")

		_logger.info("[Biflorica Offers Details] INDIVIDUAL OFFERS PAYLOAD DETAILS:")
		for i, offer in enumerate(individual_offers, 1):
			_logger.info(f"[Biflorica Offers Details] Offer {i}: {json.dumps(offer, indent=2)}")

		api_response = post_to_biflorica_api(offers_payload, settings)

		return {
			"api_response": api_response,
			"offers_payload": offers_payload,
			"individual_offers": individual_offers,
			"summary": {
				"total_items_processed": len(items_data),
				"offers_created": len(offers_payload["data"]),
				"items_skipped": len(items_data) - len(offers_payload["data"]),
				"skipped_items": [offer for offer in individual_offers if offer["status"] == "skipped"],
			},
		}

	except Exception as e:
		frappe.log_error(f"Biflorica sync error: {e!s}", "Biflorica Sync Error")
		frappe.throw(f"Error posting items to Biflorica: {e!s}")


def validate_access_token(settings):
	try:
		test_endpoint = f"{settings.base_url.rstrip('/')}/auth/verify"
		headers = {
			"Authorization": f"Bearer {settings.access_token}",
			"Content-Type": "application/json",
			"accept": "application/json",
		}

		response = requests.get(test_endpoint, headers=headers, timeout=15)

		if response.status_code == 200:
			_logger.info("[Biflorica Auth] Access token validation successful")
			return True
		else:
			frappe.log_error(
				f"Token validation failed: {response.status_code} - {response.text}", "Biflorica Auth"
			)
			return False

	except Exception as e:
		frappe.log_error(f"Token validation error: {e!s}", "Biflorica Auth")
		return False


def get_stem_length_from_stock_entry(item_code, warehouse):
	try:
		stock_entries = frappe.get_all(
			"Stock Entry",
			fields=["name", "posting_date", "custom_stem_length"],
			filters={"docstatus": 1, "purpose": "Material Receipt", "items": ["like", f"%{item_code}%"]},
			order_by="posting_date desc",
			limit=1,
		)

		if stock_entries:
			stem_length = stock_entries[0].get("custom_stem_length")
			if stem_length:
				cleaned_length = validate_and_clean_stem_length(stem_length)
				if cleaned_length:
					_logger.info(
						f"[Biflorica Stem Length] Found stem length for {item_code} in Stock Entry {stock_entries[0].name}: {stem_length} -> {cleaned_length}"
					)
					return cleaned_length

		stock_entry_details = frappe.get_all(
			"Stock Entry Detail",
			fields=["parent", "item_code", "custom_stem_length"],
			filters={"item_code": item_code, "docstatus": 1, "t_warehouse": warehouse},
			order_by="creation desc",
			limit=1,
		)

		if stock_entry_details:
			stem_length = stock_entry_details[0].get("custom_stem_length")
			if stem_length:
				cleaned_length = validate_and_clean_stem_length(stem_length)
				if cleaned_length:
					_logger.info(
						f"[Biflorica Stem Length] Found stem length for {item_code} in Stock Entry Detail {stock_entry_details[0].parent}: {stem_length} -> {cleaned_length}"
					)
					return cleaned_length

		item_stem_length = get_stem_length_from_item_master(item_code)
		if item_stem_length and item_stem_length != "50":
			_logger.info(
				f"[Biflorica Stem Length] Using stem length from Item master for {item_code}: {item_stem_length}"
			)
			return item_stem_length

		_logger.info(
			f"[Biflorica Stem Length Warning] No stem length found for {item_code} in Stock Entry or Item master, using default 50"
		)
		return "50"

	except Exception as e:
		frappe.log_error(f"Error fetching stem length for {item_code}: {e!s}", "Biflorica Stem Length Error")
		return "50"


def get_stem_length_from_item_master(item_code):
	try:
		item = frappe.get_doc("Item", item_code)
		stem_length_fields = ["stem_length", "item_length", "length", "flower_size", "stem_size", "size"]

		for field in stem_length_fields:
			stem_length = item.get(field)
			if stem_length:
				cleaned_length = validate_and_clean_stem_length(stem_length)
				if cleaned_length:
					return cleaned_length
		return "50"
	except Exception:
		return "50"


def validate_and_clean_stem_length(stem_length):
	if not stem_length:
		return None

	stem_str = str(stem_length).strip()

	stem_str = stem_str.replace("cm", "").replace("CM", "").strip()

	try:
		stem_float = float(stem_str)

		if 20 <= stem_float <= 120:
			rounded_length = round_to_nearest_tens(stem_float)
			_logger.info(
				f"[Biflorica Stem Length Rounding] Rounded stem length {stem_float} to nearest tens: {rounded_length}"
			)
			return str(rounded_length)
		else:
			_logger.info(
				f"[Biflorica Stem Length Validation] Stem length {stem_float} outside reasonable range (20-120cm)"
			)
			return None
	except ValueError:
		if "-" in stem_str:
			parts = stem_str.split("-")
			try:
				num1 = float(parts[0].strip())
				num2 = float(parts[1].strip())
				if 20 <= num1 <= 120 and 20 <= num2 <= 120:
					average = (num1 + num2) / 2
					rounded_length = round_to_nearest_tens(average)
					_logger.info(
						f"[Biflorica Stem Length Conversion] Converted stem length range {stem_str} to average: {average} and rounded to: {rounded_length}"
					)
					return str(rounded_length)
			except Exception:
				pass

		import re

		numbers = re.findall(r"\d+", stem_str)
		if numbers:
			try:
				first_num = float(numbers[0])
				if 20 <= first_num <= 120:
					rounded_length = round_to_nearest_tens(first_num)
					_logger.info(
						f"[Biflorica Stem Length Extraction] Extracted stem length {first_num} from text: {stem_str} and rounded to: {rounded_length}"
					)
					return str(rounded_length)
			except Exception:
				pass

	return None


def round_to_nearest_tens(number):
	return int(round(number / 10) * 10)


# Item fields the offer builder copies through when the site has them. Most are
# custom fields that only some farms ship, so the list is intersected with the
# live Item meta before it reaches the query.
_OFFER_ITEM_FIELDS = (
	"item_code",
	"item_name",
	"item_group",
	"variant_of",
	"packing",
	"box_type",
	"color",
	"image",
	"size",
	"characteristics",
	"stem_length",
	"item_length",
	"length",
	"flower_type",
	"flower_variety",
	"flower_size",
	"stem_size",
	"biflorica_type",
	"biflorica_variety",
)


def _biflorica_item_qty_source(warehouse):
	bins = frappe.get_all(
		"Bin",
		fields=["item_code", "actual_qty"],
		filters={"warehouse": warehouse, "actual_qty": [">", 0]},
	)
	return {b["item_code"]: b["actual_qty"] for b in bins}


def _item_meta_by_code(item_codes):
	"""{item_code: {field: value}} for the offer fields this site actually has."""
	if not item_codes:
		return {}
	existing_fields = {f.fieldname for f in frappe.get_meta("Item").fields}
	fetch_fields = [field for field in _OFFER_ITEM_FIELDS if field in existing_fields]
	return {
		i["item_code"]: i
		for i in frappe.get_all("Item", fields=fetch_fields, filters={"item_code": ["in", list(item_codes)]})
	}


def _enabled_offer_rows():
	"""The (item, length, qty) rows an operator enabled on the Stock tab.

	Read from `Ecommerce Enabled Stock`, which this app owns. `rate` is always
	None — availability is the only thing stored; the per-stem price is resolved
	from `Item Price` / the post-harvest `Stem Length` master by
	`_offer_items_from_rows`.

	This is the ONLY source of offers. Empty means nothing was enabled, and
	nothing gets posted.
	"""
	rows = frappe.get_all(
		"Ecommerce Enabled Stock",
		filters={"enabled": 1, "stock_qty": [">", 0]},
		fields=["item_code", "stem_length", "stock_qty"],
		order_by="item_code asc, stem_length asc",
	)
	for row in rows:
		row.rate = None
	return rows


def _offer_items_from_rows(rows, customer=None):
	"""Turn (item, length, qty, rate) rows into the offer-builder's item dicts.

	A row with no rate of its own is priced through the post-harvest chain
	(`Customer pricing` -> `Item Price` -> `Stem Length.price`). Rows that stay
	unpriced are kept, not dropped: `prepare_offers_payload_with_details`
	already reports zero-price items as skipped with a reason, and silently
	losing them here would make that diagnosis impossible.
	"""
	from ecommerce_integration.ecommerce_integration.utils.stem_length import (
		resolve_stem_length_rate,
	)

	item_meta = _item_meta_by_code({r.item_code for r in rows})

	offer_items = []
	for r in rows:
		base = dict(item_meta.get(r.item_code) or {"item_code": r.item_code, "item_name": r.item_code})
		base["actual_qty"] = flt(r.stock_qty)
		base["stem_length"] = r.stem_length or base.get("stem_length")
		rate = flt(r.rate)
		if rate <= 0:
			rate = flt(
				resolve_stem_length_rate(
					r.item_code,
					base.get("stem_length"),
					item_group=base.get("item_group"),
					customer=customer,
				)
			)
		base["price_per_stem"] = rate
		offer_items.append(base)

	return offer_items


def get_enabled_offer_items(warehouse=None, customer=None):
	"""Items to offer on Biflorica: ONLY what is enabled, at the enabled qty.

	Deliberately has no fallback to raw shelf or warehouse stock. It used to fall
	back when nothing was enabled, which meant pressing Post Offers with an empty
	selection offered the entire shelf — every variety and length on the list —
	rather than the handful an operator had actually chosen.

	`warehouse` is accepted for call compatibility and no longer used: the
	enabled set is explicit, so there is no location to infer stock from.
	"""
	rows = _enabled_offer_rows()
	if not rows:
		return []
	return _offer_items_from_rows(rows, customer=customer)


def _enabled_keys():
	"""{(item_code, canonical_length)} for everything currently enabled."""
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		canonical_stem_length,
	)

	return {
		(r.item_code, canonical_stem_length(r.stem_length))
		for r in frappe.get_all(
			"Ecommerce Enabled Stock",
			filters={"enabled": 1},
			fields=["item_code", "stem_length"],
		)
	}


def get_warehouse_stock_items(warehouse):
	"""Rows for the "Stock Available for Offers" table.

	When Biflorica Setting opts into shelf stock, the shelves are the source and
	each row carries its own stem length; otherwise this is Bin stock keyed by
	item code alone.

	`Show Only Enabled Stock?` narrows the table to the (item, length) pairs that
	are actually enabled — the set Post Offers will send. Until now that checkbox
	was read by nothing at all, so the table always listed the whole shelf.
	"""
	from ecommerce_integration.ecommerce_integration.utils.post_harvest import (
		canonical_stem_length,
	)
	from ecommerce_integration.ecommerce_integration.utils.shelf_stock import shelf_stock_enabled
	from ecommerce_integration.ecommerce_integration.utils.stock_picker import get_shelf_rows

	enabled_only = bool(cint(frappe.db.get_single_value("Biflorica Setting", "publish_enabled_stock_only")))
	enabled_keys = _enabled_keys() if enabled_only else None

	if shelf_stock_enabled("Biflorica Setting"):
		shelf_rows = get_shelf_rows()
		if shelf_rows:
			item_meta = _item_meta_by_code({r["item_code"] for r in shelf_rows})
			items_with_stock = []
			for r in shelf_rows:
				if (
					enabled_keys is not None
					and (
						r["item_code"],
						canonical_stem_length(r.get("stem_length")),
					)
					not in enabled_keys
				):
					continue
				item = dict(item_meta.get(r["item_code"]) or {"item_code": r["item_code"]})
				item["item_name"] = item.get("item_name") or r.get("item_name") or r["item_code"]
				item["stem_length"] = r.get("stem_length") or item.get("stem_length")
				item["actual_qty"] = flt(r.get("shelf_qty"))
				items_with_stock.append(item)
			return items_with_stock

	qty_by_code = _biflorica_item_qty_source(warehouse)
	if not qty_by_code:
		return []

	if enabled_keys is not None:
		# Bin rows carry no stem length, so match on the item alone here.
		enabled_items = {code for code, _length in enabled_keys}
		qty_by_code = {c: q for c, q in qty_by_code.items() if c in enabled_items}
		if not qty_by_code:
			return []

	items = list(_item_meta_by_code(qty_by_code.keys()).values())
	for item in items:
		item["actual_qty"] = qty_by_code.get(item["item_code"], 0)
	return items


def get_item_price(item_code, price_list=None, stem_length=None, item_group=None, customer=None):
	"""Per-stem rate for an item, via the shared post-harvest price chain.

	Falls back to any Item Price the item has on any list, so a site that prices
	only in its default list still gets a rate rather than a zero (which the
	offer builder treats as "skip this item").
	"""
	from ecommerce_integration.ecommerce_integration.utils.stem_length import (
		resolve_stem_length_rate,
	)

	try:
		rate = resolve_stem_length_rate(
			item_code,
			stem_length,
			price_list=price_list,
			item_group=item_group,
			customer=customer,
		)
		if rate:
			return float(rate)

		any_price = frappe.get_all(
			"Item Price",
			fields=["price_list_rate"],
			filters={"item_code": item_code},
			order_by="modified desc",
			limit=1,
		)
		if any_price:
			return float(any_price[0].price_list_rate or 0)

		_logger.info(f"[Biflorica Price] No price found for item {item_code}")
		return 0

	except Exception as e:
		frappe.log_error(f"Error getting price for {item_code}: {e!s}", "Biflorica Price Error")
		return 0


def get_biflorica_flower_type(item):
	return "Rose"


def get_biflorica_flower_variety(item, flower_type):
	if item.get("biflorica_variety"):
		return item.get("biflorica_variety")

	potential_varieties = [item.get("flower_variety"), item.get("variant_of"), item.get("item_name")]

	for potential_variety in potential_varieties:
		if potential_variety:
			clean_variety = str(potential_variety).strip()

			clean_variety = clean_variety.replace(flower_type, "").strip()
			clean_variety = clean_variety.replace("Rose", "").strip()

			for prefix in ["Variety", "Type", "Flower", "Stem"]:
				clean_variety = clean_variety.replace(prefix, "").strip()

			if clean_variety:
				return clean_variety[:50]

	default_varieties = {"Rose": "Standard"}

	return default_varieties.get(flower_type, "Standard")


def _slash(values):
	"""Biflorica's parallel-list encoding, e.g. "40/50/60"."""
	return "/".join(str(v) for v in values)


def prepare_offers_payload_with_details(items_data, settings, box_type=None, packrate=None, minimum=None):
	"""Build offers in the exact shape `GET /offers` returns.

	One offer is ONE BOX spanning every enabled stem length of a variety — not
	one offer per length, which is what this used to send:

	    size          "40/50/60"        the lengths, ascending
	    pricePerStem  "0.20/0.25/0.30"  one rate per length, parallel to `size`
	    sizesStems    "66/66/66"        stems of each length inside ONE box
	    packing       200               nominal stems per box (int)
	    quantity      "3.0"             number of BOXES, not stems
	    price         "49.50"           box price = sum(rate_i * stems_i)

	Checked against live offer 74, whose 9 sizes x 22 stems and 60.72 box price
	reproduce exactly under this arithmetic. Sending `quantity` as stems (the old
	behaviour) offered 200 boxes of 200 stems where 200 stems were meant.

	`minimum` is accepted so existing callers keep working but is NOT sent — the
	live offer structure carries no such field.
	"""
	# `or 1`, not just a getattr default: Biflorica Setting has no such field, so
	# a mapping-like settings object answers None rather than raising, and
	# timedelta(days=None) would blow up the whole offer run.
	offer_duration_days = getattr(settings, "offer_duration_days", None) or 1

	box_type = (box_type or "HB").strip() if isinstance(box_type, str) else (box_type or "HB")
	try:
		packrate = int(flt(packrate))
	except (TypeError, ValueError):
		packrate = 0
	if packrate <= 0:
		packrate = 300

	details = []

	def skip(item, reason, **debug):
		details.append(
			{
				"item_code": item.get("item_code"),
				"item_name": item.get("item_name"),
				"status": "skipped",
				"reason": reason,
				"payload": None,
				"debug_info": debug or None,
			}
		)

	# Collect the enabled rows into one box spec per (type, variety).
	groups = {}
	for item in items_data:
		item_code = item.get("item_code")
		quantity = flt(item.get("actual_qty"))

		price_per_stem = item.get("price_per_stem")
		if price_per_stem is None:
			price_per_stem = get_item_price(
				item_code,
				stem_length=item.get("stem_length"),
				item_group=item.get("item_group"),
			)
		price_per_stem = flt(price_per_stem)

		stem_length = validate_and_clean_stem_length(item.get("stem_length"))
		if not stem_length:
			stem_length = get_stem_length_from_stock_entry(item_code, settings.warehouse)

		if price_per_stem <= 0:
			skip(
				item,
				"Zero price - no rate in Item Price or the post-harvest Stem Length master",
				price_per_stem=price_per_stem,
				quantity=quantity,
			)
			continue
		if quantity <= 0:
			skip(item, "Zero quantity", quantity=quantity)
			continue
		if not stem_length:
			skip(item, "No stem length could be resolved for this row")
			continue

		flower_type = get_biflorica_flower_type(item)
		flower_variety = get_biflorica_flower_variety(item, flower_type)
		group = groups.setdefault(
			(flower_type, flower_variety),
			{"item": item, "type": flower_type, "variety": flower_variety, "lengths": {}},
		)
		# Two items can map to one Biflorica variety. Pool their stems and keep the
		# lower rate, so the box stays sellable at the price we quote.
		row = group["lengths"].get(stem_length)
		if row:
			row["qty"] += quantity
			row["rate"] = min(row["rate"], price_per_stem)
		else:
			group["lengths"][stem_length] = {"qty": quantity, "rate": price_per_stem}

	now = datetime.now()
	date_start = now.strftime("%Y-%m-%d %H:%M:%S")
	date_end = (now + timedelta(days=offer_duration_days)).strftime("%Y-%m-%d %H:%M:%S")

	offer_data = []
	for group in groups.values():
		item = group["item"]
		lengths = sorted(group["lengths"], key=lambda size: flt(size))

		# The box is split evenly across its lengths, the way live offer 74 is
		# (9 sizes x 22 stems against a nominal packing of 200).
		stems_each = packrate // len(lengths)
		if stems_each <= 0:
			skip(
				item,
				f"Packing of {packrate} cannot be split across {len(lengths)} stem lengths",
				stem_lengths=lengths,
			)
			continue

		# A box needs `stems_each` of EVERY length, so the scarcest length caps how
		# many whole boxes can be offered.
		boxes = min(int(flt(group["lengths"][size]["qty"]) // stems_each) for size in lengths)
		if boxes <= 0:
			skip(
				item,
				f"Not enough stock for one full box ({stems_each} stems of each of {len(lengths)} lengths)",
				stem_lengths=lengths,
				available={size: group["lengths"][size]["qty"] for size in lengths},
			)
			continue

		rates = [flt(group["lengths"][size]["rate"]) for size in lengths]
		box_price = round(sum(rate * stems_each for rate in rates), 2)

		offer = {
			"dateStart": date_start,
			"dateEnd": date_end,
			"platform": settings.platform,
			"farm": _clean_farm_code(settings.farm),
			"type": group["type"],
			"variety": group["variety"],
			"color": item.get("color") or "",
			"pictureURL": get_picture_url(item),
			"size": _slash(lengths),
			"pricePerStem": _slash(f"{rate:.2f}" for rate in rates),
			"sizesStems": _slash([stems_each] * len(lengths)),
			"price": f"{box_price:.2f}",
			"packing": packrate,
			"quantity": f"{float(boxes):.1f}",
			"boxType": box_type,
			"characteristics": get_flower_characteristics(item),
		}
		offer_data.append(offer)

		_logger.info(
			f"[Biflorica Offer] {group['variety']}: sizes {offer['size']} | "
			f"{stems_each} stems each | {boxes} box(es) of {packrate} | box price {offer['price']}"
		)
		details.append(
			{
				"item_code": item.get("item_code"),
				"item_name": item.get("item_name"),
				"status": "ready_to_post",
				"reason": "Successfully mapped",
				"payload": offer,
				"source_data": {
					"stem_lengths": lengths,
					"stems_per_length_in_box": stems_each,
					"boxes": boxes,
					"box_type": box_type,
					"packing": packrate,
					"rates": rates,
				},
			}
		)

	main_payload = {"data": offer_data, "countAll": str(len(offer_data))}
	return main_payload, details


def get_flower_characteristics(item):
	characteristics = []

	item_characteristics = item.get("characteristics")
	if item_characteristics:
		if isinstance(item_characteristics, str):
			try:
				char_list = json.loads(item_characteristics)
				if isinstance(char_list, list):
					characteristics.extend(char_list)
			except Exception:
				if "," in item_characteristics:
					characteristics.extend([c.strip() for c in item_characteristics.split(",")])
				else:
					characteristics.append(item_characteristics.strip())
		elif isinstance(item_characteristics, list):
			characteristics.extend(item_characteristics)

	if item.get("color"):
		characteristics.append(f"{item['color']} color")

	characteristics = [str(c) for c in characteristics if c]

	return characteristics


def get_picture_url(item):
	image_field = item.get("image")
	if image_field:
		if image_field.startswith(("http://", "https://")):
			return image_field
		else:
			try:
				site_url = frappe.utils.get_url()
				return f"{site_url}{image_field}"
			except Exception:
				return ""
	return ""


def post_to_biflorica_api(offers_payload, settings):
	endpoint_url = f"{settings.base_url.rstrip('/')}/offers"

	headers = {
		"Authorization": f"Bearer {settings.access_token}",
		"Content-Type": "application/json",
		"accept": "application/json",
	}

	frappe.log_error(f"Posting {len(offers_payload['data'])} offers to: {endpoint_url}", "Biflorica Sync")

	try:
		response = requests.post(endpoint_url, json=offers_payload, headers=headers, timeout=30)

		_logger.info(f"[Biflorica API Response] API RESPONSE STATUS: {response.status_code}")
		_logger.info(f"[Biflorica API Response] API RESPONSE BODY: {response.text}")

		if response.status_code in [200, 201]:
			# A create that worked answers with a JSON array carrying a per-offer
			# `result` (and the new offer's id). An EMPTY body means the request was
			# accepted and then nothing happened — Biflorica returns 200 with a
			# zero-length text/html body when the payload validates but the offer is
			# not created. Reporting that as success is how "Posted N offer(s)" came
			# to be shown for offers that never appeared on the marketplace.
			if not (response.text or "").strip():
				# Seen when Base URL points at the WRONG PLATFORM HOST. Biflorica runs
				# one host per platform — ke.term.apitest… is Kenya, ec.term.apitest…
				# is Ecuador — and the wrong one still authenticates, still serves
				# GET /offers, and still validates the payload. It just discards the
				# create and answers 200 with an empty body. That cost a long debug;
				# name it first.
				message = (
					"Biflorica accepted the request but returned an empty body, so nothing "
					f"was created. Check Biflorica Setting > Base URL ({settings.base_url}) "
					"points at the host for this platform "
					f"({settings.platform or 'not set'}) — each platform has its own host "
					"(e.g. ke.term… for Kenya, ec.term… for Ecuador), and the wrong one "
					"reads fine but silently discards new offers."
				)
				frappe.log_error(
					f"{message}\nendpoint={endpoint_url}\npayload={json.dumps(offers_payload)[:1500]}",
					"Biflorica Empty Response",
				)
				return {
					"success": False,
					"message": message,
					"offers_count": 0,
					"api_response": response.text,
					"status_code": response.status_code,
				}

			if "not_validate" in response.text or "Not parsed" in response.text:
				error_msg = f"Biflorica validation failed: {response.text}"
				frappe.log_error(error_msg, "Biflorica Validation Error")

				validation_errors = []
				try:
					errors = json.loads(response.text)
					for i, error_item in enumerate(errors):
						if "errors" in error_item:
							validation_errors.append({"offer_index": i, "errors": error_item["errors"]})
							frappe.log_error(
								f"Item {i + 1} errors: {error_item['errors']}", "Biflorica Validation Details"
							)
				except Exception:
					pass

				return {
					"success": False,
					"message": "Biflorica validation failed. Check error logs for details.",
					"validation_errors": validation_errors,
					"api_response": response.text,
					"status_code": response.status_code,
				}
			else:
				_logger.info(
					f"[Biflorica Sync] Successfully posted {len(offers_payload['data'])} offers to Biflorica"
				)
				return {
					"success": True,
					"message": f"Successfully posted {len(offers_payload['data'])} offers to Biflorica",
					"offers_count": len(offers_payload["data"]),
					"api_response": response.text,
					"status_code": response.status_code,
				}
		else:
			error_msg = f"API Error {response.status_code}: {response.text}"
			frappe.log_error(error_msg, "Biflorica Sync")
			return {
				"success": False,
				"message": error_msg,
				"status_code": response.status_code,
				"api_response": response.text,
			}

	except requests.exceptions.RequestException as e:
		error_msg = f"Request failed: {e!s}"
		frappe.log_error(error_msg, "Biflorica Sync")
		return {"success": False, "message": error_msg, "status_code": None, "api_response": None}
