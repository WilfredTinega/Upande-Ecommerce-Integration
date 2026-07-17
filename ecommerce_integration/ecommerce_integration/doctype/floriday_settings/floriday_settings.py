# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
import requests
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document
from frappe.utils import flt

SCHEDULER_TASKS = [
	("at",         "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.refresh_access_token",    "Floriday: Refresh Access Token"),
	("fi",         "ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items.sync_floriday_items",            "Floriday: Sync Items"),
	("batch",      "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.run_create_batch",         "Floriday: Create Batches"),
	("supplyline", "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.run_supplyline",           "Floriday: Create Supply Lines"),
	("so",         "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.run_sales_order",          "Floriday: Sync Sales Orders"),
	("of",         "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.run_order_fullfilment",    "Floriday: Order Fulfillment"),
	("stock",      "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.run_refresh_stock",        "Floriday: Refresh Stock"),
]


class FloridaySettings(Document):
	def onload(self):
		"""Show last_run / next_run from each Scheduled Job Type when the form loads."""
		self._populate_scheduler_run_times()

	def validate(self):
		self._apply_used_warehouse()

	def _apply_used_warehouse(self):
		"""Enforce at most one `used` row in the warehouses table and mirror its
		warehouse_id / organization_id onto the parent settings fields, so the
		rest of the integration (which reads settings.warehouse_id /
		organization_supplier_id) picks up the user's choice without a separate
		save step."""
		rows = self.get("floriday_warehouses") or []
		used = [r for r in rows if r.get("used")]
		if len(used) > 1:
			frappe.throw("Only one Floriday warehouse can be marked as Used.")
		if used:
			r = used[0]
			self.warehouse_id = r.warehouse_id or self.warehouse_id
			if r.get("organization_id"):
				self.organization_supplier_id = r.organization_id

	@frappe.whitelist()
	def fetch_warehouses(self):
		return _fetch_floriday_warehouses(self)

	def _populate_scheduler_run_times(self):
		for prefix, method, _label in SCHEDULER_TASKS:
			row = frappe.db.get_value(
				"Scheduled Job Type",
				{"method": method},
				["name", "last_execution"],
				as_dict=True,
			)
			last_run = row.last_execution if row else None
			next_run = None
			if row and row.name:
				try:
					job = frappe.get_cached_doc("Scheduled Job Type", row.name)
					if not job.stopped:
						next_run = job.get_next_execution()
				except Exception:
					next_run = None
			# Use db_set with update_modified=False so we don't churn modified timestamps
			# every form load. These are presentation-only fields.
			self.set(f"{prefix}_last_run", last_run)
			self.set(f"{prefix}_next_run", next_run)

	@frappe.whitelist()
	def update_access_token(self):
		return _refresh_access_token(self)

	@frappe.whitelist()
	def sales_order(self):
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order import create_sales_orders_from_floriday
		return create_sales_orders_from_floriday()

	@frappe.whitelist()
	def create_batch(self, selected_rows=None):
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_batch import create_batches_on_floriday
		return create_batches_on_floriday(selected_rows=selected_rows)

	@frappe.whitelist()
	def create_supplyine(self):
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_supplyline import create_supply_lines_only_from_batches
		return create_supply_lines_only_from_batches()

	@frappe.whitelist()
	def order_fullfilment(self):
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_order_fullfillment import order_fullment
		return order_fullment()

	@frappe.whitelist()
	def load_stock_items(self):
		rows = get_floriday_stock(self.warehouse)
		self.set("stock_items", [])
		for r in rows:
			self.append("stock_items", r)
		return {"count": len(rows)}

	def on_update(self):
		self._sync_scheduled_jobs()

	def _sync_scheduled_jobs(self, force=False):
		"""Mirror the user's frequency/cron/enabled choices into Scheduled Job Type rows.

		One Scheduled Job Type per task, keyed by `method`. Same pattern Frappe's
		Server Script uses (see core/doctype/server_script/server_script.py).

		Per-task short-circuit: if none of (frequency, cron, enabled) changed for a
		task on this save, skip it entirely. Saving Floriday Settings should never
		reset last_execution or perturb a job whose schedule wasn't touched.

		Pass force=True to upsert all tasks regardless (used by after_migrate and
		the manual resync helper, since migrate wipes the rows).
		"""
		for prefix, method, _label in SCHEDULER_TASKS:
			fields = (
				f"{prefix}_event_frequency",
				f"{prefix}_cron_format",
				f"{prefix}_enabled",
			)
			if not force and not any(self.has_value_changed(f) for f in fields):
				continue  # nothing changed for this task — leave its job alone

			self._upsert_scheduled_job(prefix, method)

	def _upsert_scheduled_job(self, prefix, method):
		frequency = (self.get(f"{prefix}_event_frequency") or "").strip()
		cron_format = (self.get(f"{prefix}_cron_format") or "").strip()
		enabled = bool(self.get(f"{prefix}_enabled"))

		# Stopped if disabled, no frequency, or Cron without a cron string
		stopped = 1 if (not enabled or not frequency) else 0
		if frequency == "Cron" and not cron_format:
			stopped = 1

		# Frappe's `next_execution` getter parses cron_format unconditionally, even
		# for stopped rows. A Cron-frequency row with empty cron_format makes that
		# getter crash (NoneType.lower in croniter), which then breaks sync_jobs.
		# Downgrade to Daily placeholder when there's no usable cron string.
		effective_frequency = "Daily" if (frequency == "Cron" and not cron_format) else frequency

		job_name = frappe.db.get_value("Scheduled Job Type", {"method": method})

		if not job_name:
			if stopped:
				# Don't create a row for a task that's been off and never scheduled
				return
			job = frappe.new_doc("Scheduled Job Type")
			job.method = method
			job.create_log = effective_frequency not in ("All", "Cron")
			job.frequency = effective_frequency
			job.cron_format = cron_format if effective_frequency == "Cron" else ""
			job.stopped = 0
			job.insert(ignore_permissions=True)
			return

		# Existing row — only write fields that actually differ. Avoids touching
		# last_execution / modified when the schedule didn't really change.
		new_frequency = effective_frequency or "Daily"  # placeholder for stopped jobs (required field)
		new_cron = cron_format if effective_frequency == "Cron" else ""

		current = frappe.db.get_value(
			"Scheduled Job Type",
			job_name,
			["frequency", "cron_format", "stopped"],
			as_dict=True,
		)
		updates = {}
		if current.frequency != new_frequency:
			updates["frequency"] = new_frequency
		if (current.cron_format or "") != new_cron:
			updates["cron_format"] = new_cron
		if int(current.stopped or 0) != stopped:
			updates["stopped"] = stopped

		if updates:
			frappe.db.set_value("Scheduled Job Type", job_name, updates)


