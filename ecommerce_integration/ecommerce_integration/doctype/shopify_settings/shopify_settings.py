# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import json
import time

import frappe
from frappe.integrations.utils import make_post_request
from frappe.model.document import Document

DEFAULT_API_VERSION = "2026-07"

# One Scheduled Job Type per task, keyed by `method` — same shape Floriday and
# Biflorica use, so the three connectors behave identically on the scheduler side.
SCHEDULER_TASKS = [
	(
		"sub",
		"ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_subscription_sync.sync_subscription_contracts",
		"Shopify: Sync Subscription Contracts",
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
]


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
		api_version: DF.Data | None
		attr_duration: DF.Data | None
		attr_frequency: DF.Data | None
		attr_note: DF.Data | None
		attr_recipient_name: DF.Data | None
		attr_recipient_phone: DF.Data | None
		attr_special_requests: DF.Data | None
		attr_start_date: DF.Data | None
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
		last_allocation_summary: DF.SmallText | None
		last_expiry_summary: DF.SmallText | None
		last_order_sync_summary: DF.SmallText | None
		last_order_updated_at: DF.Datetime | None
		last_sync_summary: DF.SmallText | None
		last_sync_updated_at: DF.Datetime | None
		map_summary: DF.SmallText | None
		ord_cron_format: DF.Data | None
		ord_enabled: DF.Check
		ord_event_frequency: DF.Literal[
			"", "All", "Hourly", "Hourly Long", "Daily", "Daily Long", "Weekly", "Cron"
		]
		ord_last_run: DF.Datetime | None
		ord_next_run: DF.Datetime | None
		order_lookback_days: DF.Int
		shop_domain: DF.Data | None
		sub_cron_format: DF.Data | None
		sub_enabled: DF.Check
		sub_event_frequency: DF.Literal[
			"", "All", "Hourly", "Hourly Long", "Daily", "Daily Long", "Weekly", "Cron"
		]
		sub_last_run: DF.Datetime | None
		sub_next_run: DF.Datetime | None
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
				frappe.throw("Default Source Warehouse and Default Reserve Warehouse cannot be the same.")

	def on_update(self):
		self._sync_scheduled_jobs()

	# ------------------------------------------------------------------ buttons

	@frappe.whitelist()
	def test_connection(self):
		"""Cheapest possible round trip, so credentials can be proven before anyone
		waits on a sync."""
		try:
			data = shopify_graphql("{ shop { name myshopifyDomain currencyCode } }", settings=self)
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
		self.db_set("connection_status", message[:900], update_modified=False)
		return {"ok": True, "message": message}

	@frappe.whitelist()
	def sync_now(self):
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


def get_shopify_settings():
	return frappe.get_single("Shopify Settings")


def shopify_graphql(query, variables=None, settings=None, retries=3):
	"""POST a GraphQL document to the Shopify Admin API and return its `data`.

	Raises ShopifyAPIError on a GraphQL-level error. Shopify answers throttled
	requests with HTTP 200 and a THROTTLED extension rather than an error status,
	so that case is retried with a backoff instead of surfacing as a failure.
	"""
	settings = settings or get_shopify_settings()

	if not settings.shop_domain:
		frappe.throw("Shopify Settings: Shop Domain is not set.")

	token = settings.get_password("access_token", raise_exception=False)
	if not token:
		frappe.throw("Shopify Settings: Admin API Access Token is not set.")

	url = (
		f"https://{settings.shop_domain}/admin/api/"
		f"{(settings.api_version or DEFAULT_API_VERSION).strip()}/graphql.json"
	)
	headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
	payload = {"query": query}
	if variables:
		payload["variables"] = variables

	last_error = None
	for attempt in range(retries):
		response = make_post_request(url, headers=headers, data=json.dumps(payload))
		errors = response.get("errors")

		if not errors:
			return response.get("data") or {}

		throttled = any(
			(e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors if isinstance(e, dict)
		)
		last_error = json.dumps(errors)
		if not throttled:
			break

		time.sleep(2 * (attempt + 1))

	raise ShopifyAPIError(f"Shopify GraphQL error: {last_error}")


def resync_scheduled_jobs():
	"""after_migrate hook — migrate prunes Scheduled Job Type rows that aren't in
	scheduler_events, and these are configured per Settings doc rather than in
	hooks, so they have to be re-upserted."""
	if not frappe.db.exists("DocType", "Shopify Settings"):
		return
	settings = get_shopify_settings()
	settings._sync_scheduled_jobs(force=True)
	frappe.db.commit()
