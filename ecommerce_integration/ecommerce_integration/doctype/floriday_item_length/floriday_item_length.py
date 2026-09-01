# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FloridayItemLength(Document):
	def refresh_trade_item_id(self, article_lookup, item_name=None):
		"""Set `trade_item_id` from Floriday's article list. True when it changed.

		`article_lookup` is {(normalised item name, floriday length): trade item id},
		built once per sync by `floriday_items.fetch_trade_item_ids`. Matching is on
		the item NAME plus the length, because Floriday has no notion of our item
		codes — see `_normalize_name` / `_floriday_length_for` for the two sides of
		that key.

		Ported from upande_webshop's `Stem Length Price`, which this child doctype
		replaced; the Floriday sync calls it per row, so it has to live here.
		"""
		# Imported lazily: floriday_items imports this module's parent doctype, so a
		# module-level import would be circular.
		from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import (
			_floriday_length_for,
			_normalize_name,
		)

		if not self.stem_length:
			return False

		if not item_name and self.parent:
			item_name = frappe.db.get_value("Floriday Items", self.parent, "item_name")
		if not item_name:
			return False

		name_norm = _normalize_name(item_name)
		floriday_length = _floriday_length_for(self.stem_length)
		if not name_norm or floriday_length is None:
			return False

		trade_item_id = article_lookup.get((name_norm, floriday_length))
		if not trade_item_id:
			return False

		self.trade_item_id = trade_item_id
		return True
