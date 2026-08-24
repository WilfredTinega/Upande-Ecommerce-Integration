# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Every Shopify API call, successful or not.

Named "Error Log" as requested, but it records successes too — the point is a
single place to answer "did the integration talk to Shopify, and what came back".

Writing a log entry must never be the reason an API call fails, so each insert is
wrapped in a savepoint and any problem is swallowed after being reported to the
normal Error Log.
"""

import json

import frappe
from frappe.database.database import savepoint
from frappe.deferred_insert import deferred_insert as queue_insert
from frappe.model.document import Document
from frappe.utils import cstr, now

LOG_DOCTYPE = "Shopify API Error Log"
RESPONSE_CAP = 4000
QUERY_CAP = 4000
MESSAGE_CAP = 2000


class ShopifyAPIErrorLog(Document):
	@staticmethod
	def clear_old_logs(days=30):
		"""Interface Frappe's Log Settings requires.

		`_supports_log_clearing` gates registration on this method existing, so
		without it the doctype is never added to Log Settings and would grow forever.
		"""
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Shopify API Error Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


def _truncate(value, cap):
	if value is None:
		return None
	text = value if isinstance(value, str) else json.dumps(value, indent=2, default=cstr)
	if len(text) <= cap:
		return text
	return text[:cap] + f"\n... truncated, {len(text) - cap} more characters"


def _graphql_operation_name(query):
	"""Pull `ShopOrders` out of `query ShopOrders($cursor: String) { ... }`."""
	if not query:
		return None
	for token in cstr(query).strip().split():
		if token in ("query", "mutation"):
			continue
		name = token.split("(")[0].split("{")[0].strip()
		if name:
			return name[:140]
	return None


def _error_code(errors):
	"""First extensions.code from a GraphQL errors array, e.g. ACCESS_DENIED."""
	if not isinstance(errors, list):
		return None
	for error in errors:
		if isinstance(error, dict):
			code = (error.get("extensions") or {}).get("code")
			if code:
				return cstr(code)[:140]
	return None


def log_api_call(
	operation,
	status,
	settings=None,
	endpoint=None,
	http_method="POST",
	query=None,
	variables=None,
	response=None,
	errors=None,
	error_message=None,
	duration_ms=0,
	attempts=1,
	reference_doctype=None,
	reference_name=None,
):
	"""Record one call. Returns the log name, or None when logging is off/failed."""
	try:
		if settings is None:
			settings = frappe.get_cached_doc("Shopify Settings")

		if not settings.get("log_api_calls"):
			return None
		if status == "Success" and not settings.get("log_successful_calls"):
			return None

		if errors and not error_message:
			error_message = json.dumps(errors, indent=2, default=cstr)

		record = {
			"status": status,
			"operation": cstr(operation)[:140],
			# A string, not a datetime: the redis queue json.dumps() records with no
			# default= encoder, so a datetime object fails to serialise.
			"timestamp": now(),
			"shop_domain": settings.get("shop_domain"),
			"api_version": settings.get("api_version"),
			"endpoint": cstr(endpoint)[:255] if endpoint else None,
			"http_method": http_method,
			"graphql_operation": _graphql_operation_name(query),
			"error_code": _error_code(errors),
			"error_message": _truncate(error_message, MESSAGE_CAP),
			"duration_ms": int(duration_ms or 0),
			"attempts": int(attempts or 1),
			"request_query": _truncate(query, QUERY_CAP),
			"request_variables": _truncate(variables, RESPONSE_CAP) if variables else None,
			"response_snippet": _truncate(response, RESPONSE_CAP) if response is not None else None,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"user": frappe.session.user,
		}

		# Queued into redis rather than inserted here on purpose. Callers roll the
		# database back when a step fails — an inserted row would be rolled back with
		# it, losing exactly the failures this log exists to record. Redis is outside
		# that transaction, so the entry survives; flush_api_log() drains it once the
		# operation has committed.
		queue_insert(LOG_DOCTYPE, [record])
		return True
	except Exception:
		# Logging is never worth failing a sync over.
		frappe.log_error(frappe.get_traceback(), "Shopify API Log: could not queue entry")
		return None


@frappe.whitelist()
def flush_api_log():
	"""Drain queued entries into the table. Returns how many were written.

	Frappe's own `save_to_db` flushes this queue every 15 minutes; this exists so a
	button press shows its log immediately. Inserts with ignore_permissions so the
	doctype needn't grant create rights to anyone.
	"""
	written = 0
	queue_key = f"insert_queue_for_{LOG_DOCTYPE}"

	while True:
		raw = frappe.cache.lpop(queue_key)
		if not raw:
			break

		try:
			records = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Shopify API Log: unreadable queue entry")
			continue

		if isinstance(records, dict):
			records = [records]

		for record in records:
			with savepoint(catch=Exception):
				doc = frappe.new_doc(LOG_DOCTYPE)
				doc.update(record)
				doc.insert(ignore_permissions=True)
				written += 1

	if written:
		# Background sync: persist the records written above and the summary field
		# the form reads, so a later failure cannot discard a completed run.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return written
