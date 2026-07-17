# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import json

import frappe
import requests
from frappe.model.document import Document
from frappe.utils import flt

from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
	get_biflorica_flower_variety,
	get_item_price,
	get_stem_length_from_stock_entry,
	get_warehouse_stock_items,
	post_all_items_to_biflorica,
)
from ecommerce_integration.ecommerce_integration.utils import create_orders_as_quotation

_logger = frappe.logger("biflorica", allow_site=True)


def _biflorica_deal_exists(deal_ref):
	"""True if this Biflorica deal was already imported — as a live Sales Order
	or a draft Quotation (by po_no). Cancelled docs (docstatus 2) don't count.

	po_no is standard on Sales Order; on Quotation it must be added as a custom
	field for de-duplication to work in Quotation mode (guarded here, so the
	check is simply skipped where the column is absent)."""
	for dt in ("Sales Order", "Quotation"):
		if not frappe.db.has_column(dt, "po_no"):
			continue
		name = frappe.db.get_value(dt, {"po_no": deal_ref, "docstatus": ["<", 2]}, "name")
		if name:
			return dt, name
	return None, None


# One scheduled job per prefix. Deals and predeals share a single job
# (the "deals" prefix drives its frequency); the job runs get_deals and/or
# get_predeals depending on which is individually enabled.
SCHEDULER_TASKS = [
	("at",      "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_update_access_token",        "Biflorica: Refresh Access Token"),
	("offer",   "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_post_offers",                "Biflorica: Post Offers"),
	("deals",   "ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_sync_deals_and_predeals",    "Biflorica: Sync Deals & Predeals"),
]


class BifloricaSetting(Document):
	def onload(self):
		self._populate_scheduler_run_times()

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
			self.set(f"{prefix}_last_run", last_run)
			self.set(f"{prefix}_next_run", next_run)

		# Predeals share the deals job, so mirror its run times onto the
		# Predeals tab's read-only run-time fields.
		self.set("predeal_last_run", self.get("deals_last_run"))
		self.set("predeal_next_run", self.get("deals_next_run"))

	def on_update(self):
		self._sync_scheduled_jobs()

	def _sync_scheduled_jobs(self, force=False):
		for prefix, method, _label in SCHEDULER_TASKS:
			fields = [
				f"{prefix}_event_frequency",
				f"{prefix}_cron_format",
				f"{prefix}_enabled",
			]
			# The combined deals job is also gated by predeal_enabled.
			if prefix == "deals":
				fields.append("predeal_enabled")
			if not force and not any(self.has_value_changed(f) for f in fields):
				continue
			self._upsert_scheduled_job(prefix, method)

	def _upsert_scheduled_job(self, prefix, method):
		frequency = (self.get(f"{prefix}_event_frequency") or "").strip()
		cron_format = (self.get(f"{prefix}_cron_format") or "").strip()
		enabled = bool(self.get(f"{prefix}_enabled"))
		# The shared deals job runs if deals OR predeals are enabled.
		if prefix == "deals":
			enabled = enabled or bool(self.get("predeal_enabled"))

		stopped = 1 if (not enabled or not frequency) else 0
		if frequency == "Cron" and not cron_format:
			stopped = 1

		effective_frequency = "Daily" if (frequency == "Cron" and not cron_format) else frequency

		job_name = frappe.db.get_value("Scheduled Job Type", {"method": method})

		if not job_name:
			if stopped:
				return
			job = frappe.new_doc("Scheduled Job Type")
			job.method = method
			job.create_log = effective_frequency not in ("All", "Cron")
			job.frequency = effective_frequency
			job.cron_format = cron_format if effective_frequency == "Cron" else ""
			job.stopped = 0
			job.insert(ignore_permissions=True)
			return

		new_frequency = effective_frequency or "Daily"
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


# Scheduled jobs that used to exist but have been merged/renamed; removed on
# resync so they stop firing (their methods no longer exist).
_OBSOLETE_SCHEDULER_METHODS = [
	"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_get_deals",
	"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_get_predeals",
]


@frappe.whitelist()
def resync_scheduled_jobs():
	doc = _get_settings()
	# Drop obsolete jobs (e.g. the separate deals/predeals jobs now merged).
	for method in _OBSOLETE_SCHEDULER_METHODS:
		name = frappe.db.get_value("Scheduled Job Type", {"method": method})
		if name:
			frappe.delete_doc("Scheduled Job Type", name, force=True, ignore_permissions=True)
	doc._sync_scheduled_jobs(force=True)
	frappe.db.commit()
	return {
		"jobs": frappe.get_all(
			"Scheduled Job Type",
			filters={"method": ["like", "%biflorica_setting%"]},
			fields=["method", "frequency", "cron_format", "stopped"],
			order_by="method",
		)
	}


@frappe.whitelist()
def run_update_access_token():
	if not frappe.db.get_single_value("Biflorica Setting", "at_enabled"):
		return {"skipped": True, "reason": "Update Access Token disabled"}
	return update_access_token()


@frappe.whitelist()
def run_post_offers():
	if not frappe.db.get_single_value("Biflorica Setting", "offer_enabled"):
		return {"skipped": True, "reason": "Post Offers disabled"}
	return post_offers()


@frappe.whitelist()
def run_sync_deals_and_predeals():
	"""Single scheduled job for both deals and predeals.

	Runs get_deals when deals_enabled, and get_predeals (drafts only) when
	predeal_enabled — each independently gated, but on one shared schedule.
	"""
	settings = _get_settings()
	# Rolling window: fetch only what changed since the last interval, measured
	# from the moment this run starts (now - frequency). The shared job's cadence
	# comes from the deals frequency.
	window_from = _frequency_window_from(settings, "deals")

	result = {"deals": None, "predeals": None}
	if settings.deals_enabled:
		result["deals"] = get_deals(window_from=window_from)
	else:
		result["deals"] = {"skipped": True, "reason": "Get Deals disabled"}

	if settings.predeal_enabled:
		result["predeals"] = get_predeals(window_from=window_from)
	else:
		result["predeals"] = {"skipped": True, "reason": "Get Predeals disabled"}

	return result