def _get_settings_doc():
	if frappe.get_meta("Floriday Settings").issingle:
		return frappe.get_single("Floriday Settings")
	settings_list = frappe.get_all("Floriday Settings", fields=["name"], limit_page_length=1)
	if not settings_list:
		frappe.throw("Floriday Settings doc not found. Please create it first.")
	return frappe.get_doc("Floriday Settings", settings_list[0].name)


def _refresh_access_token(doc=None):
	try:
		settings = doc or _get_settings_doc()

		if not (settings.token_url and settings.client_id and settings.client_secret and settings.scope):
			frappe.throw("token_url, client_id, client_secret, and scope are required on Floriday Settings.")

		payload = {
			"grant_type": settings.grant_type or "client_credentials",
			"client_id": settings.client_id,
			"client_secret": settings.client_secret,
			"scope": settings.scope,
		}
		headers = {"Content-Type": "application/x-www-form-urlencoded"}

		response = make_post_request(settings.token_url, data=payload, headers=headers)

		if not (response and response.get("access_token")):
			frappe.throw(f"Token endpoint returned no access_token. Response: {response}")

		settings.access_token = response["access_token"]
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		return {"status": "success"}

	except Exception as e:
		frappe.log_error(message=str(e), title="Floriday Token Exception")
		raise


@frappe.whitelist()
def refresh_access_token():
	if not frappe.db.get_single_value("Floriday Settings", "at_enabled"):
		return {"skipped": True, "reason": "Update Access Token is disabled (at_enabled = 0)"}
	return _refresh_access_token()


@frappe.whitelist()
def run_sales_order():
	if not frappe.db.get_single_value("Floriday Settings", "so_enabled"):
		return {"skipped": True, "reason": "Sales Order is disabled (so_enabled = 0)"}
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_sales_order import create_sales_orders_from_floriday
	return create_sales_orders_from_floriday()


@frappe.whitelist()
def run_create_batch():
	if not frappe.db.get_single_value("Floriday Settings", "batch_enabled"):
		return {"skipped": True, "reason": "Create Batch is disabled (batch_enabled = 0)"}
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_batch import create_batches_on_floriday
	return create_batches_on_floriday()


@frappe.whitelist()
def run_supplyline():
	if not frappe.db.get_single_value("Floriday Settings", "supplyline_enabled"):
		return {"skipped": True, "reason": "Supplyline is disabled (supplyline_enabled = 0)"}
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_supplyline import create_supply_lines_only_from_batches
	return create_supply_lines_only_from_batches()


@frappe.whitelist()
def run_order_fullfilment():
	if not frappe.db.get_single_value("Floriday Settings", "of_enabled"):
		return {"skipped": True, "reason": "Order Fullfilment is disabled (of_enabled = 0)"}
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_order_fullfillment import order_fullment
	return order_fullment()


