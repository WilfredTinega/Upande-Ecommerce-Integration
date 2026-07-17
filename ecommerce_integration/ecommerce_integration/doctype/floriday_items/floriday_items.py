# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import re

import frappe
import requests
from frappe.model.document import Document

_ITEM_GROUP_REGEXP = r"(^|[^[:alnum:]])(rose|roses|herb|herbs)([^[:alnum:]]|$)"


_NAME_PREFIXES = (
	"rosa large flowered",
	"rosa spray",
	"rosa",
	"alstroemeria",
	"gypsophila",
	"lepidium",
	"leather fern",
)


def _strip_floriday_prefixes(text):
	t = (text or "").lower()
	for p in _NAME_PREFIXES:
		if t.startswith(p + " "):
			t = t[len(p) + 1 :]
			break
	t = re.sub(r"\s*-\s*length\s+", " ", t)
	return t


def _normalize_name(text):
	if not text:
		return ""
	return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _split_name_and_length(text):
	stripped = _strip_floriday_prefixes(text)
	m = re.search(r"^(.*?)(\d+)\s*(?:cm)?\s*$", stripped)
	if not m:
		return None, None
	name = _normalize_name(m.group(1))
	length = int(m.group(2)) if m.group(2) else None
	return name, length


def _floriday_length_for(stem_length):
	# Floriday grades stem lengths by rounding down to the nearest 10.
	m = re.search(r"\d+", str(stem_length or ""))
	if not m:
		return None
	return (int(m.group(0)) // 10) * 10


def _alert(message, indicator="orange"):
	frappe.msgprint(message, alert=True, indicator=indicator)


# Generic stem-length + per-length pricing helpers, vendored in this app's utils
# (they read only ERPNext Item Price / Item Attribute data). Re-exported here so
# the rest of this integration keeps importing them from floriday_items unchanged.
from ecommerce_integration.ecommerce_integration.utils.stem_length import (  # noqa: F401
	_normalize_stem_length,
	_item_price_rates_for_list,
	_stem_length_rates_from_item_prices,
	_stem_length_rates_from_variants,
)


class FloridayItems(Document):
	@frappe.whitelist()
	def fetch_stem_length_prices(self, price_list=None):
		if not self.item_code:
			_alert("Item Code is required to fetch prices.", "red")
			return 0

		if not price_list:
			# Floriday Settings has no price_list field; resolve one (USD Price List
			# → first enabled USD Selling list, with a Webshop Settings override if
			# that app happens to be present).
			from ecommerce_integration.ecommerce_integration.utils import _resolve_price_list
			price_list = _resolve_price_list()

		has_variants = frappe.db.get_value("Item", self.item_code, "has_variants")
		if has_variants:
			latest_rate = _stem_length_rates_from_variants(self.item_code, price_list)
		else:
			latest_rate = _stem_length_rates_from_item_prices(self.item_code, price_list)

		existing = {row.stem_length: row for row in self.table_ppvq if row.stem_length}

		for stem_length, rate in latest_rate.items():
			if stem_length in existing:
				existing[stem_length].rate = rate
			else:
				self.append(
					"table_ppvq",
					{"stem_length": stem_length, "rate": rate},
				)

		self.set(
			"table_ppvq",
			[row for row in self.table_ppvq if row.stem_length in latest_rate],
		)

		self.save()
		return len(self.table_ppvq)

	def apply_trade_item_ids(self, article_lookup):
		matched = 0
		for row in self.table_ppvq:
			if row.refresh_trade_item_id(article_lookup, item_name=self.item_name):
				matched += 1
		return matched

	@frappe.whitelist()
	def fetch_trade_item_ids(self):
		if not self.item_name:
			_alert("Item Name is required to match Floriday trade items.", "red")
			return {"total_rows": 0, "matched": 0}
		if not self.table_ppvq:
			_alert("Add stem length rows first (run Fetch Stem Length Prices).", "orange")
			return {"total_rows": 0, "matched": 0}

		try:
			article_lookup = _fetch_floriday_trade_items()
		except Exception as e:
			_alert(f"Could not fetch trade items: {e}", "red")
			return {"total_rows": len(self.table_ppvq), "matched": 0}

		matched = self.apply_trade_item_ids(article_lookup)
		self.save()
		return {"total_rows": len(self.table_ppvq), "matched": matched}


def _get_floriday_settings():
	settings = frappe.get_single("Floriday Settings")
	if not (settings.base_url and settings.access_token and settings.api_key):
		frappe.throw("Floriday Settings missing base_url, access_token, or api_key.")
	return settings


def _fetch_floriday_trade_items():
	settings = _get_floriday_settings()
	try:
		response = requests.get(
			f"{settings.base_url}trade-items/",
			headers={
				"Authorization": f"Bearer {settings.access_token}",
				"X-Api-Key": settings.api_key,
				"Accept": "application/json",
			},
			timeout=60,
		)
	except Exception as e:
		frappe.throw(f"Floriday request failed: {e}")

	if response.status_code != 200:
		frappe.throw(
			f"Floriday returned {response.status_code}: {response.text[:500]}"
		)

	data = response.json()
	trade_items = data.get("results", data) if isinstance(data, dict) else data
	if not isinstance(trade_items, list):
		frappe.throw("Unexpected Floriday response shape.")

	article_lookup = {}
	for ti in trade_items:
		nl = (ti.get("tradeItemName") or {}).get("nl") or ""
		trade_item_id = ti.get("tradeItemId")
		if not (nl and trade_item_id):
			continue
		name, length = _split_name_and_length(nl)
		if not name or length is None:
			continue
		key = (name, length)
		if key not in article_lookup:
			article_lookup[key] = trade_item_id
	return article_lookup


def _find_or_create_floriday_item(item):
	existing = frappe.db.exists("Floriday Items", {"item_code": item.item_code})
	if not existing and frappe.db.exists("Floriday Items", item.item_name):
		existing = item.item_name
	if existing:
		doc = frappe.get_doc("Floriday Items", existing)
		updated = False
		if not doc.item_code:
			doc.item_code = item.item_code
			updated = True
		if not doc.item_group:
			doc.item_group = item.item_group
			updated = True
		if updated:
			doc.save()
		return doc, False

	doc = frappe.get_doc({
		"doctype": "Floriday Items",
		"item_code": item.item_code,
		"item_name": item.item_name,
		"item_group": item.item_group,
	})
	doc.insert()
	return doc, True


def get_item_mapping():
	rows = frappe.db.sql(
		"""
		select fi.item_code, slp.trade_item_id, slp.stem_length
		from `tabFloriday Items` fi
		join `tabStem Length Price` slp on slp.parent = fi.name
		where slp.parenttype = 'Floriday Items'
		and ifnull(slp.trade_item_id, '') != ''
		""",
		as_dict=True,
	)
	mapping = {}
	for r in rows:
		if r.item_code and r.item_code not in mapping:
			mapping[r.item_code] = r.trade_item_id
	return mapping


def get_item_code_from_trade_item_id(trade_item_id):
	if not trade_item_id:
		return None
	row = frappe.db.sql(
		"""
		select fi.item_code
		from `tabFloriday Items` fi
		join `tabStem Length Price` slp on slp.parent = fi.name
		where slp.parenttype = 'Floriday Items'
		and slp.trade_item_id = %s
		limit 1
		""",
		(trade_item_id,),
		as_dict=True,
	)
	return row[0].item_code if row else None


@frappe.whitelist()
def sync_system_items(force=False, price_list=None):
	if not force and not frappe.db.get_single_value("Floriday Settings", "fi_enabled"):
		return {"skipped": True, "reason": "Floriday Items sync is disabled (fi_enabled = 0)"}

	if price_list and not frappe.db.exists("Price List", price_list):
		frappe.throw(f"Price List '{price_list}' does not exist.")

	items = frappe.db.sql(
		"""
		SELECT i.name AS item_code, i.item_name, i.item_group
		FROM tabItem i
		WHERE i.disabled = 0
		  AND (i.variant_of IS NULL OR i.variant_of = '')
		  AND i.item_group REGEXP %s
		""",
		(_ITEM_GROUP_REGEXP,),
		as_dict=True,
	)

	created = 0
	updated_prices = 0
	skipped = 0
	for item in items:
		try:
			doc, was_created = _find_or_create_floriday_item(item)
			if was_created:
				created += 1
			doc.fetch_stem_length_prices(price_list=price_list)
			updated_prices += 1
		except Exception as e:
			skipped += 1
			frappe.log_error(
				f"sync_system_items failed for {item.item_code} / {item.item_name}: {e}",
				"Floriday Items Sync",
			)

	return {
		"items_processed": len(items),
		"floriday_docs_created": created,
		"price_refreshes": updated_prices,
		"skipped": skipped,
	}


@frappe.whitelist()
def update_trade_item_ids(force=False):
	if not force and not frappe.db.get_single_value("Floriday Settings", "fi_enabled"):
		return {"skipped": True, "reason": "Floriday Items sync is disabled (fi_enabled = 0)"}

	try:
		article_lookup = _fetch_floriday_trade_items()
	except Exception as e:
		frappe.log_error(f"Could not fetch trade items: {e}", "Floriday Items Sync")
		return {
			"trade_items_fetched": 0,
			"rows_matched": 0,
			"error": str(e),
		}

	floriday_docs = frappe.get_all("Floriday Items", pluck="name")
	total_matched = 0
	total_rows = 0
	unmatched = []
	docs_processed = 0
	docs_with_no_table = 0
	for name in floriday_docs:
		try:
			doc = frappe.get_doc("Floriday Items", name)
			if not doc.table_ppvq:
				docs_with_no_table += 1
				continue
			matched = doc.apply_trade_item_ids(article_lookup)
			for row in doc.table_ppvq:
				total_rows += 1
				if not row.trade_item_id:
					unmatched.append({
						"item_code": doc.item_code,
						"item_name": doc.item_name,
						"stem_length": row.stem_length,
					})
			if matched:
				doc.save()
				total_matched += matched
			docs_processed += 1
		except Exception as e:
			frappe.log_error(
				f"update_trade_item_ids failed for {name}: {e}",
				"Floriday Items Sync",
			)

	if unmatched and total_matched < total_rows:
		sample_lines = [
			f"{u['item_code']} ({u['item_name']}) / {u['stem_length']}"
			for u in unmatched[:10]
		]
		sample_keys = list(article_lookup.keys())[:10]
		frappe.log_error(
			"Unmatched rows (sample):\n"
			+ "\n".join(sample_lines)
			+ "\n\nFloriday lookup keys (sample):\n"
			+ "\n".join(repr(k) for k in sample_keys),
			"Floriday Items Sync — unmatched debug",
		)

	return {
		"trade_items_fetched": len(article_lookup),
		"docs_processed": docs_processed,
		"docs_with_no_table": docs_with_no_table,
		"rows_matched": total_matched,
		"total_rows": total_rows,
		"unmatched": unmatched,
	}


@frappe.whitelist()
def sync_floriday_items(force=False, price_list=None):
	system = sync_system_items(force=force, price_list=price_list)
	trade = update_trade_item_ids(force=force)
	return {**system, **trade}