def _get_settings():
	settings_name = "Biflorica Setting"
	if not frappe.db.exists("Biflorica Setting", settings_name):
		frappe.throw("Biflorica Setting not found. Please configure it first.")
	return frappe.get_doc("Biflorica Setting", settings_name)


def _auth_headers(settings):
	if not settings.access_token:
		frappe.throw("Access token is missing. Click 'Update Access Token' first.")
	return {
		"Authorization": f"Bearer {settings.access_token}",
		"Content-Type": "application/json",
		"accept": "application/json",
	}


def _api_call(method, path, settings, payload=None, params=None):
	url = settings.base_url.rstrip("/") + path
	headers = _auth_headers(settings)

	try:
		http_response = requests.request(
			method=method,
			url=url,
			headers=headers,
			data=json.dumps(payload) if payload is not None else None,
			params=params,
			timeout=30,
		)
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"{method} {url} request failed: {e}", "Biflorica API")
		return {"success": False, "message": str(e), "status_code": None, "data": None}

	body_preview = http_response.text[:500] if http_response.text else ""

	try:
		body = http_response.json() if http_response.text else None
	except ValueError:
		body = None

	if http_response.status_code not in (200, 201):
		frappe.log_error(
			f"{method} {url} -> {http_response.status_code}: {body_preview}",
			"Biflorica API",
		)
		return {
			"success": False,
			"message": f"API returned status {http_response.status_code}",
			"status_code": http_response.status_code,
			"data": body if body is not None else http_response.text,
		}

	return {
		"success": True,
		"message": "OK",
		"status_code": http_response.status_code,
		"data": body if body is not None else http_response.text,
	}


@frappe.whitelist()
def update_access_token():
	try:
		settings_name = "Biflorica Setting"
		settings = frappe.get_doc("Biflorica Setting", settings_name)

		base_url = settings.base_url or ""
		username = settings.username or ""
		password = settings.password or ""

		if not (base_url and username and password):
			frappe.log_error("Missing base_url, username, or password", "Biflorica Token Update")
			return {"success": False, "message": "Missing base_url, username, or password"}

		api_url = base_url.rstrip("/") + "/auth/token"
		headers = {
			"accept": "application/json",
			"Content-Type": "application/json"
		}
		payload = json.dumps({"username": username, "password": password})

		http_response = requests.post(api_url, headers=headers, data=payload, timeout=30)

		try:
			response = http_response.json()
		except ValueError:
			frappe.log_error(
				f"Non-JSON response ({http_response.status_code}): {http_response.text[:500]}",
				"Biflorica Token Update",
			)
			return {"success": False, "message": f"Non-JSON response from auth endpoint (status {http_response.status_code})"}

		if http_response.status_code not in (200, 201):
			frappe.log_error(
				f"Auth failed ({http_response.status_code}): {http_response.text[:500]}",
				"Biflorica Token Update",
			)
			return {"success": False, "message": f"Auth failed with status {http_response.status_code}"}

		# Biflorica wraps auth errors in an HTTP 200 with an in-body status code,
		# e.g. {"code": 401, "status": "error", ...}. Surface that as a clear
		# credential failure instead of the misleading "Token not found".
		body_code = (response or {}).get("code")
		body_status = str((response or {}).get("status") or "").lower()
		if (body_code is not None and int(body_code) not in (200, 201)) or body_status == "error":
			frappe.log_error(
				f"Auth rejected (body code {body_code}): {http_response.text[:500]}",
				"Biflorica Token Update",
			)
			if body_code == 401:
				message = "Invalid credentials (401): check username/password on Biflorica Setting"
			else:
				message = f"Authentication failed (Biflorica returned code {body_code})"
			return {"success": False, "message": message}

		token = ""
		if response:
			if response.get("model") and response["model"].get("token"):
				token = response["model"]["token"]
			elif response.get("token"):
				token = response["token"]

		if token != "":
			frappe.db.set_value("Biflorica Setting", settings_name, "access_token", token)
			frappe.db.commit()
			_logger.info(f"[Biflorica Token Update] Access token updated")
			return {"success": True, "message": "Access token updated successfully"}
		else:
			frappe.log_error("Token not found in API response", "Biflorica Token Update")
			return {"success": False, "message": "Token not found in API response"}

	except Exception as e:
		frappe.log_error(str(e), "Biflorica Token Update Error")
		return {"success": False, "message": str(e)}


@frappe.whitelist()
def refresh_stock():
	try:
		settings = _get_settings()
		if not settings.warehouse:
			return {"success": False, "message": "Warehouse not configured in Biflorica Setting"}

		items_data = get_warehouse_stock_items(settings.warehouse) or []

		settings.set("stock_items", [])
		for item in items_data:
			qty = item.get("actual_qty") or 0
			if qty <= 0:
				continue

			item_code = item.get("item_code")
			price = get_item_price(item_code)
			stem_length = get_stem_length_from_stock_entry(item_code, settings.warehouse)
			variety = get_biflorica_flower_variety(item, "Rose")
			uom = frappe.db.get_value("Item", item_code, "stock_uom")

			settings.append("stock_items", {
				"warehouse": settings.warehouse,
				"item_code": item_code,
				"item_name": item.get("item_name"),
				"variety": variety,
				"stem_length": stem_length,
				"qty": qty,
				"price_per_stem": price,
				"uom": uom,
			})

		settings.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"success": True,
			"message": f"Loaded {len(settings.stock_items)} items from {settings.warehouse}",
		}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Refresh Stock Error")
		return {"success": False, "message": str(e)}