@frappe.whitelist()
def run_refresh_stock():
	if not frappe.db.get_single_value("Floriday Settings", "stock_enabled"):
		return {"skipped": True, "reason": "Refresh Stock is disabled (stock_enabled = 0)"}
	# Stock View is recomputed from SLE on every form open; the cron just
	# returns the count for monitoring. Don't persist back to the doc — that
	# creates stale rows users see until they next Refresh.
	rows = get_floriday_stock()
	return {"status": "success", "count": len(rows)}


@frappe.whitelist()
def resync_scheduled_jobs():
	"""Re-upsert ALL Floriday-driven scheduled jobs from current settings.

	Wired into hooks.after_migrate because Frappe's sync_jobs deletes any
	Scheduled Job Type whose method isn't declared in scheduler_events.
	"""
	doc = _get_settings_doc()
	doc._sync_scheduled_jobs(force=True)
	frappe.db.commit()
	return {
		"jobs": frappe.get_all(
			"Scheduled Job Type",
			filters={"method": ["like", "%ecommerce_integration%"]},
			fields=["method", "frequency", "cron_format", "stopped"],
			order_by="method",
		)
	}


def _get_floriday_item_index():
	"""Build a lookup of (item_code, stem_length) -> trade_item_id from Floriday Items.

	Only rows with a trade_item_id set are included — those are the ones actually
	offered to Floriday and the ones the Stock tab cares about.
	"""
	rows = frappe.db.sql(
		"""
		SELECT fi.item_code, fi.item_name, slp.stem_length, slp.trade_item_id
		FROM `tabFloriday Items` fi
		INNER JOIN `tabStem Length Price` slp ON slp.parent = fi.name
		WHERE slp.parenttype = 'Floriday Items'
		AND slp.trade_item_id IS NOT NULL AND slp.trade_item_id != ''
		""",
		as_dict=True,
	)
	by_code_length = {}
	by_code = {}
	for r in rows:
		key = (r.item_code, (r.stem_length or "").strip())
		by_code_length[key] = r
		by_code.setdefault(r.item_code, []).append(r)
	return by_code_length, by_code


# Variant-attribute name varies between sites: kaitet uses "Length", mona uses
# "Stem Length". We try both and pick whichever a given template uses.
_STEM_LENGTH_ATTR_CANDIDATES = ("Stem Length", "Length")


