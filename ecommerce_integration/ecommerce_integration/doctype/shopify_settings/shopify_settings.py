# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import json
import time
from datetime import timezone
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, get_datetime, get_system_timezone, now_datetime

DEFAULT_API_VERSION = "2026-07"

# One Scheduled Job Type per task, keyed by `method` — same shape Floriday and
# Biflorica use, so the three connectors behave identically on the scheduler side.
SCHEDULER_TASKS = [
	(
		# Derived from stored orders, not from `subscriptionContracts`: this store sells
		# no selling plans, so Shopify holds no contracts to read. The contract sync is
		# still in the tree for a store that does, but nothing schedules it.
		"sub",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_order_pull.rebuild_subscriptions_from_orders",
		"Shopify: Derive Subscriptions from Orders",
	),
	(
		"ord",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_order_pull.sync_orders",
		"Shopify: Sync Orders",
	),
	(
		"alloc",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator.generate_allocations",
		"Shopify: Generate Allocations",
	),
	(
		"exp",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle.expire_subscriptions",
		"Shopify: Expire Subscriptions",
	),
	(
		"tok",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings.refresh_access_token_if_due",
		"Shopify: Refresh Access Token",
	),
]

DEFAULT_TOKEN_BUFFER_MINUTES = 120

# What the connector actually needs to read. A client_credentials token carries
# exactly the scopes the *app* is configured for, so this is checked against the
# grant response rather than assumed.
REQUIRED_SCOPES = ("read_orders", "read_products", "read_customers")

# Optional, and deliberately NOT in REQUIRED_SCOPES: the connector works fine
# without it, so its absence must not raise the "missing scopes" warning. It gates
# exactly one query, the `subscriptionContracts` connection, which Shopify refuses
# outright without it. Protected scope — Shopify must approve it before an app
# version can even request it.
SUBSCRIPTION_CONTRACT_SCOPE = "read_own_subscription_contracts"

# Fallback cadence per task. A field default only applies to a newly created doc,
# so on an existing Shopify Settings a freshly shipped `<prefix>_event_frequency`
# stays empty — and an enabled task with no frequency is treated as unschedulable,
# silently registering no job at all. These are applied on save instead.
DEFAULT_FREQUENCIES = {
	"sub": "Hourly",
	"ord": "Hourly",
	"alloc": "Daily",
	"exp": "Daily",
	"tok": "Hourly",
}


class ShopifyAPIError(frappe.ValidationError):
	pass


class ShopifySettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_token: DF.Password | None
		alloc_cron_format: DF.Data | None
		alloc_enabled: DF.Check
		alloc_event_frequency: DF.Literal["", "All", "Hourly", "Daily", "Weekly", "Cron"]
		alloc_last_run: DF.Datetime | None
		alloc_next_run: DF.Datetime | None
		api_log_retention_days: DF.Int
		api_version: DF.Data | None
		attr_duration: DF.Data | None
		attr_frequency: DF.Data | None
		attr_note: DF.Data | None
		attr_recipient_name: DF.Data | None
		attr_recipient_phone: DF.Data | None
		attr_special_requests: DF.Data | None
		attr_start_date: DF.Data | None
		client_id: DF.Data | None
		client_secret: DF.Password | None
		connection_status: DF.Data | None
		create_missing_customer: DF.Check
		default_company: DF.Link | None
		default_customer: DF.Link | None
		default_customer_group: DF.Link | None
		default_frequency: DF.Literal["", "Weekly", "Fortnightly", "Monthly"]
		default_price_list: DF.Link | None
		default_reserve_warehouse: DF.Link | None
		default_source_warehouse: DF.Link | None
		default_territory: DF.Link | None
		enabled: DF.Check
		exp_cron_format: DF.Data | None
		exp_enabled: DF.Check
		exp_event_frequency: DF.Literal["", "All", "Hourly", "Daily", "Weekly", "Cron"]
		exp_last_run: DF.Datetime | None
		exp_next_run: DF.Datetime | None
		granted_scopes: DF.SmallText | None
		last_allocation_summary: DF.SmallText | None
		last_expiry_summary: DF.SmallText | None
		last_full_run: DF.SmallText | None
		last_order_sync_summary: DF.SmallText | None
		last_order_updated_at: DF.Datetime | None
		last_sync_summary: DF.SmallText | None
		last_sync_updated_at: DF.Datetime | None
		log_api_calls: DF.Check
		log_successful_calls: DF.Check
		map_summary: DF.SmallText | None
		oauth_status: DF.Data | None
		ord_cron_format: DF.Data | None
		ord_enabled: DF.Check
		ord_event_frequency: DF.Literal[
			"", "All", "Hourly", "Hourly Long", "Daily", "Daily Long", "Weekly", "Cron"
		]
		ord_last_run: DF.Datetime | None
		ord_next_run: DF.Datetime | None
		order_lookback_days: DF.Int
		requested_scopes: DF.SmallText | None
		shop_domain: DF.Data | None
		sub_cron_format: DF.Data | None
		sub_enabled: DF.Check
		sub_event_frequency: DF.Literal[
			"", "All", "Hourly", "Hourly Long", "Daily", "Daily Long", "Weekly", "Cron"
		]
		sub_last_run: DF.Datetime | None
		sub_next_run: DF.Datetime | None
		tok_cron_format: DF.Data | None
		tok_enabled: DF.Check
		tok_event_frequency: DF.Literal["", "All", "Hourly", "Daily", "Cron"]
		tok_last_run: DF.Datetime | None
		tok_next_run: DF.Datetime | None
		token_expires_on: DF.Datetime | None
		token_refresh_buffer_minutes: DF.Int
	# end: auto-generated types

	def onload(self):
		"""Show last_run / next_run from each Scheduled Job Type when the form loads."""
		self._populate_scheduler_run_times()

	def validate(self):
		if self.shop_domain:
			# Users paste the full admin URL often enough that it's worth normalising.
			domain = self.shop_domain.strip().strip("/")
			for prefix in ("https://", "http://"):
				if domain.startswith(prefix):
					domain = domain[len(prefix) :]
			self.shop_domain = domain.split("/")[0]

		if self.default_source_warehouse and self.default_reserve_warehouse:
			if self.default_source_warehouse == self.default_reserve_warehouse:
				frappe.throw(_("Default Source Warehouse and Default Reserve Warehouse cannot be the same."))

		self._backfill_task_frequencies()

	def _backfill_task_frequencies(self):
		"""Give any enabled task a cadence, so ticking Enabled is enough on its own.

		Without this, enabling a task whose frequency field is blank registers no
		Scheduled Job Type and gives no indication why.
		"""
		for prefix, _method, _label in SCHEDULER_TASKS:
			if not self.get(f"{prefix}_enabled"):
				continue
			if (self.get(f"{prefix}_event_frequency") or "").strip():
				continue
			self.set(f"{prefix}_event_frequency", DEFAULT_FREQUENCIES.get(prefix, "Daily"))

	def on_update(self):
		self._sync_scheduled_jobs()
		self._sync_log_retention()

	def _sync_log_retention(self):
		"""Push the retention set here into Frappe's Log Settings.

		Frappe clears logs from Log Settings, not from this doctype, so without this
		the field would look like a control and do nothing.
		"""
		days = cint(self.api_log_retention_days)
		if days <= 0:
			return

		try:
			log_settings = frappe.get_doc("Log Settings")
		except Exception:
			return

		for row in log_settings.logs_to_clear:
			if row.ref_doctype == "Shopify API Error Log":
				if cint(row.days) != days:
					row.days = days
					log_settings.save(ignore_permissions=True)
				return

		log_settings.append("logs_to_clear", {"ref_doctype": "Shopify API Error Log", "days": days})
		log_settings.save(ignore_permissions=True)

	# ------------------------------------------------------------------ buttons

	@frappe.whitelist()
	def test_connection(self):
		"""Cheapest possible round trip, so credentials can be proven before anyone
		waits on a sync."""
		try:
			data = shopify_graphql(
				"{ shop { name myshopifyDomain currencyCode } }",
				settings=self,
				operation="Test Connection",
			)
		except Exception as e:
			message = f"Request failed: {e}"
			self.db_set("connection_status", message[:900], update_modified=False)
			return {"ok": False, "message": message}

		shop = data.get("shop") or {}
		if not shop.get("myshopifyDomain"):
			message = f"Unexpected response: {json.dumps(data)}"
			self.db_set("connection_status", message[:900], update_modified=False)
			return {"ok": False, "message": message}

		message = (
			f"Connected to {shop.get('name')} ({shop.get('myshopifyDomain')}, {shop.get('currencyCode')})"
		)

		# Connecting proves the token is valid, not that it can read anything. A
		# client_credentials token carries exactly the scopes the app is configured
		# for, so a scopeless app authenticates fine and then denies every query.
		# Pull the live scope list before judging it, so pressing Test Connection after
		# changing scopes in Shopify reflects reality immediately.
		try:
			fetch_granted_scopes(self)
		except Exception as e:
			frappe.log_error(frappe.utils.cstr(e), "Shopify: could not read access scopes")

		gaps = missing_scopes(self)
		if gaps:
			message += f"\n\nMISSING SCOPES: {', '.join(gaps)}. "
			if not (self.requested_scopes or "").strip():
				# The app itself declares nothing, so there is nothing for Shopify to grant.
				message += (
					"The Shopify app requests no Admin API scopes at all, so none can be "
					"granted. In the Dev Dashboard open the app > Versions > Create a version, "
					"add these scopes to the app scopes field and Release it. Scopes live on a "
					"version, not on the Settings page."
				)
			else:
				message += (
					f"The app requests [{self.requested_scopes}] but this installation was not "
					"granted them. Reinstall/update the app on the store to accept the expanded "
					"permissions, then press Refresh Access Token."
				)

		self.db_set("connection_status", message[:900], update_modified=False)
		_flush_api_log()
		return {"ok": True, "message": message}

	@frappe.whitelist()
	def refresh_token_now(self):
		return refresh_access_token(self)

	@frappe.whitelist()
	def sync_now(self):
		"""Re-derive subscriptions from the orders already stored — no Shopify call."""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_order_pull import (
			rebuild_subscriptions_from_orders,
		)

		return rebuild_subscriptions_from_orders(force=True)

	@frappe.whitelist()
	def sync_contracts_now(self):
		"""The `subscriptionContracts` path, kept for a store that uses selling plans.
		Nothing schedules it; it skips itself when the scope is not granted."""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_sync import (
			sync_subscription_contracts,
		)

		return sync_subscription_contracts(force=True)

	@frappe.whitelist()
	def sync_orders_now(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_order_pull import (
			sync_orders,
		)

		return sync_orders(force=True)

	@frappe.whitelist()
	def seed_product_map(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_product_map.shopify_product_map import (
			seed_product_map,
		)

		return seed_product_map()

	@frappe.whitelist()
	def expire_now(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle import (
			expire_subscriptions,
		)

		return expire_subscriptions(force=True)

	@frappe.whitelist()
	def run_full_sync(self):
		"""Whole chain, in dependency order, driven from the form.

		Each step is reported independently and a failure does not abort the rest —
		a denied scope on products should not hide whether orders would have synced.
		"""
		from ecommerce_integration.ecommerce_integration.doctype.shopify_product_map.shopify_product_map import (
			seed_product_map,
		)
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			generate_allocations,
		)
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_order_pull import (
			rebuild_subscriptions_from_orders,
			sync_orders,
		)
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_lifecycle import (
			expire_subscriptions,
		)

		steps = [
			("Refresh token", lambda: refresh_access_token_if_due(force=True)),
			("Product map", seed_product_map),
			("Orders", lambda: sync_orders(force=True)),
			("Subscriptions", lambda: rebuild_subscriptions_from_orders(force=True)),
			("Allocations", lambda: generate_allocations(force=True)),
			("Expiry", lambda: expire_subscriptions(force=True)),
		]

		results = []
		for label, fn in steps:
			try:
				outcome = fn() or {}
				if outcome.get("skipped"):
					results.append(
						{"step": label, "ok": True, "detail": f"skipped — {outcome.get('reason')}"}
					)
				else:
					detail = outcome.get("summary") or outcome.get("message") or "done"
					# A sync that aborted on a GraphQL error returns normally rather than
					# raising, so trust its own flag over the absence of an exception.
					ok = not outcome.get("aborted") and not cint(outcome.get("failed"))
					results.append({"step": label, "ok": ok, "detail": detail})
			except Exception as e:
				frappe.db.rollback()
				results.append({"step": label, "ok": False, "detail": frappe.utils.cstr(e)[:400]})
				frappe.log_error(frappe.utils.cstr(e), f"Shopify Full Sync: {label}")

		summary = " | ".join(f"{r['step']}: {'ok' if r['ok'] else 'FAILED'}" for r in results)
		self.db_set("last_full_run", f"{now_datetime()} — {summary}"[:900], update_modified=False)
		# Background sync: persist the records written above and the summary field
		# the form reads, so a later failure cannot discard a completed run.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		_flush_api_log()
		return {"results": results, "summary": summary}

	@frappe.whitelist()
	def generate_allocations(self):
		from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_allocation_generator import (
			generate_allocations as _generate,
		)

		return _generate(force=True)

	# ------------------------------------------------------------------ scheduler

	def _populate_scheduler_run_times(self):
		for prefix, method, _label in SCHEDULER_TASKS:
			row = frappe.db.get_value(
				"Scheduled Job Type",
				{"method": method},
				["name", "last_execution"],
				as_dict=True,
			)
			next_run = None
			if row and row.name:
				try:
					job = frappe.get_cached_doc("Scheduled Job Type", row.name)
					if not job.stopped:
						next_run = job.get_next_execution()
				except Exception:
					next_run = None

			# Presentation-only fields — set in memory, never written back, so a form
			# load doesn't churn `modified`.
			self.set(f"{prefix}_last_run", row.last_execution if row else None)
			self.set(f"{prefix}_next_run", next_run)

	def _sync_scheduled_jobs(self, force=False):
		"""Mirror the user's frequency/cron/enabled choices into Scheduled Job Type rows.

		Per-task short-circuit: if none of (frequency, cron, enabled) changed for a
		task on this save, leave its job alone — saving Settings should never reset
		last_execution on a schedule nobody touched.

		Pass force=True from after_migrate, which wipes the rows.
		"""
		for prefix, method, _label in SCHEDULER_TASKS:
			fields = (
				f"{prefix}_event_frequency",
				f"{prefix}_cron_format",
				f"{prefix}_enabled",
			)
			if not force and not any(self.has_value_changed(f) for f in fields):
				continue

			self._upsert_scheduled_job(prefix, method)

	def _upsert_scheduled_job(self, prefix, method):
		frequency = (self.get(f"{prefix}_event_frequency") or "").strip()
		cron_format = (self.get(f"{prefix}_cron_format") or "").strip()
		enabled = bool(self.get(f"{prefix}_enabled"))

		stopped = 1 if (not enabled or not frequency) else 0
		if frequency == "Cron" and not cron_format:
			stopped = 1

		# Frappe's `next_execution` getter parses cron_format unconditionally, even for
		# stopped rows, so a Cron row with no cron string crashes sync_jobs. Downgrade
		# to a Daily placeholder instead.
		effective_frequency = "Daily" if (frequency == "Cron" and not cron_format) else frequency

		job_name = frappe.db.get_value("Scheduled Job Type", {"method": method})

		if not job_name:
			if stopped:
				return
			job = frappe.new_doc("Scheduled Job Type")
			job.method = method
			job.frequency = effective_frequency
			job.cron_format = cron_format if effective_frequency == "Cron" else ""
			job.create_log = effective_frequency not in ("All", "Cron")
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


def _flush_api_log():
	"""Drain queued API log entries now, so a UI action shows its own log rows."""
	from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
		flush_api_log,
	)

	return flush_api_log()


def get_shopify_settings():
	return frappe.get_single("Shopify Settings")


def shopify_datetime(value):
	"""Convert a Shopify timestamp into something Frappe can store.

	Shopify sends ISO-8601 UTC, e.g. `2026-07-27T08:54:37Z`. Two problems with using
	that directly: `get_datetime` returns a *timezone-aware* datetime, which MariaDB
	rejects outright ("Incorrect datetime value"), and even stripping the marker
	would leave the value 3 hours out on an Africa/Nairobi system. So convert to the
	system timezone and drop the tzinfo, which is how Frappe stores datetimes.
	"""
	if not value:
		return None

	parsed = get_datetime(value)
	if parsed is None:
		return None
	if parsed.tzinfo is None:
		return parsed
	return parsed.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def to_shopify_utc(value):
	"""Inverse of shopify_datetime, for Shopify `query:` filters.

	A system-timezone value handed to Shopify labelled as UTC would be wrong by the
	offset, silently shifting which records a watermark selects.
	"""
	if not value:
		return None

	parsed = get_datetime(value)
	if parsed is None:
		return None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ACCESS_SCOPES_QUERY = """
{
  currentAppInstallation {
    accessScopes { handle }
    app { title handle apiKey requestedAccessScopes { handle } }
  }
}
"""


def fetch_granted_scopes(settings=None):
	"""Read the scopes the installed app actually holds and store them.

	More authoritative than the `scope` field on the client_credentials response,
	and it works for a pasted long-lived shpat_ token too — that path has no grant
	response at all, so without this its scopes would stay blank forever and the
	"missing scopes" warning would nag incorrectly.
	"""
	settings = settings or get_shopify_settings()

	data = shopify_graphql(ACCESS_SCOPES_QUERY, settings=settings, operation="Check Access Scopes")
	installation = data.get("currentAppInstallation") or {}
	app = installation.get("app") or {}

	granted = ",".join(s["handle"] for s in (installation.get("accessScopes") or []) if s.get("handle"))
	# What the app's own configuration asks for. Distinguishes "nobody ever added
	# scopes to the app" from "scopes are declared but this installation wasn't
	# granted them" — different fixes entirely.
	requested = ",".join(s["handle"] for s in (app.get("requestedAccessScopes") or []) if s.get("handle"))

	if (settings.granted_scopes or "") != granted:
		settings.db_set("granted_scopes", granted, update_modified=False)
		settings.granted_scopes = granted
	if (settings.requested_scopes or "") != requested:
		settings.db_set("requested_scopes", requested, update_modified=False)
		settings.requested_scopes = requested

	return {
		"scopes": granted,
		"requested": requested,
		"app": app.get("title"),
		"app_api_key": app.get("apiKey"),
		"missing": missing_scopes(settings),
	}


def missing_scopes(settings=None):
	"""Required scopes the current token does NOT carry.

	Shopify returns granted scopes comma-separated, sometimes with whitespace, and
	may return a broader `write_*` that implies its `read_*` counterpart.
	"""
	settings = settings or get_shopify_settings()
	granted = {s.strip() for s in (settings.granted_scopes or "").split(",") if s.strip()}

	missing = []
	for scope in REQUIRED_SCOPES:
		implied_by_write = scope.replace("read_", "write_", 1)
		if scope not in granted and implied_by_write not in granted:
			missing.append(scope)
	return missing


def scope_status(scope, settings=None):
	"""Whether the installed app holds `scope`: "granted", "missing" or "unknown".

	"unknown" carries as much weight as the other two. `granted_scopes` is only
	populated by fetch_granted_scopes(), so a Settings doc nobody has tested yet
	knows nothing about its own permissions — callers must not read that silence as
	a denial and refuse to run. Shopify is the authority; a real request is how you
	find out.
	"""
	settings = settings or get_shopify_settings()
	granted = {s.strip() for s in (settings.granted_scopes or "").split(",") if s.strip()}

	if not granted:
		return "unknown"
	if scope in granted or scope.replace("read_", "write_", 1) in granted:
		return "granted"
	return "missing"


def refresh_access_token(settings=None):
	"""Mint an Admin API token from the app's Client ID and Secret.

	Shopify's `client_credentials` grant is available to apps built for your own
	store and needs no merchant authorisation:

	    POST https://{shop}/admin/oauth/access_token
	    Content-Type: application/x-www-form-urlencoded
	    grant_type=client_credentials&client_id=...&client_secret=...

	The token it returns lives about 24 hours, so it is treated as a cache rather
	than a credential to maintain — see `_ensure_access_token`.
	"""
	settings = settings or get_shopify_settings()

	if not settings.shop_domain:
		frappe.throw(_("Shopify Settings: Shop Domain is not set."))

	client_secret = settings.get_password("client_secret", raise_exception=False)
	if not (settings.client_id and client_secret):
		frappe.throw(_("Shopify Settings: Client ID and Client Secret are required to mint a token."))

	from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
		log_api_call,
	)

	url = f"https://{settings.shop_domain}/admin/oauth/access_token"
	started = time.monotonic()
	try:
		response = make_post_request(
			url,
			data={
				"grant_type": "client_credentials",
				"client_id": settings.client_id,
				"client_secret": client_secret,
			},
		)
	except Exception as e:
		message = f"Token request failed: {e}"
		settings.db_set("oauth_status", message[:900], update_modified=False)
		log_api_call(
			"Refresh Access Token",
			"Failed",
			settings=settings,
			endpoint=url,
			error_message=message,
			duration_ms=(time.monotonic() - started) * 1000,
		)
		raise

	token = (response or {}).get("access_token")
	if not token:
		message = f"Token endpoint returned no access_token. Response: {json.dumps(response)}"
		settings.db_set("oauth_status", message[:900], update_modified=False)
		log_api_call(
			"Refresh Access Token",
			"Failed",
			settings=settings,
			endpoint=url,
			error_message=message,
			duration_ms=(time.monotonic() - started) * 1000,
		)
		raise ShopifyAPIError(message)

	# Expire a minute early so a long-running job can't be caught mid-flight.
	expires_in = cint(response.get("expires_in")) or 86400
	expires_on = add_to_date(now_datetime(), seconds=max(expires_in - 60, 60))

	settings.access_token = token
	settings.token_expires_on = expires_on
	settings.granted_scopes = response.get("scope")
	settings.oauth_status = f"Token minted, valid until {expires_on}"
	settings.save(ignore_permissions=True)

	# The token itself is never logged — only that one was issued, and with what.
	log_api_call(
		"Refresh Access Token",
		"Success",
		settings=settings,
		endpoint=url,
		response={"expires_in": expires_in, "scope": response.get("scope")},
		duration_ms=(time.monotonic() - started) * 1000,
	)
	# Background sync: persist the records written above and the summary field
	# the form reads, so a later failure cannot discard a completed run.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	_flush_api_log()

	# The mint response's `scope` can be empty even when the installation has scopes,
	# so confirm against the installation itself.
	try:
		fetch_granted_scopes(settings)
	except Exception as e:
		frappe.log_error(frappe.utils.cstr(e), "Shopify: could not read access scopes")

	gaps = missing_scopes(settings)
	return {
		"ok": True,
		"expires_on": str(expires_on),
		"scopes": response.get("scope"),
		"missing_scopes": gaps,
		"message": settings.oauth_status
		+ (f". Still missing: {', '.join(gaps)}" if gaps else ". All required scopes present."),
	}