@frappe.whitelist()
def post_offers(box_type=None, packrate=None, minimum=None):
	try:
		result = post_all_items_to_biflorica(box_type=box_type, packrate=packrate, minimum=minimum) or {}
		frappe.db.set_value("Biflorica Setting", "Biflorica Setting", "offer_last_run", frappe.utils.now_datetime())
		frappe.db.commit()

		api_response = result.get("api_response") or {}
		offers_payload = result.get("offers_payload") or {}
		posted_offers = offers_payload.get("data") or []

		raw_response = api_response.get("api_response")
		parsed_results = []
		if isinstance(raw_response, str):
			try:
				parsed_results = json.loads(raw_response)
			except ValueError:
				parsed_results = []
		elif isinstance(raw_response, list):
			parsed_results = raw_response

		api_succeeded = api_response.get("success", True)

		success_varieties = []
		failed_varieties = []

		if not parsed_results and api_succeeded and posted_offers:
			# Biflorica returns an EMPTY 200 body when every offer is accepted;
			# it only returns a per-offer JSON array when there are errors. So an
			# empty body on a successful call means all posted offers went through.
			success_varieties = [
				(o.get("variety") or "(unknown)") for o in posted_offers
			]
		else:
			for idx, item_result in enumerate(parsed_results or []):
				if not isinstance(item_result, dict):
					continue
				variety = ""
				if idx < len(posted_offers):
					variety = posted_offers[idx].get("variety") or "(unknown)"
				if item_result.get("result") == "ok":
					success_varieties.append(variety)
				else:
					errors = item_result.get("errors") or {}
					reason_parts = []
					for field, msgs in errors.items():
						if isinstance(msgs, list):
							reason_parts.append(f"{field}: {', '.join(str(m) for m in msgs)}")
						else:
							reason_parts.append(f"{field}: {msgs}")
					failed_varieties.append({
						"variety": variety,
						"reason": "; ".join(reason_parts) or "rejected",
					})

		# Persist offer id -> size at post time. This is the only moment we know
		# both for certain; deals later resolve their stem length from here even
		# after the offer has expired off Biflorica.
		_store_posted_offer_sizes(parsed_results, posted_offers)

		summary = result.get("summary") or {}
		summary["success_varieties"] = success_varieties
		summary["failed_varieties"] = failed_varieties
		summary["success_count"] = len(success_varieties)
		summary["failed_count"] = len(failed_varieties)

		overall_success = bool(api_succeeded) and not failed_varieties

		if success_varieties and failed_varieties:
			message = f"Posted {len(success_varieties)}, failed {len(failed_varieties)}"
		elif success_varieties:
			message = f"Posted {len(success_varieties)} offer(s)"
		elif failed_varieties:
			message = f"All {len(failed_varieties)} offer(s) failed"
		else:
			message = api_response.get("message") or "No offers processed"

		return {
			"success": overall_success,
			"message": message,
			"summary": summary,
			"data": result,
		}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Post Offers Error")
		return {"success": False, "message": str(e)}


def _to_float(value):
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def _store_posted_offer_sizes(parsed_results, posted_offers):
	"""Record offer id -> size for each successfully posted offer.

	Biflorica returns the offer ids in the same order as the posted payload
	(`[{"result":"ok","id":"80"}, ...]`), so id at index i pairs with the
	offer payload at index i. Stored on the Biflorica Setting `live_offers`
	table (offer_id + size + variety), upserting by offer id, so a deal can
	later resolve its exact stem length even after the offer expires.
	"""
	if not parsed_results or not posted_offers:
		return
	pairs = []
	for idx, item_result in enumerate(parsed_results):
		if not isinstance(item_result, dict) or item_result.get("result") != "ok":
			continue
		offer_id = str(item_result.get("id") or "").strip()
		if not offer_id or idx >= len(posted_offers):
			continue
		payload = posted_offers[idx]
		pairs.append((offer_id, str(payload.get("size") or ""), payload.get("variety") or ""))
	if not pairs:
		return

	doc = frappe.get_doc("Biflorica Setting", "Biflorica Setting")
	by_id = {str(r.offer_id): r for r in doc.live_offers}
	for offer_id, size, variety in pairs:
		row = by_id.get(offer_id)
		if row:
			row.size = size
			row.variety = variety
		else:
			doc.append("live_offers", {"offer_id": offer_id, "size": size, "variety": variety})
	doc.save(ignore_permissions=True)
	frappe.db.commit()


@frappe.whitelist()
def get_offers():
	try:
		settings = _get_settings()
		result = _api_call("GET", "/offers", settings)
		if not result["success"]:
			return result

		body = result.get("data") or {}
		offers = []
		if isinstance(body, dict):
			offers = body.get("data") or []
		elif isinstance(body, list):
			offers = body

		doc = _get_settings()
		doc.set("live_offers", [])
		for offer in offers:
			if not isinstance(offer, dict):
				continue
			doc.append("live_offers", {
				"offer_id": str(offer.get("id") or ""),
				"type": offer.get("type") or "",
				"variety": offer.get("variety") or "",
				"color": offer.get("color") or "",
				"size": str(offer.get("size") or ""),
				"quantity": _to_float(offer.get("quantity")),
				"packing": str(offer.get("packing") or ""),
				"price_per_stem": _to_float(offer.get("pricePerStem")),
				"price": _to_float(offer.get("price")),
				"box_type": offer.get("boxType") or "",
				"platform": offer.get("platform") or "",
				"farm": offer.get("farm") or "",
				"date_start": offer.get("dateStart") or None,
				"date_end": offer.get("dateEnd") or None,
			})
		# Note: don't stamp offer_last_run here — that field reflects the Post
		# Offers scheduled job's run time, not this manual live-offers fetch.
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		result["message"] = f"Loaded {len(doc.live_offers)} live offers"
		return result
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Get Offers Error")
		return {"success": False, "message": str(e)}


