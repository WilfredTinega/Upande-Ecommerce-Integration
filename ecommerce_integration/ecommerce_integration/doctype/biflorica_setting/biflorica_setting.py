# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import json
import re
from datetime import datetime
from urllib.parse import urljoin

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
	get_biflorica_flower_variety,
	get_item_price,
	get_stem_length_from_stock_entry,
	get_warehouse_stock_items,
	post_all_items_to_biflorica,
)
from ecommerce_integration.ecommerce_integration.utils import create_orders_as_quotation

_logger = frappe.logger("biflorica", allow_site=True)


def deal_po_ref(deal_id, kind="deal"):
	"""The po_no this app stamps for one Biflorica deal/predeal."""
	prefix = "BIFLORICA-PREDEAL" if kind == "predeal" else "BIFLORICA"
	return f"{prefix}-{deal_id}"


def deal_po_refs(deal_id):
	"""Every po_no a given Biflorica id could already be stored under.

	An approved predeal KEEPS ITS ID and simply moves from /deals/predeal into
	/deals. Looking only in the current kind's namespace imported it twice — once
	as BIFLORICA-PREDEAL-25 from the predeal, then again as BIFLORICA-25 when the
	approved predeal reappeared as a deal. Checking both means one Biflorica id
	can only ever produce one order.
	"""
	return [deal_po_ref(deal_id, "deal"), deal_po_ref(deal_id, "predeal")]


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
	(
		"at",
		"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_update_access_token",
		"Biflorica: Refresh Access Token",
	),
	(
		"offer",
		"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_post_offers",
		"Biflorica: Post Offers",
	),
	(
		"deals",
		"ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_setting.run_sync_deals_and_predeals",
		"Biflorica: Sync Deals & Predeals",
	),
]