def can_mint_token(settings):
	"""True when app credentials are present, so a token can be minted on demand."""
	return bool(settings.client_id and settings.get_password("client_secret", raise_exception=False))


def token_is_due(settings):
	"""True when the token is missing, or close enough to expiry to replace now.

	Uses a buffer rather than the exact expiry so a token is never handed to a
	long-running job moments before it dies.
	"""
	if not can_mint_token(settings):
		return False
	if not settings.get_password("access_token", raise_exception=False):
		return True
	if not settings.token_expires_on:
		return True

	buffer_minutes = cint(settings.token_refresh_buffer_minutes) or DEFAULT_TOKEN_BUFFER_MINUTES
	return get_datetime(settings.token_expires_on) <= add_to_date(now_datetime(), minutes=buffer_minutes)


def _ensure_access_token(settings):
	"""Return a usable token, minting a fresh one first if the current one is due.

	Checked on every API call as well as on a schedule, so a 24-hour expiry can
	never surface as a failed sync because a cron slot was missed.
	"""
	if token_is_due(settings):
		refresh_access_token(settings)
		settings.reload()
		return settings.get_password("access_token", raise_exception=False)

	token = settings.get_password("access_token", raise_exception=False)
	if token:
		# Either still well within its life, or a pasted long-lived token with no
		# expiry for us to honour.
		return token

	frappe.throw(
		_(
			"Shopify Settings: no Admin API Access Token. Either paste a shpat_ token, "
			"or set the Client ID and Secret so one can be minted."
		)
	)