def _to_iso_z(value):
	if not value:
		return None
	dt = frappe.utils.get_datetime(value)
	return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _approve_deal_entry(deal):
	"""Build one /deals/approve `data` entry from a deal/predeal dict.

	The approve API expects {id, packing, deliveryDate} per item — not a plain
	deal_id. id must be an int; deliveryDate is ISO-8601 with a trailing Z.
	"""
	try:
		deal_id = int(deal.get("id"))
	except (TypeError, ValueError):
		return None
	entry = {"id": deal_id}
	if deal.get("packing") not in (None, ""):
		entry["packing"] = int(flt(deal.get("packing")))
	dd = _to_iso_z(deal.get("deliveryDate"))
	if dd:
		entry["deliveryDate"] = dd
	return entry


def _approve_deals(settings, deals):
	"""POST /deals/approve for a list of deal/predeal dicts in the API's format:
	{"data": [{id, packing, deliveryDate}, ...], "countAll": N}.
	"""
	entries = [e for e in (_approve_deal_entry(d) for d in deals) if e]
	if not entries:
		return {"success": False, "message": "no valid deals to approve"}
	payload = {"data": entries, "countAll": len(entries)}
	return _api_call("POST", "/deals/approve", settings, payload=payload)


# How far back a scheduled run looks, per Biflorica event frequency. The "Long"
# variants share the base interval; "All" means no time window (full fetch).
_FREQUENCY_LOOKBACK = {
	"Hourly": {"hours": 1},
	"Hourly Long": {"hours": 1},
	"Daily": {"days": 1},
	"Daily Long": {"days": 1},
	"Weekly": {"days": 7},
	"Weekly Long": {"days": 7},
	"Monthly": {"days": 30},
	"Monthly Long": {"days": 30},
	"Yearly": {"days": 365},
}


def _frequency_window_from(settings, prefix):
	"""fromDate for a scheduled run = now minus the schedule's frequency window.

	Hourly -> last 1h, Daily -> last 1d, Weekly -> 7d, etc. Returns None for
	"All"/Cron/unknown (no rolling window — fall back to the static filters).
	"""
	frequency = (getattr(settings, f"{prefix}_event_frequency", None) or "").strip()
	delta = _FREQUENCY_LOOKBACK.get(frequency)
	if not delta:
		return None
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), **{f"{k}": -v for k, v in delta.items()})


def _build_deal_params(settings, prefix, window_from=None):
	"""Build /deals query params.

	When `window_from` is given (scheduled runs), it overrides the static
	`{prefix}_from_date` filter so the fetch covers just the last interval.
	"""
	params = {}
	from_value = window_from or getattr(settings, f"{prefix}_from_date", None)
	from_date = _to_iso_z(from_value)
	to_date = _to_iso_z(getattr(settings, f"{prefix}_to_date", None))
	mutation_date = _to_iso_z(getattr(settings, f"{prefix}_mutation_date", None))
	limit = getattr(settings, f"{prefix}_limit", None)
	offset = getattr(settings, f"{prefix}_offset", None)

	if from_date:
		params["fromDate"] = from_date
	# A rolling window shouldn't be capped by a stale static toDate.
	if to_date and not window_from:
		params["toDate"] = to_date
	if mutation_date:
		params["mutationDate"] = mutation_date
	if limit:
		params["limit"] = int(limit)
	if offset:
		params["offset"] = int(offset)
	return params


def _deal_box_label(deal):
	"""Box label for a deal = the buyer code (e.g. b-B12)."""
	return (deal.get("buyer") or "").strip()


def _stem_length_rounded_map():
	"""Map {tens-rounded numeric -> Stem Length record name}.

	Built once per deal sync so _resolve_stem_length doesn't scan the whole
	Stem Length table per deal. First name wins for a given rounded bucket.
	"""
	rounded = {}
	for name in frappe.get_all("Stem Length", pluck="name"):
		digits = "".join(ch for ch in str(name) if ch.isdigit())
		if not digits:
			continue
		rounded.setdefault(int(round(int(digits) / 10.0) * 10), name)
	return rounded


def _resolve_stem_length(size, rounded_map=None):
	"""Map a Biflorica deal size (e.g. "50") to a Stem Length record name.

	Offers post the size rounded to the nearest ten ("50", "70"), but Stem
	Length records are like "52cm" / "72cm". Try an exact match first, then
	match the record whose numeric value rounds to the deal size.

	`rounded_map` is the pre-built {rounded -> name} map (see
	_stem_length_rounded_map); when None it is built here.
	"""
	if not size:
		return None
	size = str(size).strip()
	if frappe.db.exists("Stem Length", size):
		return size
	try:
		target = int(round(float(size.replace("cm", "").strip()) / 10.0) * 10)
	except (TypeError, ValueError):
		return None
	if rounded_map is None:
		rounded_map = _stem_length_rounded_map()
	return rounded_map.get(target)


def _fetch_live_offers(settings):
	"""Return the live offers list from Biflorica's /offers endpoint (or []).

	Fetched once per deal sync and shared by both _offer_size_map and
	_variety_size_map, so a single sync hits /offers once rather than twice.
	"""
	res = _api_call("GET", "/offers", settings)
	if not res.get("success"):
		return []
	body = res.get("data") or {}
	offers = body.get("data") if isinstance(body, dict) else body
	return [o for o in (offers or []) if isinstance(o, dict)]