class BifloricaSetting(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from ecommerce_integration.ecommerce_integration.doctype.biflorica_offer_view.biflorica_offer_view import (
			BifloricaOfferView,
		)
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_stock_view.biflorica_stock_view import (
			BifloricaStockView,
		)

		access_token: DF.LongText | None
		at_cron_format: DF.Data | None
		at_enabled: DF.Check
		at_event_frequency: DF.Literal[
			"All",
			"Hourly",
			"Daily",
			"Weekly",
			"Monthly",
			"Yearly",
			"Hourly Long",
			"Daily Long",
			"Weekly Long",
			"Monthly Long",
			"Cron",
		]
		at_last_run: DF.Datetime | None
		at_next_run: DF.Datetime | None
		base_url: DF.Data
		create_orders_as_quotation: DF.Check
		customer: DF.Link
		deals_cron_format: DF.Data | None
		deals_enabled: DF.Check
		deals_event_frequency: DF.Literal[
			"All",
			"Hourly",
			"Daily",
			"Weekly",
			"Monthly",
			"Yearly",
			"Hourly Long",
			"Daily Long",
			"Weekly Long",
			"Monthly Long",
			"Cron",
		]
		deals_last_run: DF.Datetime | None
		deals_next_run: DF.Datetime | None
		deals_period: DF.Int
		farm: DF.Data | None
		live_offers: DF.Table[BifloricaOfferView]
		offer_cron_format: DF.Data | None
		offer_enabled: DF.Check
		offer_event_frequency: DF.Literal[
			"All",
			"Hourly",
			"Daily",
			"Weekly",
			"Monthly",
			"Yearly",
			"Hourly Long",
			"Daily Long",
			"Weekly Long",
			"Monthly Long",
			"Cron",
		]
		offer_last_run: DF.Datetime | None
		offer_next_run: DF.Datetime | None
		password: DF.Password
		platform: DF.Data
		predeal_cron_format: DF.Data | None
		predeal_enabled: DF.Check
		predeal_event_frequency: DF.Literal[
			"All",
			"Hourly",
			"Daily",
			"Weekly",
			"Monthly",
			"Yearly",
			"Hourly Long",
			"Daily Long",
			"Weekly Long",
			"Monthly Long",
			"Cron",
		]
		predeal_last_run: DF.Datetime | None
		predeal_next_run: DF.Datetime | None
		predeal_period: DF.Int
		price_list: DF.Link | None
		publish_enabled_stock_only: DF.Check
		stock_items: DF.Table[BifloricaStockView]
		token_url: DF.Data | None
		use_shelf_stock: DF.Check
		username: DF.Data
		warehouse: DF.Link
	# end: auto-generated types

	def validate(self):
		# A warning, not a throw: the operator may be mid-edit, and the host map
		# only knows the platforms it has been confirmed against.
		from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_customer_offer import (
			platform_host_mismatch,
		)

		mismatch = platform_host_mismatch(self)
		if mismatch:
			frappe.msgprint(mismatch, title=_("Check Biflorica host"), indicator="orange")

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
	# Also called from after_install, where the doctype may not be synced yet.
	if not frappe.db.exists("DocType", "Biflorica Setting"):
		return {"jobs": []}
	doc = _get_settings()
	# Drop obsolete jobs (e.g. the separate deals/predeals jobs now merged).
	for method in _OBSOLETE_SCHEDULER_METHODS:
		name = frappe.db.get_value("Scheduled Job Type", {"method": method})
		if name:
			frappe.delete_doc("Scheduled Job Type", name, force=True, ignore_permissions=True)
	doc._sync_scheduled_jobs(force=True)
	# Also runs from after_install/after_migrate, which have no request
	# transaction to commit the Scheduled Job Type upserts for us.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
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
		frappe.throw(_("Biflorica Setting not found. Please configure it first."))
	return frappe.get_doc("Biflorica Setting", settings_name)


def _auth_headers(settings):
	if not settings.access_token:
		frappe.throw(_("Access token is missing. Click 'Update Access Token' first."))
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


def _read_api_password(settings) -> tuple[str, bool]:
	"""Return (password, decrypt_failed) for the Biflorica Setting password.

	A Password field lives encrypted in `__Auth`, keyed by the site's
	encryption_key. When that key no longer matches what encrypted the row —
	a site restored without its original site_config.json, or a key that was
	regenerated — frappe.throw()s inside the decrypt. raise_exception=False
	swallows the exception, but frappe.throw has already queued its message for
	the client, which surfaces as an unrelated "check site_config.json" dialog.
	Drop that message so the caller can report something actionable instead.
	"""
	log_length = len(frappe.message_log or [])
	password = settings.get_password("password", raise_exception=False)
	if password:
		return password, False

	leaked = len(frappe.message_log or []) - log_length
	# `_` is the translation helper in this module, so name the throwaway.
	for _leaked_message in range(max(leaked, 0)):
		frappe.clear_last_message()

	return "", leaked > 0


@frappe.whitelist()
def update_access_token():
	try:
		settings_name = "Biflorica Setting"
		settings = frappe.get_doc("Biflorica Setting", settings_name)

		base_url = settings.base_url or ""
		token_url = (settings.token_url or "").strip()
		username = settings.username or ""
		password, decrypt_failed = _read_api_password(settings)

		if decrypt_failed:
			frappe.log_error(
				"Stored password could not be decrypted; the site encryption key no longer "
				"matches the value in __Auth",
				"Biflorica Token Update",
			)
			return {
				"success": False,
				"message": "Stored password could not be decrypted. Re-enter the Password on "
				"Biflorica Setting and save.",
			}

		if not (username and password and (token_url or base_url)):
			frappe.log_error("Missing Token URL/Base URL, username, or password", "Biflorica Token Update")
			return {
				"success": False,
				"message": "Missing Token URL (or Base URL), username, or password",
			}

		# Token URL wins when set, so a relocated auth endpoint can be retargeted
		# from the form. A scheme-less value ("/apiv3/auth/token") is treated as a
		# path on Base URL rather than passed to requests as-is.
		if token_url:
			api_url = token_url if "://" in token_url else urljoin(base_url, token_url)
		else:
			api_url = base_url.rstrip("/") + "/auth/token"
		headers = {"accept": "application/json", "Content-Type": "application/json"}
		payload = json.dumps({"username": username, "password": password})

		http_response = requests.post(api_url, headers=headers, data=payload, timeout=30)

		try:
			response = http_response.json()
		except ValueError:
			frappe.log_error(
				f"Non-JSON response from {api_url} ({http_response.status_code}): {http_response.text[:500]}",
				"Biflorica Token Update",
			)
			return {
				"success": False,
				"message": f"Non-JSON response from auth endpoint (status {http_response.status_code})",
			}

		if http_response.status_code not in (200, 201):
			frappe.log_error(
				f"Auth failed at {api_url} ({http_response.status_code}): {http_response.text[:500]}",
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
				f"Auth rejected at {api_url} (body code {body_code}): {http_response.text[:500]}",
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
			frappe.db.set_single_value("Biflorica Setting", "access_token", token)
			frappe.db.commit()
			_logger.info("[Biflorica Token Update] Access token updated")
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
			# Shelf rows already know their stem length; only warehouse rows have
			# to be traced back through the stock ledger for one.
			stem_length = item.get("stem_length") or get_stem_length_from_stock_entry(
				item_code, settings.warehouse
			)
			price = get_item_price(item_code, stem_length=stem_length, item_group=item.get("item_group"))
			variety = get_biflorica_flower_variety(item, "Rose")
			uom = frappe.db.get_value("Item", item_code, "stock_uom")

			settings.append(
				"stock_items",
				{
					"warehouse": settings.warehouse,
					"item_code": item_code,
					"item_name": item.get("item_name"),
					"variety": variety,
					"stem_length": stem_length,
					"qty": qty,
					"price_per_stem": price,
					"uom": uom,
				},
			)

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
def post_offers(
	box_type: str | None = None,
	packrate: str | int | float | None = None,
	minimum: str | int | float | None = None,
):
	try:
		result = post_all_items_to_biflorica(box_type=box_type, packrate=packrate, minimum=minimum) or {}
		frappe.db.set_single_value("Biflorica Setting", "offer_last_run", frappe.utils.now_datetime())
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
			# Reached only when the API reported success AND returned a body we could
			# not parse into per-offer results. An empty body is NOT success — it is
			# rejected upstream in post_to_biflorica_api, which is why this no longer
			# counts the posted offers as accepted. Anything landing here is
			# unverifiable, so it is reported as failed rather than assumed good.
			success_varieties = []
			failed_varieties = [
				{
					"variety": o.get("variety") or "(unknown)",
					"reason": "no confirmation returned by Biflorica",
				}
				for o in posted_offers
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
					failed_varieties.append(
						{
							"variety": variety,
							"reason": "; ".join(reason_parts) or "rejected",
						}
					)

		# Persist offer id -> size at post time. This is the only moment we know
		# both for certain; deals later resolve their stem length from here even
		# after the offer has expired off Biflorica.
		_store_posted_offer_sizes(parsed_results, posted_offers)

		summary = result.get("summary") or {}
		summary["success_varieties"] = success_varieties
		summary["failed_varieties"] = failed_varieties
		summary["success_count"] = len(success_varieties)
		summary["failed_count"] = len(failed_varieties)

		# Rows the builder dropped before anything was sent (no price, no stock,
		# no stem length): without these the run reports "0 offers" and no reason.
		summary["skipped_reasons"] = _group_skipped_items(summary.get("skipped_items"))

		# Biflorica answers "Successfully posted 0 offers" to an empty payload;
		# a green tick there hides an enabled variety that never got offered.
		overall_success = bool(api_succeeded) and not failed_varieties and bool(success_varieties)

		# One misconfigured setting rejects every offer for the same reason. Say it
		# once, and say which setting — repeating an identical reason per variety
		# buries the one fact the operator can act on.
		shared_reason = _shared_failure_reason(failed_varieties, len(posted_offers))
		if shared_reason:
			summary["shared_reason"] = shared_reason
			summary["settings_hint"] = _settings_hint_for(shared_reason, _get_settings())

		if success_varieties and failed_varieties:
			message = f"Posted {len(success_varieties)}, failed {len(failed_varieties)}"
		elif success_varieties:
			message = f"Posted {len(success_varieties)} offer(s)"
		elif failed_varieties:
			message = f"All {len(failed_varieties)} offer(s) failed"
			if summary.get("settings_hint"):
				message = summary["settings_hint"]
			elif shared_reason:
				message = f"All {len(failed_varieties)} offer(s) failed — {shared_reason}"
		elif summary["skipped_reasons"]:
			first = summary["skipped_reasons"][0]
			message = f"Nothing offered — {first['reason'].lower()}: {', '.join(first['items'])}"
			if len(summary["skipped_reasons"]) > 1:
				message += f" (and {len(summary['skipped_reasons']) - 1} other reason(s))"
		else:
			# result["message"] is set when the builder never reached the API at all.
			message = api_response.get("message") or result.get("message") or "No offers processed"

		return {
			"success": overall_success,
			"message": message,
			"summary": summary,
			"data": result,
		}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Post Offers Error")
		return {"success": False, "message": str(e)}


# Biflorica reports a rejected *account-level* value (the farm, chiefly) against
# every offer in the request, with `result: "error"` rather than `not_validate`.
# The message it uses for an unresolvable farm is "Not parsed Farms".
_SETTINGS_FIELD_BY_ERROR_KEY = {
	"farm": ("farm", "Farm"),
	"platform": ("platform", "Platform"),
}


def _group_skipped_items(skipped_items):
	"""[{reason, items, count}] — the builder's skipped rows grouped by reason."""
	by_reason = {}
	for row in skipped_items or []:
		if not isinstance(row, dict):
			continue
		reason = row.get("reason") or "skipped"
		label = row.get("item_name") or row.get("item_code") or "(unknown)"
		debug = row.get("debug_info") or {}
		length = debug.get("stem_length") or row.get("stem_length")
		by_reason.setdefault(reason, []).append(f"{label} {length}" if length else label)
	return [{"reason": reason, "items": items, "count": len(items)} for reason, items in by_reason.items()]


def _shared_failure_reason(failed_varieties, posted_count):
	"""The one reason behind EVERY failure, or None when they differ.

	Only meaningful when nothing succeeded: a reason shared by some but not all
	offers is genuinely per-offer and belongs in the per-variety list.
	"""
	if not failed_varieties or len(failed_varieties) < posted_count:
		return None
	reasons = {f.get("reason") for f in failed_varieties}
	if len(reasons) != 1:
		return None
	return next(iter(reasons)) or None


def _settings_hint_for(reason, settings):
	"""Actionable text naming the setting to change, or None.

	Biflorica exposes no endpoint that lists valid farms (`/farms` answers "This
	operation is not implemented"), so the value has to come from the operator's
	Biflorica account — which is exactly what this says.
	"""
	if not reason:
		return None
	key = str(reason).split(":", 1)[0].strip().lower()
	field = _SETTINGS_FIELD_BY_ERROR_KEY.get(key)
	if not field:
		return None

	fieldname, label = field
	sent = settings.get(fieldname)
	return (
		f"Biflorica rejected {label} '{sent}', so none of the offers could be created. "
		f"Set Biflorica Setting > {label} to a value registered on your Biflorica "
		f"account — the API cannot list the valid ones."
	)


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
	# Called from the offers sync loop; persist the live_offers rows before the
	# next Biflorica request so a later HTTP failure can't roll them back.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit


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
		expired = 0
		for offer in offers:
			if not isinstance(offer, dict):
				continue
			# Deal enrichment still reads the raw /offers list, so a deal struck
			# against a since-ended offer keeps resolving its stem length.
			if _offer_has_expired(offer):
				expired += 1
				continue
			doc.append(
				"live_offers",
				{
					"offer_id": str(offer.get("id") or ""),
					"type": offer.get("type") or "",
					"variety": offer.get("variety") or "",
					"color": offer.get("color") or "",
					"size": str(offer.get("size") or ""),
					"sizes_stems": str(offer.get("sizesStems") or ""),
					"quantity": _to_float(offer.get("quantity")),
					"packing": str(offer.get("packing") or ""),
					"price_per_stem": _to_float(offer.get("pricePerStem")),
					"price_per_stem_list": str(offer.get("pricePerStem") or ""),
					"price": _to_float(offer.get("price")),
					"box_type": offer.get("boxType") or "",
					"platform": offer.get("platform") or "",
					"farm": offer.get("farm") or "",
					"date_start": offer.get("dateStart") or None,
					"date_end": offer.get("dateEnd") or None,
				},
			)
		# Note: don't stamp offer_last_run here — that field reflects the Post
		# Offers scheduled job's run time, not this manual live-offers fetch.
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		result["message"] = f"Loaded {len(doc.live_offers)} live offers"
		if expired:
			result["message"] += f" ({expired} expired offer(s) skipped)"
		return result
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Get Offers Error")
		return {"success": False, "message": str(e)}


def _offer_has_expired(offer):
	"""True when the offer's `dateEnd` is behind us; no end date never expires."""
	end = offer.get("dateEnd") or offer.get("date_end")
	if not end:
		return False
	try:
		ends = frappe.utils.get_datetime(end)
	except Exception:
		return False
	# A bare date means the whole of that day, not midnight.
	if (ends.hour, ends.minute, ends.second) == (0, 0, 0):
		return getdate(ends) < getdate()
	return ends < frappe.utils.now_datetime()


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


def _period_window_start(settings, prefix):
	"""`now` minus the tab's Period (hours), or None when unset.

	Mirrors Floriday Settings' own `period` field; zero keeps the old unbounded
	behaviour so an upgrade changes nothing until the field is filled in.
	"""
	try:
		hours = int(flt(getattr(settings, f"{prefix}_period", 0)))
	except (TypeError, ValueError):
		hours = 0
	if hours <= 0:
		return None
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours)


def _build_deal_params(settings, prefix, window_from=None):
	"""Build /deals query params.

	Three ways to bound the window, most explicit first:

	  1. `window_from` — a scheduled run asking for just the last interval.
	  2. the static `{prefix}_from_date` filter, when an operator set one.
	  3. `{prefix}_period`, in HOURS, rolling back from now.

	With none of them Biflorica is asked for its entire history, which is what
	the Period field exists to stop: every run re-fetched years of deals, and
	almost all of them were already ordered or long past their delivery date.
	"""
	params = {}
	from_value = (
		window_from
		or getattr(settings, f"{prefix}_from_date", None)
		or _period_window_start(settings, prefix)
	)
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
	# Read the LENGTH FIELD, never the record name. The master autonames
	# differently per farm and on some sites falls back to a hash — "qk3ai9rtod"
	# yields digits "39", which bucketed the 80CM record under 40cm and left
	# 50/60/70/80 unmapped entirely.
	has_length = frappe.get_meta("Stem Length").has_field("length")
	fields = ["name"] + (["length"] if has_length else [])
	for row in frappe.get_all("Stem Length", fields=fields):
		source = (row.get("length") if has_length else None) or row.get("name")
		digits = "".join(ch for ch in str(source) if ch.isdigit())
		if not digits:
			continue
		rounded.setdefault(int(round(int(digits) / 10.0) * 10), row.get("name"))
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


def _sum_sizes_stems(value):
	"""Total stems in one box from a `sizesStems` list: "39/39/39/39/39" -> 195."""
	total = 0
	for part in str(value or "").split("/"):
		part = part.strip()
		if part:
			try:
				total += int(float(part))
			except ValueError:
				return 0
	return total


def _offer_breakdown_map(settings, live_offers=None):
	"""{offer id -> [(size, rate_per_stem, stems_per_box), ...]} from the offer.

	An offer is one BOX spanning several stem lengths, each with its own rate:

	    size         "40/50/60/70/80"
	    pricePerStem "0.20/0.25/0.30/0.35/0.40"
	    sizesStems   "39/39/39/39/39"

	Kept as parallel triples so a deal can become one Sales Order line PER
	LENGTH. Collapsing it to a blended rate cannot reproduce the deal value:
	offer 74 is 60.72 for 198 stems, and 60.72/198 rounds to 0.31, which bills
	61.38 — out by 0.66. Per-length lines total exactly 60.72.

	Only the stored rows carry rates for an offer that has since expired, which is
	the usual case by the time a deal is fetched.
	"""
	breakdown = {}

	def parse(offer_id, size, rates, stems):
		sizes = [x.strip() for x in str(size or "").split("/") if x.strip()]
		rate_parts = [x.strip() for x in str(rates or "").split("/") if x.strip()]
		stem_parts = [x.strip() for x in str(stems or "").split("/") if x.strip()]
		if not sizes or len(sizes) != len(rate_parts) or len(sizes) != len(stem_parts):
			return
		try:
			rows = [(sizes[i], flt(rate_parts[i]), int(float(stem_parts[i]))) for i in range(len(sizes))]
		except ValueError:
			return
		if all(stems > 0 for _s, _r, stems in rows):
			breakdown.setdefault(str(offer_id), rows)

	try:
		doc = frappe.get_cached_doc("Biflorica Setting", "Biflorica Setting")
		for r in doc.live_offers:
			if r.offer_id:
				parse(
					r.offer_id,
					r.size,
					getattr(r, "price_per_stem_list", None),
					getattr(r, "sizes_stems", None),
				)
	except Exception:
		pass

	if live_offers is None:
		live_offers = _fetch_live_offers(settings)
	for o in live_offers:
		parse(o.get("id"), o.get("size"), o.get("pricePerStem"), o.get("sizesStems"))

	return breakdown


def _offer_stems_map(settings, live_offers=None):
	"""{offer id -> stems per box}, from the offer's own `sizesStems`.

	A deal states its quantity in BOXES; the stems only exist on the offer it was
	struck against. `deal.packing` is NOT that number — Biflorica normalises an
	offer on receipt (we post 40 stems of each of 5 sizes, it stores 39/39/39/39/39
	= 195 against a nominal `packing` of 200), and the deal then reports
	`packing: 40`. Taking boxes * deal.packing gave 80 stems where Biflorica's own
	Deals screen says 390.

	Same two sources as `_offer_size_map`: rows captured at post time survive the
	offer expiring off /offers, which routinely happens before a deal is fetched.
	"""
	stems_by_offer = {}

	try:
		doc = frappe.get_cached_doc("Biflorica Setting", "Biflorica Setting")
		for r in doc.live_offers:
			stems = _sum_sizes_stems(getattr(r, "sizes_stems", None))
			if r.offer_id and stems:
				stems_by_offer[str(r.offer_id)] = stems
	except Exception:
		pass

	if live_offers is None:
		live_offers = _fetch_live_offers(settings)
	for o in live_offers:
		stems = _sum_sizes_stems(o.get("sizesStems"))
		if o.get("id") and stems:
			stems_by_offer.setdefault(str(o.get("id")), stems)

	return stems_by_offer


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


def _find_or_create_named(doctype, value, name_fields):
	"""The `doctype` record called `value`, created if it is not there yet.

	`name_fields` are the candidate naming/title fields to match and populate,
	in order — these masters are autonamed off their own Data field, and the
	field is named differently on each (delivery_point, shipping_agent, ...).

	Not every site's copy of these masters has such a field, though. "Shipping
	Agent" and "Delivery Point" are shipped by BOTH upande_kaitet (autoname
	`field:shipping_agent`) and upande_packhouse (autoname `prompt`, single
	`description` field). On a packhouse-only site none of `name_fields` exists,
	so the loop set nothing and `prompt` autonaming had no name to use — every
	Floriday import died on "Please set the document name" while stamping the
	logistics fields. `description` is therefore tried as a last label field, and
	a `prompt`-autonamed doctype is named from `value` explicitly.
	"""
	if not value or not frappe.db.exists("DocType", doctype):
		return None

	existing = frappe.db.get_value(doctype, value, "name")
	if existing:
		return existing
	meta = frappe.get_meta(doctype)
	candidates = [*name_fields, "description"]
	for fieldname in candidates:
		if meta.has_field(fieldname):
			existing = frappe.db.get_value(doctype, {fieldname: value}, "name")
			if existing:
				return existing

	doc = frappe.new_doc(doctype)
	for fieldname in candidates:
		if meta.has_field(fieldname):
			doc.set(fieldname, value)
	if (meta.autoname or "").lower() == "prompt":
		doc.name = value
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _resolve_delivery_point(deal):
	"""Delivery Point for a deal = its cargo. Find by name, else create it."""
	return _find_or_create_named(
		"Delivery Point", (deal.get("cargo") or "").strip(), ("delivery_point", "title")
	)


def _resolve_shipping_agent(deal):
	"""Shipping Agent for a deal = its cargo, e.g. "EBF Cargo Kenya".

	Biflorica's `cargo` is a freight COMPANY, which is what Shipping Agent holds
	(the live Kaitet master is 97 rows of MORGAN CARGO KENYA LTD, EXPOLANKA
	FREIGHT LTD, AIRFLO LTD, ...). Delivery Point is the handover shed and gets
	the same value only because it is the one shipping detail a deal carries.
	"""
	return _find_or_create_named(
		"Shipping Agent", (deal.get("cargo") or "").strip(), ("shipping_agent", "title")
	)


def _customer_address(customer):
	"""The customer's primary Address, else any Address linked to them."""
	if not customer:
		return None
	addr = frappe.db.get_value("Customer", customer, "customer_primary_address")
	if addr:
		return addr
	return frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		"parent",
	)


def _customer_address_country(customer):
	"""Country from the customer's primary/linked Address, if any."""
	addr = _customer_address(customer)
	return frappe.db.get_value("Address", addr, "country") if addr else None


def _delivery_date_has_passed(deal):
	"""True when the deal's delivery date is behind us; today still passes.

	ERPNext refuses a Sales Order dated after its own delivery date
	(sales_order.validate_delivery_date), so such a deal cannot be ordered at all.
	"""
	delivery_date = deal.get("deliveryDate")
	if not delivery_date:
		return False
	try:
		return getdate(delivery_date) < getdate()
	except Exception:
		return False


def _resolve_deal_spec(customer, item_code, stem_length, delivery_date=None):
	"""(spec name, box item) for this customer/variety/length, or (None, None).

	Matched on the spec's declared relationships, never on its name: customer +
	an Approved Varieties row for the item + a Box Build row for the length.
	Mono Box wins over Mixed Box (a deal is one variety), then most recent.
	"""
	if not customer or not item_code or not frappe.db.exists("DocType", "Specifications"):
		return None, None

	approved = frappe.get_all(
		"Spec Approved Variety",
		filters={"variety": item_code, "parenttype": "Specifications"},
		pluck="parent",
	)
	if not approved:
		return None, None

	specs = frappe.get_all(
		"Specifications",
		filters={"name": ["in", approved], "customer": customer},
		fields=["name", "box_assortment", "status", "valid_from", "expiry_date", "modified"],
		order_by="modified desc",
	)
	on = getdate(delivery_date) if delivery_date else getdate()
	candidates = []
	for spec in specs:
		if (spec.status or "Active") != "Active":
			continue
		if spec.valid_from and getdate(spec.valid_from) > on:
			continue
		if spec.expiry_date and getdate(spec.expiry_date) < on:
			continue
		box_items = frappe.get_all(
			"Spec Box Item",
			filters={"parent": spec.name, "parenttype": "Specifications", "length": stem_length},
			fields=["bunch_type", "stems_per_bunch", "bunches_per_box", "length", "box_type", "pack_rate"],
			order_by="idx asc",
			limit=1,
		)
		if box_items:
			candidates.append((spec, box_items[0]))

	if not candidates:
		return None, None
	candidates.sort(key=lambda pair: pair[0].box_assortment == "Mixed Box")
	spec, box_item = candidates[0]
	return spec.name, box_item


def _spec_detail_payload(spec_name):
	"""Spec-level packing detail for a Sales Order line.

	Delegates to upande_packhouse's own spec autofill so the floor gets exactly
	what it expects; that app is optional, hence the guarded lookup.
	"""
	try:
		detail_payload = frappe.get_attr("upande_packhouse.spec_autofill._detail_payload")
	except Exception:
		detail_payload = None

	doc = frappe.get_cached_doc("Specifications", spec_name)
	if detail_payload:
		try:
			return detail_payload(doc)
		except Exception:
			# Fall through to the spec's own fields: everything but the consumables.
			pass
	return {
		"custom_cut_stage": doc.cut_stage or "",
		"custom_defoliation_length": doc.defoliation_length or "",
		"custom_consumables_charge": 1 if doc.consumables_charge else 0,
		"custom_documentation_fee": 1 if doc.documentation_charge else 0,
		"custom_certificate_of_origin": 1 if doc.certificate_of_origin else 0,
	}


def _set_line_field(line, soi_meta, fieldname, value):
	"""Set `fieldname`, dropping a Link whose target record does not exist.

	The spec and the line do not always point at the same master (box_type ->
	Box Type vs Item), and an unresolvable Link kills the whole insert.
	"""
	if not soi_meta.has_field(fieldname) or value in (None, ""):
		return
	field = soi_meta.get_field(fieldname)
	if field.fieldtype == "Link" and not frappe.db.exists(field.options, value):
		return
	line[fieldname] = value


def _bunch_size(uom):
	"""Stems in one bunch, off the UOM name: "Bunch (10)" -> 10.

	Same reading as upande_packhouse's sales_order_engine._uom_factor; the two
	must agree or the stem count changes when the packhouse touches the order.
	"""
	match = re.search(r"\((\d+)\)", uom or "")
	return int(match.group(1)) if match else 0


def _uom_conversion_factor(item_code, uom):
	"""Stems per `uom`: the UOM name wins, the Item's own conversion is fallback.

	The name wins because the packhouse reads only that; an Item Conversion that
	disagrees would have it silently recount the order on first save.
	"""
	if not uom or frappe.db.get_value("Item", item_code, "stock_uom") == uom:
		return 1
	named = _bunch_size(uom)
	if named:
		return named
	explicit = flt(
		frappe.db.get_value("UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor")
	)
	return explicit or 1


def _deal_selling_uom(item_code, stems_per_bunch=None):
	"""(uom, stems per uom): the spec's bunch size if a UOM exists, else sales UOM."""
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Stems"
	if stems_per_bunch:
		spec_uom = f"Bunch ({int(stems_per_bunch)})"
		if frappe.db.exists("UOM", spec_uom):
			return spec_uom, _uom_conversion_factor(item_code, spec_uom)
	sales_uom = frappe.db.get_value("Item", item_code, "sales_uom") or stock_uom
	return sales_uom, _uom_conversion_factor(item_code, sales_uom)


def _apply_spec_to_line(line, soi_meta, spec_name, box_item):
	"""Stamp the spec's packing instructions onto a line.

	Quantities are untouched: the UOM was already settled from this spec's
	bunch size before the line was built (_deal_selling_uom).
	"""
	mixed_bunch = 1 if box_item.get("bunch_type") == "Mixed Bunch" else 0
	assortment = frappe.db.get_value("Specifications", spec_name, "box_assortment")
	mixed_box = 1 if (assortment == "Mixed Box" and not mixed_bunch) else 0

	_set_line_field(line, soi_meta, "custom_line", spec_name)
	_set_line_field(line, soi_meta, "custom_box_type", box_item.get("box_type"))
	if soi_meta.has_field("custom_mixed_box"):
		line["custom_mixed_box"] = mixed_box
	if soi_meta.has_field("custom_mixed_bunch"):
		line["custom_mixed_bunch"] = mixed_bunch

	# Packrate is a Link on straight boxes and a plain Int on mixed ones.
	pack_rate = int(flt(box_item.get("pack_rate")))
	if pack_rate:
		if (mixed_box or mixed_bunch) and soi_meta.has_field("custom_packrate_mixed_box"):
			line["custom_packrate_mixed_box"] = pack_rate
		elif soi_meta.has_field("custom_packrate") and frappe.db.exists("Packrate", str(pack_rate)):
			line["custom_packrate"] = str(pack_rate)

	for fieldname, value in _spec_detail_payload(spec_name).items():
		_set_line_field(line, soi_meta, fieldname, value)


def _resolve_deals_consignee(settings, customer):
	"""Configured `deals_consignee`, else the customer's own Consignee, else None.

	A deal names no consignee. The master's link to the customer is the only
	other source, and it is used only when UNAMBIGUOUS — one buyer can have
	several consignees, and a guess on a draft reads as a reviewed value.
	Covers both variants: a `customer` Link and a `customers` Table MultiSelect.
	"""
	configured = getattr(settings, "deals_consignee", None)
	if configured:
		return configured
	if not customer or not frappe.db.exists("DocType", "Consignee"):
		return None

	if frappe.db.exists("Consignee", customer):
		return customer

	meta = frappe.get_meta("Consignee")
	matches = []
	if meta.has_field("customer"):
		matches = [row.name for row in frappe.get_all("Consignee", filters={"customer": customer}, limit=2)]
	if not matches and meta.has_field("customers"):
		matches = [
			row.parent
			for row in frappe.get_all(
				"Consignee Customer",
				filters={"customer": customer, "parenttype": "Consignee"},
				fields=["parent"],
				limit=2,
			)
		]
	return matches[0] if len(matches) == 1 else None


def _resolve_deal_item(deal):
	"""Resolve the deal variety to an ERPNext Item (item_name == variety)."""
	variety = deal.get("variety")
	if not variety:
		return None
	return frappe.db.get_value("Item", {"item_name": variety}, "name") or (
		variety if frappe.db.exists("Item", variety) else None
	)


# upande_packhouse's roses pipeline keys off Business Unit == "Roses"; a deal
# order without it is skipped by the packhouse entirely.
DEALS_BUSINESS_UNIT_DEFAULT = "Roses"


def _deals_business_unit(settings):
	"""Business Unit for deal Sales Orders: the configured one, else Roses."""
	configured = getattr(settings, "deals_business_unit", None)
	if configured:
		return configured
	if frappe.db.exists("Business Unit", DEALS_BUSINESS_UNIT_DEFAULT):
		return DEALS_BUSINESS_UNIT_DEFAULT
	return None


def _deals_company(settings):
	"""Company for deal Sales Orders: the configured one, else the warehouse's.

	A warehouse always belongs to exactly one company, so deriving it from the
	deals source warehouse (or the offers warehouse) is unambiguous and saves
	asking for a value the site already knows.
	"""
	company = getattr(settings, "deals_company", None)
	if company:
		return company
	for fieldname in ("deals_source_warehouse", "warehouse"):
		warehouse = getattr(settings, fieldname, None)
		if warehouse:
			derived = frappe.db.get_value("Warehouse", warehouse, "company")
			if derived:
				return derived
	return None


def _create_sales_order_from_deal(
	settings, deal, kind="deal", stem_length_map=None, stems_map=None, breakdown_map=None
):
	"""Create a DRAFT Sales Order for one Biflorica deal/predeal. Idempotent on id.

	Both kinds land as drafts. Biflorica carries no consignee — the buyer picks
	the destination per order, and on the live data the same buyer alternates
	between consignees with no derivable pattern — so somebody has to set
	`custom_consignee` before the order is committed. Submitting is that review
	step, and for a predeal it doubles as the Biflorica confirmation (see
	confirm_biflorica_predeal_on_submit).

	`kind` ("deal"/"predeal") namespaces the po_no key.

	Returns (sales_order_name, status, error_message) where status is one of
	"created" / "created_incomplete" (created, but a mandatory consignee could
	not be resolved, so submit will refuse it until one is set) / "exists", and
	error_message is set (with name/status None) on failure.
	"""
	deal_id = str(deal.get("id") or "")
	if not deal_id:
		return None, None, "deal has no id"

	# Idempotency key stored in po_no. Namespace it so a bare deal id can't
	# collide with unrelated Sales Orders that legitimately use the same po_no,
	# and so deals and predeals never collide with each other.
	deal_ref = deal_po_ref(deal_id, kind)
	existing_refs = deal_po_refs(deal_id)

	# Create a draft Quotation instead of a Sales Order when the webshop is set to
	# "Create Orders as Quotation"; staff review it and convert it to a Sales Order.
	target_dt = "Quotation" if create_orders_as_quotation("Biflorica Setting") else "Sales Order"
	target_item_dt = "Quotation Item" if target_dt == "Quotation" else "Sales Order Item"

	# Only a still-valid (draft/submitted) doc blocks recreation — if the prior one
	# was cancelled (docstatus 2), create a fresh one for the deal.
	for ref in dict.fromkeys(existing_refs):
		_existing_dt, existing = _biflorica_deal_exists(ref)
		if existing:
			return existing, "exists", None

	# All Biflorica deals register under the single Customer set on Biflorica
	# Setting. `deals_customer` overrides, but the general `customer` field is the
	# obvious place to set it and used to be read by nothing at all — so fall back
	# to it rather than failing next to a field the operator has already filled in.
	customer = getattr(settings, "deals_customer", None) or getattr(settings, "customer", None)
	if not customer:
		return None, None, "no Customer configured on Biflorica Setting (Customer or Deals Customer)"

	company = _deals_company(settings)
	if not company:
		return (
			None,
			None,
			(
				"no Deals Company configured on Biflorica Setting, and none could be "
				"derived from the configured warehouse"
			),
		)

	so_meta = frappe.get_meta(target_dt)
	business_unit = _deals_business_unit(settings)
	if (
		so_meta.has_field("custom_business_unit")
		and so_meta.get_field("custom_business_unit").reqd
		and not business_unit
	):
		return None, None, "no Deals Business Unit configured on Biflorica Setting"
	if (
		so_meta.has_field("custom_farm")
		and so_meta.get_field("custom_farm").reqd
		and not getattr(settings, "deals_farm", None)
	):
		return None, None, "no Deals Farm configured on Biflorica Setting"
	# An unresolvable consignee does not drop the deal: the draft is inserted
	# without the mandatory check and submit re-imposes it.
	consignee = _resolve_deals_consignee(settings, customer)
	consignee_required = so_meta.has_field("custom_consignee") and so_meta.get_field("custom_consignee").reqd
	needs_consignee = consignee_required and not consignee

	item_code = _resolve_deal_item(deal)
	if not item_code:
		return None, None, f"no Item matching variety '{deal.get('variety')}'"

	# Deal quantity is in BOXES. Stems per box come from the OFFER's sizesStems,
	# not from `deal.packing` — see _offer_stems_map for why those differ.
	boxes = flt(deal.get("quantity"))
	offer_id = str(deal.get("offer") or "")
	# Per-length breakdown of the box, when the offer is on record. Each length
	# becomes its own Sales Order line at its own rate, which is the only way the
	# lines add back up to the deal value exactly.
	breakdown = (breakdown_map or {}).get(offer_id) or []

	stems_per_box = flt((stems_map or {}).get(offer_id))
	if stems_per_box <= 0:
		# No offer on record (expired before this sync, or posted elsewhere).
		stems_per_box = flt(deal.get("packing"))
	total_stems = boxes * stems_per_box
	if total_stems <= 0:
		return None, None, f"non-positive quantity (boxes={boxes}, stems/box={stems_per_box})"

	# Deal `price` is the total deal value. Without a breakdown all we can do is
	# blend it across the stems, which rounds; with one it is never used.
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
	# The company default (Standard Selling, KES) leaves a USD order disagreeing
	# with itself about its own currency. Rates stay pinned per line either way.
	if so_meta.has_field("selling_price_list") and getattr(settings, "price_list", None):
		so.selling_price_list = settings.price_list

	delivery_date = deal.get("deliveryDate") or frappe.utils.nowdate()
	delivery_point = _resolve_delivery_point(deal)
	consignee_country = _customer_address_country(customer)
	customer_territory = frappe.db.get_value("Customer", customer, "territory")

	# Mandatory integration fields on this site's Sales Order.
	if so_meta.has_field("custom_sales_order_type"):
		so.custom_sales_order_type = "Roses"
	# `business_unit` is the accounting dimension the packhouse reads;
	# `custom_business_unit` is its legacy mirror.
	for fieldname in ("business_unit", "custom_business_unit"):
		if so_meta.has_field(fieldname) and business_unit:
			so.set(fieldname, business_unit)
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
	# `cargo` is the freight company -> Shipping Agent.
	shipping_agent = _resolve_shipping_agent(deal)
	if so_meta.has_field("custom_shipping_agent") and shipping_agent:
		so.custom_shipping_agent = shipping_agent
	# upande_packhouse ships these three as mandatory with sync_on_migrate=1, so
	# clearing the flags on the site does not survive a migrate — the values have
	# to come from the deal. `cargo` is the freight agent (it drives Drop Off
	# Point, Truck Details and Shipping Agent alike) and the Biflorica deal id is
	# the only stable reference we have for the S number.
	cargo = (deal.get("cargo") or "").strip()
	if so_meta.has_field("custom_drop_off_point") and cargo:
		so.custom_drop_off_point = cargo
	if so_meta.has_field("custom_truck_details") and cargo:
		so.custom_truck_details = cargo
	if so_meta.has_field("custom_s_number") and deal_id:
		so.custom_s_number = deal_ref
	# Ship to the customer's own address. A Biflorica deal names no address, and
	# on sites where this is mandatory an unset value blocks the insert outright.
	if so_meta.has_field("shipping_address_name") and not so.get("shipping_address_name"):
		address = _customer_address(customer)
		if address:
			so.shipping_address_name = address
	# Store the namespaced deal ref in po_no — doubles as the idempotency key above.
	if so_meta.has_field("po_no"):
		so.po_no = deal_ref

	# One entry per Sales Order line: (stems, per-stem rate, stem length label).
	# With a breakdown that is one line per length; without one it is a single
	# blended line, which is all the deal payload alone supports.
	if breakdown:
		line_specs = [(boxes * stems, line_rate, size) for size, line_rate, stems in breakdown if stems > 0]
	else:
		line_specs = [(total_stems, rate, None)]

	for line_stems, line_rate, line_size in line_specs:
		_append_deal_line(
			so,
			settings=settings,
			deal=deal,
			target_item_dt=target_item_dt,
			item_code=item_code,
			stems=line_stems,
			rate=line_rate,
			size=line_size,
			delivery_date=delivery_date,
			box_label=box_label,
			stem_length_map=stem_length_map,
		)

	so.flags.ignore_permissions = True
	# The mandatory check runs again on submit, so nothing reaches Biflorica or
	# the packhouse without a consignee.
	so.insert(ignore_permissions=True, ignore_mandatory=needs_consignee)
	return so.name, ("created_incomplete" if needs_consignee else "created"), None


def _append_deal_line(
	so,
	*,
	settings,
	deal,
	target_item_dt,
	item_code,
	stems,
	rate,
	size,
	delivery_date,
	box_label,
	stem_length_map,
):
	"""Append one Sales Order line for `stems` of `item_code` at `rate` per stem."""
	total_stems = stems
	soi_meta = frappe.get_meta(target_item_dt)

	# Stem length first: the spec is matched on it, and its bunch size then
	# decides what one selling unit is.
	deal_len = size or deal.get("stem_length")
	stem_length = _resolve_stem_length(deal_len, stem_length_map)
	spec_name, box_item = _resolve_deal_spec(
		so.get("customer") or so.get("party_name"), item_code, stem_length, delivery_date
	)
	line_uom, conversion_factor = _deal_selling_uom(item_code, (box_item or {}).get("stems_per_bunch"))

	# qty counts BUNCHES, stock_qty counts stems, so the rate must be per bunch:
	# a per-stem rate against a bunch qty divides the order total by the bunch.
	line = {
		"item_code": item_code,
		"qty": flt(total_stems) / conversion_factor,
		"uom": line_uom,
		"conversion_factor": conversion_factor,
		# Pinned so the price list cannot override the negotiated price.
		"rate": flt(rate) * conversion_factor,
		"price_list_rate": flt(rate) * conversion_factor,
	}
	# delivery_date only exists on Sales Order Item; harmless to omit on Quotation.
	if soi_meta.has_field("delivery_date"):
		line["delivery_date"] = delivery_date
	if settings.warehouse:
		line["warehouse"] = settings.warehouse
	# Site Server Script requires a non-zero "Ordered Stems" on each line.
	if soi_meta.has_field("custom_ordered_quantity"):
		line["custom_ordered_quantity"] = total_stems
	if soi_meta.has_field("custom_ordered_stems"):
		line["custom_ordered_stems"] = total_stems
	# Box label (buyer code) on the existing Sales Order Item field.
	if soi_meta.has_field("custom_box_label") and box_label:
		line["custom_box_label"] = box_label

	# Transit truck: mandatory on a Roses line, and a deal names none. Its cargo
	# is the freight handover, as on Truck Details / Drop Off / Shipping Agent.
	_set_line_field(line, soi_meta, "custom_truck", (deal.get("cargo") or "").strip())

	# Accounting dimensions repeat on the line; the GL picks them up from here.
	if soi_meta.has_field("business_unit"):
		_set_line_field(line, soi_meta, "business_unit", _deals_business_unit(settings))

	# Source warehouse from Biflorica Setting config.
	source_wh = getattr(settings, "deals_source_warehouse", None)
	if soi_meta.has_field("custom_source_warehouse") and source_wh:
		line["custom_source_warehouse"] = source_wh
	# Stem length (Link to Stem Length) = the size of the deal's offer (captured
	# at post time / read live). Left blank when the offer size is unavailable —
	# no guessed fallback.
	if soi_meta.has_field("custom_length") and stem_length:
		line["custom_length"] = stem_length
	# Packrate (Link to Packrate) from the deal packing; skip if no such record.
	pack = str(int(flt(deal.get("packing")))) if deal.get("packing") else None
	if soi_meta.has_field("custom_packrate") and pack and frappe.db.exists("Packrate", pack):
		line["custom_packrate"] = pack
	# Number of boxes = deal quantity. Stamped per line, since every line of a
	# multi-length deal belongs to the same boxes.
	if soi_meta.has_field("custom_number_of_boxes"):
		line["custom_number_of_boxes"] = int(flt(deal.get("quantity")))

	# Applied last so the spec wins over the deal-derived packrate: `packing` is
	# how the trade was priced, the spec is how the floor is told to pack.
	if spec_name:
		_apply_spec_to_line(line, soi_meta, spec_name, box_item)

	so.append("items", line)


@frappe.whitelist()
def get_deals(window_from: str | datetime | None = None):
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
		stems_by_offer = _offer_stems_map(settings, live_offers)
		breakdown_by_offer = _offer_breakdown_map(settings, live_offers)
		stem_length_map = _stem_length_rounded_map()

		created, approved, existing_deals, failed, incomplete = [], [], [], [], []
		skipped_past = 0
		for deal in deals:
			if not isinstance(deal, dict):
				continue
			deal_id = str(deal.get("id") or "")
			label = _deal_box_label(deal) or deal_id

			# Nothing to order against a delivery date that has already passed.
			if _delivery_date_has_passed(deal):
				skipped_past += 1
				continue

			if not deal.get("stem_length"):
				offer_size = size_by_offer.get(str(deal.get("offer"))) or size_by_variety.get(
					deal.get("variety")
				)
				if offer_size:
					deal["stem_length"] = offer_size

			try:
				so_name, status, err = _create_sales_order_from_deal(
					settings,
					deal,
					stem_length_map=stem_length_map,
					stems_map=stems_by_offer,
					breakdown_map=breakdown_by_offer,
				)
			except Exception as e:
				frappe.db.rollback()
				frappe.log_error(f"Deal {deal_id}: {e}", "Biflorica Deal -> SO Error")
				# A msgprint validation stays queued for the client and would pop one
				# modal per deal on top of the summary; `failed` carries the reason.
				frappe.clear_messages()
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
			if status == "created_incomplete":
				incomplete.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})

			# SO is in ERPNext -> approve the deal on Biflorica.
			approve_res = _approve_deals(settings, [deal])
			if approve_res.get("success"):
				approved.append(deal_id)
			else:
				failed.append(
					{
						"deal_id": deal_id,
						"box_label": label,
						"reason": f"SO {so_name} created but approve failed: {approve_res.get('message')}",
					}
				)

		frappe.db.set_single_value("Biflorica Setting", "deals_last_run", frappe.utils.now_datetime())
		frappe.db.commit()

		summary = {
			"fetched": len(deals),
			"created": created,
			"approved": approved,
			"existing": existing_deals,
			"failed": failed,
			"incomplete": incomplete,
			"skipped_past_count": skipped_past,
			"created_count": len(created),
			"approved_count": len(approved),
			"existing_count": len(existing_deals),
			"failed_count": len(failed),
			"incomplete_count": len(incomplete),
		}
		# One missing setting fails every deal identically; say it once.
		shared_reason = _shared_failure_reason(failed, len(deals) - skipped_past)
		if shared_reason:
			summary["shared_reason"] = shared_reason
		parts = []
		if created:
			parts.append(f"Created {len(created)} Sales Order(s), approved {len(approved)} deal(s)")
		if incomplete:
			parts.append(f"{len(incomplete)} need a Consignee before submit")
		if existing_deals:
			parts.append(f"{len(existing_deals)} already exist")
		if failed:
			parts.append(
				f"All {len(failed)} deal(s) failed — {shared_reason}"
				if shared_reason
				else f"{len(failed)} issue(s)"
			)
		message = "; ".join(parts) if parts else "No new orders"

		return {"success": not failed, "message": message, "summary": summary, "data": result}
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Get Deals Error")
		return {"success": False, "message": str(e)}