def _resolve_variant_item(template_code, stem_length):
	"""Given a template item_code + stem_length, return the variant item_code.

	If the template has no variants, returns template_code itself.
	If the template has variants under a `Stem Length` (or `Length`) attribute,
	matches the variant whose attribute_value normalizes to the same stem_length.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import _normalize_stem_length

	target = _normalize_stem_length(stem_length)
	if not target:
		return template_code

	variants = frappe.db.sql(
		"""
		SELECT i.name AS item_code, iva.attribute, iva.attribute_value
		FROM `tabItem` i
		INNER JOIN `tabItem Variant Attribute` iva ON iva.parent = i.name
		WHERE i.variant_of = %s AND iva.attribute IN %s
		""",
		(template_code, _STEM_LENGTH_ATTR_CANDIDATES),
		as_dict=True,
	)
	if not variants:
		return template_code

	for v in variants:
		if _normalize_stem_length(v.attribute_value) == target:
			return v.item_code
	return template_code


def _site_has_sle_stem_length():
	"""Detect whether `tabStock Ledger Entry` has the `custom_stem_length` column.

	Cached per request. On kaitet this is True (stem length is captured in a
	custom field on SLE). On mona this is False — stem length is encoded in
	variant item codes instead.
	"""
	if frappe.local.flags.get("_floriday_sle_stem_length_present") is not None:
		return frappe.local.flags["_floriday_sle_stem_length_present"]
	cols = frappe.db.sql(
		"SHOW COLUMNS FROM `tabStock Ledger Entry` LIKE 'custom_stem_length'"
	)
	present = bool(cols)
	frappe.local.flags["_floriday_sle_stem_length_present"] = present
	return present


def _site_has_se_detail_stem_length():
	"""Detect whether `tabStock Entry Detail` has `custom_stem_length`."""
	if frappe.local.flags.get("_floriday_sed_stem_length_present") is not None:
		return frappe.local.flags["_floriday_sed_stem_length_present"]
	cols = frappe.db.sql(
		"SHOW COLUMNS FROM `tabStock Entry Detail` LIKE 'custom_stem_length'"
	)
	present = bool(cols)
	frappe.local.flags["_floriday_sed_stem_length_present"] = present
	return present


def _variant_to_template_index():
	"""Map every variant item_code → (template_code, stem_length_attr_value).

	Used on variant-driven sites (e.g. mona) so we can attribute warehouse stock
	to a template + stem length. Returns {} on sites that don't use variants.
	"""
	rows = frappe.db.sql(
		"""
		SELECT i.name AS variant, i.variant_of AS template, iva.attribute_value AS stem_length
		FROM `tabItem` i
		INNER JOIN `tabItem Variant Attribute` iva ON iva.parent = i.name
		WHERE i.variant_of IS NOT NULL AND i.variant_of != ''
		AND iva.attribute IN %s
		""",
		(_STEM_LENGTH_ATTR_CANDIDATES,),
		as_dict=True,
	)
	return {r.variant: (r.template, r.stem_length) for r in rows}


def _available_for_sale_warehouses(company=None):
	"""Return names of all warehouses whose name contains 'Available for Sale',
	optionally restricted to a company."""
	filters = {"warehouse_name": ["like", "%Available for Sale%"]}
	if company:
		filters["company"] = company
	rows = frappe.get_all("Warehouse", filters=filters, pluck="name")
	return rows


def _floriday_flagged_qty_map(item_codes, warehouses):
	"""Per-(item_code, normalized stem length) qty available for sale on Floriday.

	Returns None when the shelf flag is off (caller keeps the SLE qty). Otherwise
	returns {(item_code, "52cm"): qty, ...}.

	With shelf mode on, the published qty is the RAW shelf total — every stem
	physically on a `Shelf` (Shelf Item.stem_qty), summed per (variety, stem
	length), the same source the storefront reads. This offers all shelf stems
	to Floriday regardless of whether they've been staged into the online
	warehouse.

	`Shelf Item.variety` is the plain item code and `Shelf Item.stem_length` is
	the Stem Length name (e.g. "52cm"). On kaitet (custom-field model) the SLE
	rows at the call site are keyed on the same plain item code, so the keys
	align. Keys use _normalize_stem_length on both sides so "52cm"/"52"/"52 cm"
	match the call site's normalized stem length.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import (
		_normalize_stem_length,
	)
	from ecommerce_integration.ecommerce_integration.utils.shelf_stock import (
		shelf_stock_enabled,
		get_shelf_qty_by_length,
	)

	if not shelf_stock_enabled("Floriday Settings"):
		return None

	# Shelf rows only ever carry the plain/template item code (Shelf Item.variety);
	# no variant ever sits on a shelf. On a variant-model site the SLE item_codes
	# are variant codes, which would never match a shelf row — so substituting
	# would zero out every row. Refuse to apply shelf qty there (keep SLE qty)
	# rather than silently publishing 0.
	if not _site_has_sle_stem_length():
		return None

	if not item_codes:
		return {}

	qty_map = {}
	for code in item_codes:
		# {stem_length_name: total_stems} across all shelves for this item.
		for stem_length_name, qty in get_shelf_qty_by_length(code).items():
			key = (code, _normalize_stem_length(stem_length_name))
			qty_map[key] = qty_map.get(key, 0.0) + flt(qty)
	return qty_map