def _offer_size_map(settings, live_offers=None):
	"""Map {offer id -> size} from the live offers, so a deal (which carries only
	an `offer` id, not its stem length) can resolve its size.

	A single offer's `size` may be a single value ("50") or a slash list
	("35/40/50/..."); only single-size offers yield a usable stem length.

	`live_offers` is the pre-fetched /offers list (see _fetch_live_offers); when
	None it is fetched here.
	"""
	size_by_offer = {}

	# 1) Sizes captured at post time (survive offer expiry) — the source of truth.
	try:
		doc = frappe.get_cached_doc("Biflorica Setting", "Biflorica Setting")
		for r in doc.live_offers:
			size = str(r.size or "").strip()
			if r.offer_id and size and "/" not in size:
				size_by_offer[str(r.offer_id)] = size
	except Exception:
		pass

	# 2) Live offers currently on Biflorica (fills any not yet stored).
	if live_offers is None:
		live_offers = _fetch_live_offers(settings)
	for o in live_offers:
		size = str(o.get("size") or "").strip()
		if size and "/" not in size:
			size_by_offer.setdefault(str(o.get("id")), size)

	return size_by_offer


def _variety_size_map(settings, live_offers=None):
	"""Map {variety -> size} from current/stored single-size offers.

	Used as a last resort when a deal's own offer has expired and its id can't
	be resolved: a variety with exactly one posted single size yields that size.
	Not a guess — it's the actual size Biflorica has posted for that variety.

	`live_offers` is the pre-fetched /offers list (see _fetch_live_offers); when
	None it is fetched here.
	"""
	from collections import defaultdict

	sizes = defaultdict(set)

	try:
		doc = frappe.get_cached_doc("Biflorica Setting", "Biflorica Setting")
		for r in doc.live_offers:
			size = str(r.size or "").strip()
			if r.variety and size and "/" not in size:
				sizes[r.variety].add(size)
	except Exception:
		pass

	if live_offers is None:
		live_offers = _fetch_live_offers(settings)
	for o in live_offers:
		size = str(o.get("size") or "").strip()
		if o.get("variety") and size and "/" not in size:
			sizes[o.get("variety")].add(size)

	return {v: next(iter(s)) for v, s in sizes.items() if len(s) == 1}


def _resolve_delivery_point(deal):
	"""Delivery Point for a deal = its cargo. Find by name, else create it."""
	cargo = (deal.get("cargo") or "").strip()
	if not cargo:
		return None
	existing = frappe.db.get_value("Delivery Point", cargo, "name") or frappe.db.get_value(
		"Delivery Point", {"delivery_point": cargo}, "name"
	)
	if existing:
		return existing
	dp = frappe.new_doc("Delivery Point")
	# Delivery Point's first Data field is typically `delivery_point`; set both
	# the autoname source and any title field defensively.
	if dp.meta.has_field("delivery_point"):
		dp.delivery_point = cargo
	dp.flags.ignore_permissions = True
	dp.insert(ignore_permissions=True)
	return dp.name


def _customer_address_country(customer):
	"""Country from the customer's primary/linked Address, if any."""
	addr = frappe.db.get_value("Customer", customer, "customer_primary_address")
	if not addr:
		addr = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
			"parent",
		)
	return frappe.db.get_value("Address", addr, "country") if addr else None


def _resolve_deal_item(deal):
	"""Resolve the deal variety to an ERPNext Item (item_name == variety)."""
	variety = deal.get("variety")
	if not variety:
		return None
	return frappe.db.get_value("Item", {"item_name": variety}, "name") or (
		variety if frappe.db.exists("Item", variety) else None
	)