PREDEAL_PO_PREFIX = "BIFLORICA-PREDEAL-"


@frappe.whitelist()
def get_predeals(window_from: str | datetime | None = None):
	"""Fetch Biflorica preorders and create them as DRAFT Sales Orders.

	Both deals and predeals land as drafts — the consignee has to be set by hand
	before the order is committed.
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
		stems_by_offer = _offer_stems_map(settings, live_offers)
		breakdown_by_offer = _offer_breakdown_map(settings, live_offers)
		stem_length_map = _stem_length_rounded_map()

		created, existing_deals, failed, incomplete = [], [], [], []
		skipped_past = 0
		for deal in predeals:
			if not isinstance(deal, dict):
				continue
			deal_id = str(deal.get("id") or "")
			label = _deal_box_label(deal) or deal_id

			# Nothing to order against a delivery date that has already passed.
			if _delivery_date_has_passed(deal):
				skipped_past += 1
				continue

			if not deal.get("stem_length"):
				offer_size = size_by_offer.get(str(deal.get("offer"))) or size_by_variety.get(
					deal.get("variety")
				)
				if offer_size:
					deal["stem_length"] = offer_size

			try:
				so_name, status, err = _create_sales_order_from_deal(
					settings,
					deal,
					kind="predeal",
					stem_length_map=stem_length_map,
					stems_map=stems_by_offer,
					breakdown_map=breakdown_by_offer,
				)
			except Exception as e:
				frappe.db.rollback()
				frappe.log_error(f"Predeal {deal_id}: {e}", "Biflorica Predeal -> SO Error")
				frappe.clear_messages()
				err, so_name, status = str(e), None, None

			if err:
				failed.append({"deal_id": deal_id, "box_label": label, "reason": err})
				continue
			if status == "exists":
				existing_deals.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})
				continue

			frappe.db.commit()
			created.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})
			if status == "created_incomplete":
				incomplete.append({"deal_id": deal_id, "box_label": label, "sales_order": so_name})

		frappe.db.set_single_value("Biflorica Setting", "predeal_last_run", frappe.utils.now_datetime())
		frappe.db.commit()

		summary = {
			"fetched": len(predeals),
			"created": created,
			"existing": existing_deals,
			"failed": failed,
			"incomplete": incomplete,
			"skipped_past_count": skipped_past,
			"created_count": len(created),
			"existing_count": len(existing_deals),
			"failed_count": len(failed),
			"incomplete_count": len(incomplete),
		}
		shared_reason = _shared_failure_reason(failed, len(predeals) - skipped_past)
		if shared_reason:
			summary["shared_reason"] = shared_reason
		parts = []
		if created:
			parts.append(f"Created {len(created)} draft Sales Order(s)")
		if incomplete:
			parts.append(f"{len(incomplete)} need a Consignee before submit")
		if existing_deals:
			parts.append(f"{len(existing_deals)} already exist")
		if failed:
			parts.append(
				f"All {len(failed)} predeal(s) failed — {shared_reason}"
				if shared_reason
				else f"{len(failed)} issue(s)"
			)
		message = "; ".join(parts) if parts else "No new orders"

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
	predeal_id = po_no[len(PREDEAL_PO_PREFIX) :]
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
		_get_settings()  # raises if Biflorica Setting is not configured

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
def approve_deal(deal_id: str):
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
			frappe.db.set_single_value("Biflorica Setting", "deals_last_run", frappe.utils.now_datetime())
			frappe.db.commit()
		return result
	except Exception as e:
		frappe.log_error(str(e), "Biflorica Approve Deal Error")
		return {"success": False, "message": str(e)}