@frappe.whitelist()
def refresh_access_token_if_due(force: bool = False):
	"""Scheduled counterpart to the lazy refresh: keeps the token warm even when
	nothing has called Shopify."""
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.tok_enabled:
			return {"skipped": True, "reason": "Auto-Refresh Token is disabled"}

	if not can_mint_token(settings):
		return {"skipped": True, "reason": "No Client ID / Secret to mint a token with"}

	if not token_is_due(settings):
		return {
			"skipped": True,
			"reason": f"Token still valid until {settings.token_expires_on}",
			"expires_on": str(settings.token_expires_on),
		}

	return refresh_access_token(settings)


def shopify_graphql(query, variables=None, settings=None, retries=3, operation=None):
	"""POST a GraphQL document to the Shopify Admin API and return its `data`.

	Raises ShopifyAPIError on a GraphQL-level error. Shopify answers throttled
	requests with HTTP 200 and a THROTTLED extension rather than an error status,
	so that case is retried with a backoff instead of surfacing as a failure.

	Every call — successful or not — is recorded in Shopify API Error Log. This is
	the single choke point for Shopify traffic, so instrumenting it here covers
	every caller rather than relying on each one to remember.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
		log_api_call,
	)

	settings = settings or get_shopify_settings()

	if not settings.shop_domain:
		frappe.throw(_("Shopify Settings: Shop Domain is not set."))

	token = _ensure_access_token(settings)

	url = (
		f"https://{settings.shop_domain}/admin/api/"
		f"{(settings.api_version or DEFAULT_API_VERSION).strip()}/graphql.json"
	)
	headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
	payload = {"query": query}
	if variables:
		payload["variables"] = variables

	label = operation or "GraphQL"
	started = time.monotonic()
	last_error = None
	attempt = 0

	for attempt in range(1, retries + 1):
		try:
			response = make_post_request(url, headers=headers, data=json.dumps(payload))
		except Exception as e:
			# Transport-level failure: no GraphQL body to inspect.
			log_api_call(
				label,
				"Failed",
				settings=settings,
				endpoint=url,
				query=query,
				variables=variables,
				error_message=f"Request failed: {e}",
				duration_ms=(time.monotonic() - started) * 1000,
				attempts=attempt,
			)
			raise

		errors = response.get("errors")

		if not errors:
			log_api_call(
				label,
				"Success",
				settings=settings,
				endpoint=url,
				query=query,
				variables=variables,
				response=response.get("data"),
				duration_ms=(time.monotonic() - started) * 1000,
				attempts=attempt,
			)
			return response.get("data") or {}

		throttled = any(
			(e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors if isinstance(e, dict)
		)
		last_error = json.dumps(errors)
		if not throttled:
			break

		time.sleep(2 * attempt)

	log_api_call(
		label,
		"Failed",
		settings=settings,
		endpoint=url,
		query=query,
		variables=variables,
		response=response,
		errors=json.loads(last_error) if last_error else None,
		duration_ms=(time.monotonic() - started) * 1000,
		attempts=attempt,
	)

	raise ShopifyAPIError(f"Shopify GraphQL error: {last_error}")


def resync_scheduled_jobs():
	"""after_migrate hook — migrate prunes Scheduled Job Type rows that aren't in
	scheduler_events, and these are configured per Settings doc rather than in
	hooks, so they have to be re-upserted."""
	if not frappe.db.exists("DocType", "Shopify Settings"):
		return
	settings = get_shopify_settings()
	settings._sync_scheduled_jobs(force=True)
	# after_migrate hook: no request transaction to persist the job upserts.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit


def ensure_log_retention():
	"""after_migrate hook — re-assert the Shopify API log retention in Frappe's
	Log Settings.

	`_sync_log_retention` only fires on Shopify Settings save, and `Log Settings`
	is site data: a row dropped there (or a site restored from a backup taken
	before the field was set) leaves Shopify API Error Log growing unbounded with
	nothing in the form to indicate it."""
	if not frappe.db.exists("DocType", "Shopify Settings"):
		return
	settings = get_shopify_settings()
	settings._sync_log_retention()
	# after_migrate hook: no request transaction to persist the retention row.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