def _create_sales_order_from_deal(settings, deal, submit=True, kind="deal", stem_length_map=None):
	"""Create a Sales Order for one Biflorica deal/predeal. Idempotent on id.

	`submit=True` submits the SO (deals); `submit=False` leaves it as a draft
	(predeals — submitting the draft later confirms the predeal on Biflorica via
	the on_submit hook). `kind` ("deal"/"predeal") namespaces the po_no key.

	Returns (sales_order_name, status, error_message) where status is one of
	"created" / "exists", and error_message is set (with name/status None) on
	failure.
	"""
	deal_id = str(deal.get("id") or "")
	if not deal_id:
		return None, None, "deal has no id"

	# Idempotency key stored in po_no. Namespace it so a bare deal id can't
	# collide with unrelated Sales Orders that legitimately use the same po_no,
	# and so deals and predeals never collide with each other.
	prefix = "BIFLORICA-PREDEAL" if kind == "predeal" else "BIFLORICA"
	deal_ref = f"{prefix}-{deal_id}"

	# Create a draft Quotation instead of a Sales Order when the webshop is set to
	# "Create Orders as Quotation"; staff review it and convert it to a Sales Order.
	target_dt = "Quotation" if create_orders_as_quotation() else "Sales Order"
	target_item_dt = "Quotation Item" if target_dt == "Quotation" else "Sales Order Item"

	# Only a still-valid (draft/submitted) doc blocks recreation — if the prior one
	# was cancelled (docstatus 2), create a fresh one for the deal.
	_existing_dt, existing = _biflorica_deal_exists(deal_ref)
	if existing:
		return existing, "exists", None

	# All Biflorica deals register under the single Customer set on Biflorica Setting.
	customer = getattr(settings, "deals_customer", None)
	if not customer:
		return None, None, "no Deals Customer configured on Biflorica Setting"

	if not getattr(settings, "deals_company", None):
		return None, None, "no Deals Company configured on Biflorica Setting"

	so_meta = frappe.get_meta(target_dt)
	if so_meta.has_field("custom_business_unit") and so_meta.get_field("custom_business_unit").reqd \
			and not getattr(settings, "deals_business_unit", None):
		return None, None, "no Deals Business Unit configured on Biflorica Setting"
	if so_meta.has_field("custom_farm") and so_meta.get_field("custom_farm").reqd \
			and not getattr(settings, "deals_farm", None):
		return None, None, "no Deals Farm configured on Biflorica Setting"

	item_code = _resolve_deal_item(deal)
	if not item_code:
		return None, None, f"no Item matching variety '{deal.get('variety')}'"

	# Deal quantity is in BOXES; packing is stems/box. SO line qty = total stems.
	boxes = flt(deal.get("quantity"))
	packing = flt(deal.get("packing"))
	total_stems = boxes * packing
	if total_stems <= 0:
		return None, None, f"non-positive quantity (boxes={boxes}, packing={packing})"

	# Deal `price` is the total deal value; derive a per-stem rate.
	total_price = flt(deal.get("price"))
	rate = (total_price / total_stems) if total_stems else 0

	box_label = _deal_box_label(deal)

	so = frappe.new_doc(target_dt)
	if target_dt == "Quotation":
		# Quotation has no `customer`; it uses quotation_to + party_name.
		so.quotation_to = "Customer"
		so.party_name = customer
	else:
		so.customer = customer
	company = getattr(settings, "deals_company", None)
	if company:
		so.company = company
	# Biflorica deals are priced in USD (the deal `price` is USD).
	so.currency = getattr(settings, "deals_currency", None) or "USD"
	so.transaction_date = frappe.utils.nowdate()
	# delivery_date is a Sales Order field; on Quotation it applies only if added.
	if so_meta.has_field("delivery_date"):
		so.delivery_date = deal.get("deliveryDate") or frappe.utils.nowdate()
	# Flag preorder-sourced Sales Orders.
	if kind == "predeal" and so_meta.has_field("custom_is_preorder"):
		so.custom_is_preorder = 1
	# Keep the deal's negotiated price: don't let the price list / pricing rules
	# override the per-stem rate we set below.
	so.ignore_pricing_rule = 1

	delivery_date = deal.get("deliveryDate") or frappe.utils.nowdate()
	delivery_point = _resolve_delivery_point(deal)
	consignee_country = _customer_address_country(customer)
	# Consignee is configured on Biflorica Setting (not derived from the customer).
	consignee = getattr(settings, "deals_consignee", None)
	customer_territory = frappe.db.get_value("Customer", customer, "territory")

	# Mandatory integration fields on this site's Sales Order.
	if so_meta.has_field("custom_sales_order_type"):
		so.custom_sales_order_type = "Roses"
	if so_meta.has_field("custom_business_unit"):
		so.custom_business_unit = getattr(settings, "deals_business_unit", None)
	if so_meta.has_field("custom_farm"):
		so.custom_farm = getattr(settings, "deals_farm", None)
	if so_meta.has_field("custom_order_name"):
		so.custom_order_name = box_label or deal_id
	if so_meta.has_field("custom_ordered_stems"):
		so.custom_ordered_stems = total_stems
	# Delivery point comes from the deal's cargo.
	if so_meta.has_field("custom_delivery_point") and delivery_point:
		so.custom_delivery_point = delivery_point
	if so_meta.has_field("custom_expected_delivery_date"):
		so.custom_expected_delivery_date = delivery_date
	if so_meta.has_field("custom_week"):
		so.custom_week = str(frappe.utils.get_datetime(delivery_date).isocalendar()[1])
	if so_meta.has_field("custom_mode_of_transport"):
		so.custom_mode_of_transport = "Air"
	# Country / consignee are taken from the Customer.
	if so_meta.has_field("custom_statescountry") and customer_territory:
		so.custom_statescountry = customer_territory
	if so_meta.has_field("custom_consignee_country") and consignee_country:
		so.custom_consignee_country = consignee_country
	if so_meta.has_field("custom_consignee") and consignee:
		so.custom_consignee = consignee
	# Store the namespaced deal ref in po_no — doubles as the idempotency key above.
	if so_meta.has_field("po_no"):
		so.po_no = deal_ref

	# Sell in BUNCHES: qty = total_stems / bunch size, UOM = the item's sales
	# (bunch) UOM. stock_qty stays = total_stems (bunches * conversion_factor),
	# and the kaitet amount override is rate(per stem) * stock_qty, so we keep
	# the per-stem rate to land the correct deal total.
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Stems"
	sales_uom = frappe.db.get_value("Item", item_code, "sales_uom") or stock_uom
	conversion_factor = 1
	if sales_uom and sales_uom != stock_uom:
		conversion_factor = flt(frappe.db.get_value(
			"UOM Conversion Detail", {"parent": item_code, "uom": sales_uom}, "conversion_factor"
		)) or 1
	line_qty = (total_stems / conversion_factor) if conversion_factor else total_stems
	line = {
		"item_code": item_code,
		"qty": line_qty,
		"uom": sales_uom,
		"conversion_factor": conversion_factor,
		# Rate is per STEM; pin both rate and price_list_rate so the price list
		# can't override the deal's negotiated price.
		"rate": rate,
		"price_list_rate": rate,
	}
	# delivery_date only exists on Sales Order Item; harmless to omit on Quotation.
	if frappe.get_meta(target_item_dt).has_field("delivery_date"):
		line["delivery_date"] = delivery_date
	if settings.warehouse:
		line["warehouse"] = settings.warehouse
	# Site Server Script requires a non-zero "Ordered Stems" on each line.
	soi_meta = frappe.get_meta(target_item_dt)
	if soi_meta.has_field("custom_ordered_quantity"):
		line["custom_ordered_quantity"] = total_stems
	if soi_meta.has_field("custom_ordered_stems"):
		line["custom_ordered_stems"] = total_stems
	# Box label (buyer code) on the existing Sales Order Item field.
	if soi_meta.has_field("custom_box_label") and box_label:
		line["custom_box_label"] = box_label

	# Source warehouse from Biflorica Setting config.
	source_wh = getattr(settings, "deals_source_warehouse", None)
	if soi_meta.has_field("custom_source_warehouse") and source_wh:
		line["custom_source_warehouse"] = source_wh
	# Stem length (Link to Stem Length) = the size of the deal's offer (captured
	# at post time / read live). Resolved to a Stem Length record; left blank if
	# the offer size is unavailable — no guessed fallback.
	deal_len = deal.get("stem_length")
	stem_length = _resolve_stem_length(deal_len, stem_length_map)
	if soi_meta.has_field("custom_length") and stem_length:
		line["custom_length"] = stem_length
	# Packrate (Link to Packrate) from the deal packing; skip if no such record.
	pack = str(int(flt(deal.get("packing")))) if deal.get("packing") else None
	if soi_meta.has_field("custom_packrate") and pack and frappe.db.exists("Packrate", pack):
		line["custom_packrate"] = pack
	# Number of boxes = deal quantity (boxes).
	if soi_meta.has_field("custom_number_of_boxes"):
		line["custom_number_of_boxes"] = int(boxes)

	so.append("items", line)

	so.flags.ignore_permissions = True
	so.insert(ignore_permissions=True)
	# In Quotation mode the doc is always left as a draft for staff to review and
	# convert to a Sales Order — the `submit` flag (deal vs predeal) only drives
	# Sales Order submission and its on_submit hooks.
	if submit and target_dt == "Sales Order":
		so.submit()
	# Always log the created order JSON for traceability against the deal.
	frappe.log_error(json.dumps(so.as_dict(), indent=2, default=str), f"Biflorica {target_dt} {so.name}")
	return so.name, "created", None