def _aggregate_floriday_stock(warehouses, apply_stock_source=False):
	"""SLE-aggregated per-(warehouse, item, stem_length) balances joined to
	Floriday Items. Handles two data models transparently:

	- Custom-field model (kaitet): stem length lives in
	  `tabStock Ledger Entry.custom_stem_length`; aggregation groups by it.
	- Variant model (mona): each stem length is its own variant item_code (e.g.
	  Alicia-50cm). Aggregation groups by item_code only; stem length is read
	  from the variant attribute and the template's Floriday mapping.

	`apply_stock_source`: when True (the Floriday-warehouse / publish path), the
	shelf or age-bin qty override is applied — the published qty is taken from
	that source instead of the SLE sum. The System-stock view passes False so it
	always reports real per-warehouse SLE balances, never the global shelf total.
	"""
	by_code_length, by_code = _get_floriday_item_index()
	if not by_code_length or not warehouses:
		return []

	floriday_templates = list(by_code.keys())
	if not floriday_templates:
		return []

	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import _normalize_stem_length

	use_sle_stem = _site_has_sle_stem_length()

	# Resolve the set of item codes to query SLE for. Custom-field sites query
	# the template (or whatever item_code is on Floriday Items). Variant sites
	# also need every variant of those templates.
	candidate_items = set(floriday_templates)
	variant_to_template = {}
	if not use_sle_stem:
		variant_to_template = _variant_to_template_index()
		for variant, (template, _sl) in variant_to_template.items():
			if template in by_code:
				candidate_items.add(variant)

	if not candidate_items:
		return []

	# Stock-source override: when Floriday Settings opts into shelf or age-bin
	# stock, the published qty for each (item, stem length) is taken from that
	# source instead of the SLE sum. The SLE aggregation still drives WHICH
	# (item, length, trade_item) rows exist; only the qty is swapped. Keyed by
	# (item_code, normalized stem length). See _floriday_flagged_qty_map.
	# Only applied on the publish path (Floriday warehouse), never the system view.
	flagged_qty = _floriday_flagged_qty_map(candidate_items, warehouses) if apply_stock_source else None

	if use_sle_stem:
		sle_rows = frappe.db.sql(
			"""
			SELECT sle.warehouse,
			       sle.item_code,
			       COALESCE(NULLIF(TRIM(sle.custom_stem_length), ''), '') AS stem_length,
			       SUM(sle.actual_qty) AS qty,
			       MAX(sle.stock_uom) AS uom,
			       MAX(i.item_name) AS item_name
			FROM `tabStock Ledger Entry` sle
			INNER JOIN `tabItem` i ON i.name = sle.item_code
			WHERE sle.warehouse IN %(warehouses)s
			  AND sle.is_cancelled = 0
			  AND sle.item_code IN %(codes)s
			GROUP BY sle.warehouse, sle.item_code, COALESCE(NULLIF(TRIM(sle.custom_stem_length), ''), '')
			HAVING SUM(sle.actual_qty) > 0
			""",
			{"warehouses": tuple(warehouses), "codes": tuple(candidate_items)},
			as_dict=True,
		)
	else:
		sle_rows = frappe.db.sql(
			"""
			SELECT sle.warehouse,
			       sle.item_code,
			       '' AS stem_length,
			       SUM(sle.actual_qty) AS qty,
			       MAX(sle.stock_uom) AS uom,
			       MAX(i.item_name) AS item_name
			FROM `tabStock Ledger Entry` sle
			INNER JOIN `tabItem` i ON i.name = sle.item_code
			WHERE sle.warehouse IN %(warehouses)s
			  AND sle.is_cancelled = 0
			  AND sle.item_code IN %(codes)s
			GROUP BY sle.warehouse, sle.item_code
			HAVING SUM(sle.actual_qty) > 0
			""",
			{"warehouses": tuple(warehouses), "codes": tuple(candidate_items)},
			as_dict=True,
		)

	results = []
	for row in sle_rows:
		# Resolve template + stem_length for this SLE row.
		if row.item_code in by_code:
			# SLE keyed on the same code as Floriday Items (custom-field model)
			template_code = row.item_code
			row_stem = row.stem_length
		elif row.item_code in variant_to_template:
			# Variant model: SLE is the variant; lift to template + variant's stem length
			template_code, row_stem = variant_to_template[row.item_code]
		else:
			continue

		mapping = by_code.get(template_code, [])
		norm_target = _normalize_stem_length(row_stem)

		match = None
		if norm_target:
			for r in mapping:
				if _normalize_stem_length(r.stem_length) == norm_target:
					match = r
					break
		if not match:
			continue

		# Swap in shelf/age-bin qty when a stock-source flag is on. The map is
		# keyed by (item_code, normalized stem length); fall back to the SLE qty
		# only when no flag is active (flagged_qty is None then).
		# Keep every SLE row even when its shelf qty is 0 — the row set must match
		# the normal-warehouse view; only the qty is swapped (the batch-creation
		# step decides what to actually post, e.g. the 200-multiple minimum).
		if flagged_qty is not None:
			qty = flagged_qty.get((row.item_code, norm_target), 0.0)
		else:
			qty = float(row.qty)

		results.append({
			"warehouse": row.warehouse,
			"item_code": row.item_code,
			"item_name": row.item_name,
			"stem_length": row_stem or match.stem_length,
			"trade_item_id": match.trade_item_id,
			"qty": qty,
			"uom": row.uom,
		})

	results.sort(key=lambda x: (x["warehouse"] or "", x["item_name"] or "", x["stem_length"] or ""))
	return results


@frappe.whitelist()
def get_floriday_stock(warehouse=None):
	"""Per-(item, stem_length) balances in the configured Floriday warehouse
	(Online Available for Sale), computed from Stock Ledger Entry and joined to
	Floriday trade items.

	`warehouse` defaults to Floriday Settings.warehouse. Only rows with positive
	qty AND a Floriday trade_item_id mapping are returned.
	"""
	if not warehouse:
		warehouse = _get_settings_doc().warehouse
	if not warehouse:
		return []
	return _aggregate_floriday_stock([warehouse], apply_stock_source=True)


