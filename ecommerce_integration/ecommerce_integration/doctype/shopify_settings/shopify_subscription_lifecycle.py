# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Keeps ERPNext's view of a subscription in step with its own dates.

A subscription carries a start and an end date. Once the end date has passed the
subscription goes Inactive and stops generating deliveries — so ERPNext reflects
reality between polls rather than only just after one.
"""

from datetime import timedelta

import frappe
from frappe.utils import add_months, add_to_date, cint, cstr, getdate, nowdate

from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
	get_shopify_settings,
)

# Frequency -> how far apart consecutive deliveries fall.
FREQUENCY_STEP = {
	"Weekly": {"days": 7},
	"Fortnightly": {"days": 14},
	"Monthly": {"months": 1},
}

TERMINAL_STATUSES = ("Cancelled", "Expired", "Inactive")


def next_delivery_date(from_date, frequency, periods=1):
	"""Advance a date by `periods` steps of `frequency`."""
	step = FREQUENCY_STEP.get(frequency or "Monthly", {"months": 1})
	if "months" in step:
		return getdate(add_months(from_date, step["months"] * periods))
	return getdate(add_to_date(from_date, days=step["days"] * periods))


WEEKDAY_NUMBERS = {
	"monday": 0,
	"tuesday": 1,
	"wednesday": 2,
	"thursday": 3,
	"friday": 4,
	"saturday": 5,
	"sunday": 6,
}

# A delivery every day for a year is already absurd; this only stops a bad frequency
# or a start date in 1970 from spinning forever.
MAX_SCHEDULED_DATES = 400


def parse_delivery_days(text):
	""""Monday, Thu" -> [0, 3]. Unknown words are ignored rather than fatal."""
	found = []
	for token in cstr(text).replace(";", ",").split(","):
		token = token.strip().lower()
		if not token:
			continue
		for name, number in WEEKDAY_NUMBERS.items():
			# Accept "Mon" as well as "Monday", but not a bare "m".
			if len(token) >= 3 and name.startswith(token[:3]):
				found.append(number)
				break
	return sorted(set(found))


def scheduled_delivery_dates(start_date, frequency, delivery_days, until):
	"""Every delivery date from `start_date` through `until`, in order.

	With no weekdays chosen this is one delivery per period stepping from the start
	date — the original behaviour, so a subscription that never sets them keeps the
	dates it already had. With weekdays chosen, every chosen day inside each period is
	a delivery, which is how more than one delivery a week is expressed: Weekly plus
	Monday and Thursday is twice a week.

	Weekday selection is meaningless against a Monthly cadence — "the 15th of each
	month" is not a weekday — so it is ignored there rather than silently reinterpreted.
	"""
	if not start_date:
		return []

	start = getdate(start_date)
	until = getdate(until)
	if until < start:
		return []

	days = parse_delivery_days(delivery_days)
	frequency = frequency or "Monthly"

	if not days or frequency == "Monthly":
		dates = []
		for index in range(MAX_SCHEDULED_DATES):
			date = next_delivery_date(start, frequency, periods=index)
			if date > until:
				break
			dates.append(date)
		return dates

	period = 7 if frequency == "Weekly" else 14
	# Anchor on the Monday of the start week, so a chosen weekday earlier in the week
	# than the start day still belongs to a period rather than being skipped.
	cursor = start - timedelta(days=start.weekday())
	dates = []
	while cursor <= until and len(dates) < MAX_SCHEDULED_DATES:
		for weekday in days:
			date = cursor + timedelta(days=weekday)
			if start <= date <= until:
				dates.append(date)
		cursor += timedelta(days=period)
	return sorted(set(dates))


def end_date_for(start_date, frequency, deliveries_total):
	"""Last delivery date of a fixed-length subscription.

	3 boxes monthly from the 1st means deliveries on the 1st of months 0, 1 and 2 —
	so the end date is two steps out, not three.
	"""
	total = cint(deliveries_total)
	if not start_date or total < 1:
		return None
	return next_delivery_date(getdate(start_date), frequency, periods=total - 1)


@frappe.whitelist()
def expire_subscriptions(force=False):
	"""Flip subscriptions whose end date has passed to Inactive, and refresh the
	delivered/remaining counters on the ones still running."""
	settings = get_shopify_settings()

	if not force:
		if not settings.enabled:
			return {"skipped": True, "reason": "Shopify Settings is not enabled"}
		if not settings.exp_enabled:
			return {"skipped": True, "reason": "Subscription Expiry is disabled"}

	today = getdate(nowdate())
	expired = refreshed = failed = 0

	live = frappe.get_all(
		"Shopify Subscription",
		filters=[["status", "not in", TERMINAL_STATUSES]],
		fields=["name", "end_date", "deliveries_total", "source_order"],
	)

	for subscription in live:
		try:
			completed = _count_completed(subscription)
			total = cint(subscription.deliveries_total)
			remaining = max(total - completed, 0) if total else 0

			updates = {
				"deliveries_completed": completed,
				"deliveries_remaining": remaining,
			}

			# Two independent ways to be finished: the end date has passed, or every
			# purchased delivery has shipped.
			past_end = subscription.end_date and getdate(subscription.end_date) < today
			all_delivered = bool(total) and completed >= total

			if past_end or all_delivered:
				updates["status"] = "Inactive"
				expired += 1
			else:
				refreshed += 1

			frappe.db.set_value("Shopify Subscription", subscription.name, updates, update_modified=False)
		except Exception as e:
			failed += 1
			frappe.log_error(cstr(e), f"Shopify Expiry: {subscription.name}")

	summary = f"made inactive {expired}, still running {refreshed}, failed {failed}"
	settings.db_set("last_expiry_summary", summary, update_modified=False)
	frappe.db.commit()

	return {"summary": summary, "inactive": expired, "running": refreshed, "failed": failed}


def _count_completed(subscription):
	"""Deliveries that actually went out — shipped allocations against this
	subscription."""
	return cint(
		frappe.db.count(
			"Shopify Allocation",
			{"shopify_subscription": subscription.name, "status": "Shipped", "docstatus": 1},
		)
	)