@frappe.whitelist()
def get_deals(window_from=None):
	try:
		settings = _get_settings()
		params = _build_deal_params(settings, "deals", window_from=window_from)
		result = _api_call("GET", "/deals", settings, params=params)
		if not result["success"]:
			return result

		body = result.get("data") or {}
		deals = body.get("data") if isinstance(body, dict) else body
		deals = deals or []

		# A deal only carries its `offer` id, not the stem length — resolve the
		# size from the offers list (by offer id, then by variety as last resort)
		# and attach it to each deal as `stem_length`. Fetch /offers once and feed
		# both maps from it.
		live_offers = _fetch_live_offers(settings)
		size_by_offer = _offer_size_map(settings, live_offers)
		size_by_variety = _variety_size_map(settings, live_offers)
		stem_length_map = _stem_length_rounded_map()

		created, approved, existing_deals, failed = [], [], [], []
		for deal in deals:
			if not isinstance(deal, dict):
				continue
			frappe.log_error(json.dumps(deal, indent=2, default=str), "Biflorica Deal")
			deal_id = str(deal.get("id") or "")
			label = _deal_box_label(deal) or deal_id

			if not deal.get("stem_length"):
				offer_size = size_by_offer.get(str(deal.get("offer"))) or size_by_variety.get(deal.get("variety"))
				if offer_size:
					deal["stem_length"] = offer_size

			try:
				so_name, status, err = _create_sales_order_from_deal(
					settings, deal, stem_length_map=stem_length_map
				)
			except Exception as e:
				frappe.db.rollback()
				frappe.log_error(f"Deal {deal_id}: {e}", "Biflorica Deal -> SO Error")
				err, so_name, status = str(e), None, None

			if err:
				failed.append({"deal_id": deal_id, "box_label": label, "reason": err})
				continue

			# Already had a Sales Order -> report it, don't re-create or re-approve.
			if status == "exists":
				existing_deals.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})
				continue

			frappe.db.commit()
			created.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})

			# SO is in ERPNext -> approve the deal on Biflorica.
			approve_res = _approve_deals(settings, [deal])
			if approve_res.get("success"):
				approved.append(deal_id)
			else:
				failed.append({
					"deal_id": deal_id,
					"box_label": label,
					"reason": f"SO {so_name} created but approve failed: {approve_res.get('message')}",
				})

		frappe.db.set_value("Biflorica Setting", "Biflorica Setting", "deals_last_run", frappe.utils.now_datetime())
		frappe.db.commit()

		summary = {
			"fetched": len(deals),
			"created": created,
			"approved": approved,
			"existing": existing_deals,
			"failed": failed,
			"created_count": len(created),
			"approved_count": len(approved),
			"existing_count": len(existing_deals),
			"failed_count": len(failed),
		}
		parts = []
		if created:
			parts.append(f"Created {len(created)} Sales Order(s), approved {len(approved)} deal(s)")
		if existing_deals:
			parts.append(f"{len(existing_deals)} already exist")
		if failed:
			parts.append(f"{len(failed)} issue(s)")
		message = "; ".join(parts) if parts else "No deals to process"

		return {"success": not failed, "message": message, "summary": summary, "data": result}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Get Deals Error")
		return {"success": False, "message": str(e)}


PREDEAL_PO_PREFIX = "BIFLORICA-PREDEAL-"