@frappe.whitelist()
def get_floriday_batch_rows():
	"""Batch rows derived from the items ENABLED on the Stock tab.

	The Stock tab's Enable/Disable picker publishes rows by flipping
	`Stem Length Price.enabled` (see shelf_move.js / set_webshop_enabled_stock).
	Those enabled rows — not the separate "Shelf Stock Items" picker — are the
	source for batching: every enabled (item, stem length) that also has a
	Floriday `trade_item_id` mapping is returned, with its published qty
	(Stem Length Price.stock_qty) floored to a 200 multiple.

	Returns a list of {item_code, item_name, stem_length, trade_item_id, qty},
	one per batchable enabled row (qty >= 200, mapping present). Rows without a
	Floriday mapping or below 200 stems are dropped.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import (
		_normalize_stem_length,
	)
	from ecommerce_integration.ecommerce_integration.utils.webshop_stock import (
		get_webshop_enabled_rows,
	)

	BATCH_MULTIPLE = 200

	by_code_length, by_code = _get_floriday_item_index()
	if not by_code:
		return []

	# Index Floriday mappings by (item_code, normalized length) so a published
	# "52cm"/"52" length matches the mapping's length regardless of formatting.
	mapping_by_norm = {}
	for code, mappings in by_code.items():
		for m in mappings:
			mapping_by_norm[(code, _normalize_stem_length(m.stem_length))] = m

	rows = []
	for r in get_webshop_enabled_rows():
		match = mapping_by_norm.get(
			(r.get("item_code"), _normalize_stem_length(r.get("stem_length")))
		)
		if not match:
			continue  # enabled length not offered to Floriday — can't batch it
		qty = int(flt(r.get("stock_qty")) // BATCH_MULTIPLE * BATCH_MULTIPLE)
		if qty < BATCH_MULTIPLE:
			continue
		rows.append({
			"item_code": r.get("item_code"),
			"item_name": r.get("item_name") or match.item_name,
			"stem_length": r.get("stem_length") or match.stem_length,
			"trade_item_id": match.trade_item_id,
			"qty": qty,
		})

	rows.sort(key=lambda x: (x["item_name"] or "", x["stem_length"] or ""))
	return rows


@frappe.whitelist()
def get_floriday_shelf_rows():
	"""Shelf-mode batch picker rows: every (item, stem length) sitting on a Shelf
	that ALSO has a Floriday trade_item_id mapping.

	Returns a list of
	  {item_code, item_name, stem_length, trade_item_id, shelf_qty}
	one row per (variety, stem length) with shelf stock. `shelf_qty` is the raw
	total stems on shelves (Shelf Item.stem_qty), the same source the storefront
	reads. Only rows whose (item_code, normalized length) maps to a Floriday trade
	item are returned — others can't be batched.

	Used by the "Shelf Stock Items" panel on Floriday Settings (shelf mode on).
	The qty actually batched is chosen per-row in the UI, not here.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import (
		_normalize_stem_length,
	)
	from ecommerce_integration.ecommerce_integration.utils.shelf_stock import (
		shelf_stock_enabled,
		get_shelf_qty_by_length,
	)

	if not shelf_stock_enabled("Floriday Settings"):
		return []

	_by_code_length, by_code = _get_floriday_item_index()
	if not by_code:
		return []

	rows = []
	for item_code, mappings in by_code.items():
		shelf_by_length = get_shelf_qty_by_length(item_code)
		if not shelf_by_length:
			continue

		# Index this item's Floriday mappings by normalized stem length.
		mapping_by_norm = {}
		for m in mappings:
			mapping_by_norm[_normalize_stem_length(m.stem_length)] = m

		for stem_length_name, qty in shelf_by_length.items():
			norm = _normalize_stem_length(stem_length_name)
			match = mapping_by_norm.get(norm)
			if not match:
				continue  # shelf length not offered to Floriday — can't batch it
			rows.append({
				"item_code": item_code,
				"item_name": match.item_name,
				# Prefer the shelf's own length label (what the user sees on the shelf).
				"stem_length": stem_length_name or match.stem_length,
				"trade_item_id": match.trade_item_id,
				"shelf_qty": flt(qty),
			})

	rows.sort(key=lambda r: (r["item_name"] or "", r["stem_length"] or ""))
	return rows


