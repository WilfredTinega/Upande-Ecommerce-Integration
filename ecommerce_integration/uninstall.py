# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Leave nothing behind when the app is uninstalled.

Frappe removes an app's own DocTypes, Workspaces and scheduled jobs on uninstall,
but NOT the Custom Fields and Property Setters it added to other apps' doctypes —
those are ordinary site records and simply stay, pointing at fields and options
that no longer mean anything. On a site where this app is installed and removed
repeatedly (CI, a staging rebuild) that leaves a growing pile of orphans, and a
Link field left behind aimed at a deleted DocType breaks the test-record walk and
`bench migrate` for everyone else.

So `before_uninstall` deletes exactly what this app created: the fields declared
in FLORIDAY_CUSTOM_FIELDS / BIFLORICA_CUSTOM_FIELDS and the scheduled jobs keyed
to this app's methods. Nothing is guessed — only declared fields are removed, and
only when they are still present.
"""

import frappe


def _declared_custom_fields():
	"""[(doctype, fieldname)] for every Custom Field this app declares."""
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_custom_fields import (
		BIFLORICA_CUSTOM_FIELDS,
	)
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
		FLORIDAY_CUSTOM_FIELDS,
	)

	seen = []
	for spec in list(FLORIDAY_CUSTOM_FIELDS) + list(BIFLORICA_CUSTOM_FIELDS):
		pair = (spec["dt"], spec["df"]["fieldname"])
		if pair not in seen:
			seen.append(pair)
	return seen


def _fields_owned_by_other_apps():
	"""{(doctype, fieldname)} that another INSTALLED app also declares.

	Declaring a field does not make it ours to delete. `Sales Order.custom_farm`
	is declared here and by upande_harvest; `custom_order_name` and
	`custom_consignee` by upande_packhouse. Removing those on uninstall would rip
	mandatory fields out from under apps that are still installed — and on this
	bench that is 3 of the 29 fields declared here, so it is not a corner case.

	Read from each installed app's own `custom/*.json` fixtures, which is where a
	Frappe app declares the fields it owns.
	"""
	import json
	import pathlib

	shared = set()
	for app in frappe.get_installed_apps():
		if app == "ecommerce_integration":
			continue
		try:
			custom_dir = pathlib.Path(frappe.get_app_path(app, app, "custom"))
		except Exception:
			continue
		if not custom_dir.is_dir():
			continue
		for path in custom_dir.glob("*.json"):
			try:
				data = json.loads(path.read_text())
			except (OSError, ValueError):
				continue
			for field in data.get("custom_fields") or []:
				if field.get("dt") and field.get("fieldname"):
					shared.add((field["dt"], field["fieldname"]))
	return shared


def remove_custom_fields():
	"""Delete the Custom Fields this app OWNS — never one another app also declares."""
	owned_elsewhere = _fields_owned_by_other_apps()

	removed, kept = [], []
	for doctype, fieldname in _declared_custom_fields():
		if (doctype, fieldname) in owned_elsewhere:
			kept.append(f"{doctype}.{fieldname}")
			continue
		name = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname})
		if not name:
			continue
		frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
		removed.append(f"{doctype}.{fieldname}")

	if kept:
		print(f"ecommerce_integration uninstall: left {len(kept)} field(s) owned by other apps: {kept}")
	return removed


def remove_scheduled_jobs():
	"""Drop Scheduled Job Types pointing at this app's methods.

	Frappe prunes jobs declared in `scheduler_events`, but this app also creates
	them at runtime (the per-channel frequency settings), and those are plain
	records that would otherwise keep firing against missing code.
	"""
	names = frappe.get_all(
		"Scheduled Job Type",
		filters={"method": ["like", "ecommerce_integration.%"]},
		pluck="name",
	)
	for name in names:
		frappe.delete_doc("Scheduled Job Type", name, ignore_permissions=True, force=True)
	return names


def before_uninstall():
	fields = remove_custom_fields()
	jobs = remove_scheduled_jobs()
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	print(
		f"ecommerce_integration uninstall: removed {len(fields)} custom field(s), "
		f"{len(jobs)} scheduled job(s)"
	)