@frappe.whitelist()
def get_predeals(window_from=None):
	"""Fetch Biflorica preorders and create them as DRAFT Sales Orders.

	Unlike deals (auto-submitted + approved), predeals land as drafts for review.
	Submitting the draft Sales Order later confirms the preorder on Biflorica
	(see process_predeals).
	"""
	try:
		settings = _get_settings()
		params = _build_deal_params(settings, "predeal", window_from=window_from)
		result = _api_call("GET", "/deals/predeal", settings, params=params)
		if not result["success"]:
			return result

		body = result.get("data") or {}
		predeals = body.get("data") if isinstance(body, dict) else body
		predeals = predeals or []

		live_offers = _fetch_live_offers(settings)
		size_by_offer = _offer_size_map(settings, live_offers)
		size_by_variety = _variety_size_map(settings, live_offers)
		stem_length_map = _stem_length_rounded_map()

		created, existing_deals, failed = [], [], []
		for deal in predeals:
			if not isinstance(deal, dict):
				continue
			frappe.log_error(json.dumps(deal, indent=2, default=str), "Biflorica Predeal")
			deal_id = str(deal.get("id") or "")
			label = _deal_box_label(deal) or deal_id

			if not deal.get("stem_length"):
				offer_size = size_by_offer.get(str(deal.get("offer"))) or size_by_variety.get(deal.get("variety"))
				if offer_size:
					deal["stem_length"] = offer_size

			try:
				so_name, status, err = _create_sales_order_from_deal(
					settings, deal, submit=False, kind="predeal", stem_length_map=stem_length_map
				)
			except Exception as e:
				frappe.db.rollback()
				frappe.log_error(f"Predeal {deal_id}: {e}", "Biflorica Predeal -> SO Error")
				err, so_name, status = str(e), None, None

			if err:
				failed.append({"deal_id": deal_id, "box_label": label, "reason": err})
				continue
			if status == "exists":
				existing_deals.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})
				continue

			frappe.db.commit()
			created.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})

		frappe.db.set_value("Biflorica Setting", "Biflorica Setting", "predeal_last_run", frappe.utils.now_datetime())
		frappe.db.commit()

		summary = {
			"fetched": len(predeals),
			"created": created,
			"existing": existing_deals,
			"failed": failed,
			"created_count": len(created),
			"existing_count": len(existing_deals),
			"failed_count": len(failed),
		}
		parts = []
		if created:
			parts.append(f"Created {len(created)} draft Sales Order(s)")
		if existing_deals:
			parts.append(f"{len(existing_deals)} already exist")
		if failed:
			parts.append(f"{len(failed)} issue(s)")
		message = "; ".join(parts) if parts else "No predeals to process"

		return {"success": not failed, "message": message, "summary": summary, "data": result}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Get Predeals Error")
		return {"success": False, "message": str(e)}


def _draft_predeal_sales_orders():
	"""All draft Sales Orders sourced from Biflorica predeals."""
	return frappe.get_all(
		"Sales Order",
		filters={"po_no": ["like", f"{PREDEAL_PO_PREFIX}%"], "docstatus": 0},
		pluck="name",
	)


def confirm_biflorica_predeal_on_submit(doc, method=None):
	"""Approve the predeal on Biflorica when its draft Sales Order is submitted.

	Submit == approve. Identified by po_no = "BIFLORICA-PREDEAL-<id>". Pulls the
	predeal's {id, packing, deliveryDate} for the approve payload; throws (and so
	blocks the submit) if Biflorica rejects it.
	"""
	po_no = doc.get("po_no") or ""
	if not po_no.startswith(PREDEAL_PO_PREFIX):
		return
	predeal_id = po_no[len(PREDEAL_PO_PREFIX):]
	if not predeal_id:
		return

	settings = _get_settings()
	# Look up the predeal so the approve payload carries packing + deliveryDate.
	predeal = {"id": predeal_id}
	pd_res = _api_call("GET", "/deals/predeal", settings, params=_build_deal_params(settings, "predeal"))
	if pd_res.get("success"):
		pbody = pd_res.get("data") or {}
		for pd in (pbody.get("data") if isinstance(pbody, dict) else pbody) or []:
			if isinstance(pd, dict) and str(pd.get("id")) == str(predeal_id):
				predeal = pd
				break

	res = _approve_deals(settings, [predeal])
	if not res.get("success"):
		frappe.throw(f"Could not approve Biflorica preorder {predeal_id}: {res.get('message')}")
	frappe.msgprint(f"Biflorica preorder {predeal_id} approved.", alert=True)


@frappe.whitelist()
def process_predeals():
	"""Predeal workflow: fetch predeals as DRAFT Sales Orders, then submit each.

	Submitting a draft triggers confirm_biflorica_predeal_on_submit, which
	approves that predeal on Biflorica — so submit == approve.
	"""
	try:
		settings = _get_settings()

		stage1 = get_predeals()
		if not stage1.get("success"):
			return stage1

		submitted, submit_failed = [], []
		for so_name in _draft_predeal_sales_orders():
			try:
				so_doc = frappe.get_doc("Sales Order", so_name)
				so_doc.submit()  # on_submit hook approves on Biflorica
				frappe.db.commit()
				submitted.append(so_name)
			except Exception as e:
				frappe.db.rollback()
				frappe.log_error(f"Submit {so_name}: {e}", "Biflorica Predeal Submit Error")
				submit_failed.append({"sales_order": so_name, "reason": str(e)})

		summary = {
			"stage1": stage1.get("summary"),
			"submitted": submitted,
			"submit_failed": submit_failed,
			"submitted_count": len(submitted),
			"failed_count": len(submit_failed),
		}
		parts = []
		if submitted:
			parts.append(f"Submitted + approved {len(submitted)} predeal(s)")
		if submit_failed:
			parts.append(f"{len(submit_failed)} issue(s)")
		message = "; ".join(parts) if parts else (stage1.get("message") or "No predeals to process")

		return {
			"success": not submit_failed,
			"message": message,
			"summary": summary,
		}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Process Predeals Error")
		return {"success": False, "message": str(e)}


@frappe.whitelist()
def approve_deal(deal_id):
	try:
		if not deal_id:
			return {"success": False, "message": "Deal ID is required"}
		settings = _get_settings()
		# Look up the deal/predeal so we can send the {id, packing, deliveryDate}
		# the approve API requires; fall back to id-only if not found.
		match = {"id": deal_id}
		for path, prefix in (("/deals", "deals"), ("/deals/predeal", "predeal")):
			r = _api_call("GET", path, settings, params=_build_deal_params(settings, prefix))
			if not r.get("success"):
				continue
			b = r.get("data") or {}
			for d in (b.get("data") if isinstance(b, dict) else b) or []:
				if isinstance(d, dict) and str(d.get("id")) == str(deal_id):
					match = d
					break
		result = _approve_deals(settings, [match])
		if result["success"]:
			frappe.db.set_value("Biflorica Setting", "Biflorica Setting", "deals_last_run", frappe.utils.now_datetime())
			frappe.db.commit()
		return result
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Approve Deal Error")
		return {"success": False, "message": str(e)}