@frappe.whitelist()
def get_system_floriday_stock():
	"""Per-(warehouse, item, stem_length) balances across all 'Available for
	Sale' warehouses EXCEPT the configured Floriday warehouse.

	Filtered to rows with qty > 1000 (small balances aren't worth tracking
	here) and sorted by qty descending.
	"""
	settings = _get_settings_doc()
	floriday_warehouse = settings.warehouse
	company = _pick_floriday_company()
	all_afs = _available_for_sale_warehouses(company=company)
	system_warehouses = [w for w in all_afs if w != floriday_warehouse]
	if not system_warehouses:
		return []
	rows = _aggregate_floriday_stock(system_warehouses)
	rows = [r for r in rows if (r.get("qty") or 0) > 1000]
	rows.sort(key=lambda r: -(r.get("qty") or 0))
	return rows


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def item_query_with_stock(doctype, txt, searchfield, start, page_len, filters):
	"""Link query: return Items with positive bin balance in `filters.warehouse`.

	Used by the Add/Move dialog so the Item picker only shows items actually
	present at the selected Source Warehouse — preventing typos from selecting
	near-duplicate Items (e.g. 'Belalinda' vs 'Bellalinda').
	"""
	warehouse = (filters or {}).get("warehouse")
	if not warehouse:
		return []
	txt = f"%{(txt or '').strip()}%"
	rows = frappe.db.sql(
		"""
		SELECT i.name, i.item_name, i.item_group
		FROM `tabBin` b
		INNER JOIN `tabItem` i ON i.name = b.item_code
		WHERE b.warehouse = %(wh)s
		  AND b.actual_qty > 0
		  AND (i.name LIKE %(txt)s OR i.item_name LIKE %(txt)s)
		ORDER BY i.item_name
		LIMIT %(start)s, %(page_len)s
		""",
		{"wh": warehouse, "txt": txt, "start": start or 0, "page_len": page_len or 20},
	)
	return rows


@frappe.whitelist()
def get_warehouse_stock_items(warehouse=None):
	"""Return items with positive bin balance in the given warehouse.

	Used by the Move Stock dialog to filter the Item Link to items actually present
	in the Floriday warehouse. Defaults to Floriday Settings.warehouse.
	"""
	if not warehouse:
		warehouse = _get_settings_doc().warehouse
	if not warehouse:
		return []

	rows = frappe.db.sql(
		"""
		SELECT b.item_code, b.actual_qty, b.stock_uom, i.item_name
		FROM `tabBin` b
		INNER JOIN `tabItem` i ON i.name = b.item_code
		WHERE b.warehouse = %s AND b.actual_qty > 0
		ORDER BY i.item_name
		""",
		(warehouse,),
		as_dict=True,
	)
	return rows


@frappe.whitelist()
def get_item_floriday_meta(item_code):
	"""Given a Stock-level item_code (could be variant or template), return any
	matching Floriday stem_length + trade_item_id mapping plus stock UOM.

	Returns {item_name, stock_uom, stem_length, trade_item_id} where stem_length /
	trade_item_id may be empty when the item isn't mapped.
	"""
	if not item_code:
		return {}

	item = frappe.db.get_value(
		"Item", item_code, ["item_name", "stock_uom", "variant_of"], as_dict=True
	)
	if not item:
		return {}

	out = {
		"item_name": item.item_name,
		"stock_uom": item.stock_uom,
		"stem_length": "",
		"trade_item_id": "",
	}

	from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import _normalize_stem_length

	# If item is a variant, its template is the Floriday Items key; resolve length from variant attribute.
	template = item.variant_of or item_code
	length_value = ""
	if item.variant_of:
		length_attr = frappe.db.get_value(
			"Item Variant Attribute",
			{"parent": item_code, "attribute": "Length"},
			"attribute_value",
		)
		length_value = length_attr or ""

	# Pull Floriday Items rows for the template
	rows = frappe.db.sql(
		"""
		SELECT slp.stem_length, slp.trade_item_id
		FROM `tabFloriday Items` fi
		INNER JOIN `tabStem Length Price` slp ON slp.parent = fi.name
		WHERE slp.parenttype = 'Floriday Items'
		AND fi.item_code = %s
		AND slp.trade_item_id IS NOT NULL AND slp.trade_item_id != ''
		ORDER BY slp.stem_length
		""",
		(template,),
		as_dict=True,
	)
	out["stem_length_options"] = [
		{"stem_length": r.stem_length, "trade_item_id": r.trade_item_id}
		for r in rows
	]
	if not rows:
		return out

	if length_value:
		norm_target = _normalize_stem_length(length_value)
		for r in rows:
			if _normalize_stem_length(r.stem_length) == norm_target:
				out["stem_length"] = r.stem_length
				out["trade_item_id"] = r.trade_item_id
				return out
	# Non-variant or no match yet — if there is exactly one mapping, use it
	if len(rows) == 1:
		out["stem_length"] = rows[0].stem_length
		out["trade_item_id"] = rows[0].trade_item_id

	return out


@frappe.whitelist()
def get_floriday_company():
	"""Return the company that Floriday stock entries should use."""
	return _pick_floriday_company()


@frappe.whitelist()
def get_floriday_item_options():
	"""Return Floriday Items with their stem_length / trade_item_id rows for the Add Stock dialog."""
	by_code_length, by_code = _get_floriday_item_index()
	out = []
	for item_code, rows in by_code.items():
		out.append({
			"item_code": item_code,
			"item_name": rows[0].item_name,
			"stem_lengths": [
				{"stem_length": r.stem_length, "trade_item_id": r.trade_item_id}
				for r in rows
			],
		})
	out.sort(key=lambda x: x["item_name"] or "")
	return out


def _pick_floriday_company():
	"""Pick the company to use for Floriday Stock Entries.

	Rule (per user): always prefer a company with 'roses' or 'flower' in its name.
	If exactly one company exists, use it. If multiple, pick the first whose name
	contains 'roses' or 'flower' (case-insensitive); otherwise fall back to the
	default company on Global Defaults.
	"""
	companies = frappe.get_all("Company", fields=["name"], order_by="name")
	if not companies:
		frappe.throw("No Company found")
	if len(companies) == 1:
		return companies[0].name
	for c in companies:
		nm = (c.name or "").lower()
		if "rose" in nm or "flower" in nm:
			return c.name
	default = frappe.db.get_single_value("Global Defaults", "default_company")
	if default:
		return default
	return companies[0].name


def _available_qty_at(warehouse, item_code, stem_length):
	"""Return SLE-summed actual_qty for (item, warehouse, stem_length).

	On variant sites the variant item_code already carries the stem-length
	dimension, so we sum SLE for that item without filtering on
	`custom_stem_length` (which doesn't exist).
	"""
	if _site_has_sle_stem_length():
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(actual_qty), 0) AS qty
			FROM `tabStock Ledger Entry`
			WHERE warehouse = %(wh)s
			  AND item_code = %(ic)s
			  AND is_cancelled = 0
			  AND COALESCE(NULLIF(TRIM(custom_stem_length), ''), '') = %(sl)s
			""",
			{"wh": warehouse, "ic": item_code, "sl": (stem_length or "").strip()},
			as_dict=True,
		)
	else:
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(actual_qty), 0) AS qty
			FROM `tabStock Ledger Entry`
			WHERE warehouse = %(wh)s
			  AND item_code = %(ic)s
			  AND is_cancelled = 0
			""",
			{"wh": warehouse, "ic": item_code},
			as_dict=True,
		)
	return float(row[0].qty) if row else 0.0


@frappe.whitelist()
def preview_scheduler_run_times():
	"""Diagnostic: show what the form will display in last_run/next_run for each task."""
	doc = _get_settings_doc()
	doc._populate_scheduler_run_times()
	out = {}
	for prefix, method, label in SCHEDULER_TASKS:
		out[label] = {
			"method": method,
			"last_run": doc.get(f"{prefix}_last_run"),
			"next_run": doc.get(f"{prefix}_next_run"),
		}
	return out


def _fetch_floriday_warehouses(doc):
	"""GET {base_url}/warehouses and replace the child table with the owned,
	non-deleted warehouses. The first owned row's organization is mirrored
	into organization_supplier_id so downstream code that already reads that
	field keeps working.

	Preserves any existing `used` selection: if the user had ticked a row for
	warehouse X, and X comes back in the new response, X stays ticked.
	"""
	base_url = (doc.base_url or "").rstrip("/")
	if not (base_url and doc.api_key and doc.access_token):
		frappe.throw("base_url, api_key, and access_token are required on Floriday Settings.")

	headers = {
		"Authorization": f"Bearer {doc.access_token}",
		"X-Api-Key": doc.api_key,
		"Accept": "application/json",
	}

	url = f"{base_url}/warehouses"
	try:
		response = requests.get(url, headers=headers, timeout=30)
	except requests.RequestException as e:
		frappe.log_error(message=str(e), title="Floriday Fetch Warehouses Exception")
		frappe.throw(f"Floriday warehouses request failed: {e}")

	if response.status_code != 200:
		frappe.log_error(
			message=f"HTTP {response.status_code}: {response.text[:1000]}",
			title="Floriday Fetch Warehouses HTTP Error",
		)
		frappe.throw(f"Floriday returned HTTP {response.status_code}. See error log.")

	payload = response.json() or []
	# API returns a list, but some endpoints wrap in {results: [...]}. Be defensive.
	if isinstance(payload, dict):
		items = payload.get("results") or payload.get("warehouses") or []
	else:
		items = payload

	# Keep only warehouses the credentials own and that are live.
	owned = [
		w for w in items
		if isinstance(w, dict)
		and w.get("hasAccess")
		and not w.get("isExternal")
		and not w.get("isDeleted")
	]

	previously_used = {
		r.warehouse_id for r in (doc.get("floriday_warehouses") or []) if r.get("used")
	}

	doc.set("floriday_warehouses", [])
	for w in owned:
		wid = w.get("warehouseId") or ""
		doc.append("floriday_warehouses", {
			"warehouse_id": wid,
			"warehouse_name": w.get("name") or "",
			"organization_id": w.get("organizationId") or "",
			"used": 1 if wid in previously_used else 0,
		})

	if owned and not doc.organization_supplier_id:
		doc.organization_supplier_id = owned[0].get("organizationId") or ""

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {"status": "success", "count": len(owned), "total": len(items)}


