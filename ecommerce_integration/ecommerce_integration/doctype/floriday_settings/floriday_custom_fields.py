# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

# Specs the integration relies on, keyed by "<DocType>::<fieldname>"; each `df`
# is passed straight to create_custom_field(dt, df).
FLORIDAY_CUSTOM_FIELDS = [
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_delivery_point",
			"label": "Delivery Point",
			"fieldtype": "Link",
			"options": "Delivery Point",
			"insert_after": "delivery_date",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_floriday_delivery_id",
			"label": "Floriday Delivery ID",
			"fieldtype": "Data",
			"insert_after": "custom_delivery_point",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			# Stamped once the fulfillment order is accepted by Floriday, and read
			# back as the "already fulfilled" guard so a resubmit or a rerun of the
			# bulk job never posts a second fulfillment for the same order.
			"fieldname": "custom_floriday_fulfillment_order_id",
			"label": "Floriday Fulfillment Order ID",
			"fieldtype": "Data",
			"read_only": 1,
			"no_copy": 1,
			# Written after the Sales Order is submitted, so it has to be writable
			# on a submitted document.
			"allow_on_submit": 1,
			"insert_after": "custom_floriday_delivery_id",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_sales_order_type",
			"label": "Sales Order Type",
			"fieldtype": "Select",
			"options": "\nRoses\nYoghurt\nMilk\nPoultry\nAvocados\nCoffee\nShopify Roses",
			"insert_after": "naming_series",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_order_name",
			"label": "Order Name",
			"fieldtype": "Data",
			"insert_after": "custom_sales_order_type",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_business_unit",
			"label": "Business Unit",
			"fieldtype": "Link",
			"options": "Business Unit",
			"insert_after": "delivery_date",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_farm",
			"label": "Farm",
			"fieldtype": "Link",
			"options": "Farm",
			"insert_after": "company",
		},
	},
	{
		"dt": "Sales Order",
		"df": {
			"fieldname": "custom_ordered_stems",
			"label": "Ordered Stems",
			"fieldtype": "Float",
			"insert_after": "custom_order_name",
		},
	},
	{
		"dt": "Sales Order Item",
		"df": {
			"fieldname": "custom_ordered_quantity",
			"label": "Ordered Stems",
			"fieldtype": "Float",
			"insert_after": "stock_reserved_qty",
		},
	},
	{
		"dt": "Sales Order Item",
		"df": {
			"fieldname": "custom_source_warehouse",
			"label": "Source Warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"insert_after": "warehouse",
		},
	},
	{
		"dt": "Customer",
		"df": {
			"fieldname": "custom_floriday_id",
			"label": "Floriday ID",
			"fieldtype": "Data",
			"insert_after": "customer_name",
		},
	},
	{
		# The Floriday GLN of this drop-off point. Each Delivery Point carries the
		# id it maps to, so an order for that GLN reuses the same record instead of
		# creating another one.
		"dt": "Delivery Point",
		"df": {
			"fieldname": "custom_floriday_delivery_point_id",
			"label": "Floriday Delivery Point ID",
			"fieldtype": "Data",
			"unique": 1,
			"insert_after": "delivery_point",
		},
	},
	{
		"dt": "Delivery Point",
		"df": {
			"fieldname": "custom_delivery_address",
			"label": "Delivery Address",
			"fieldtype": "Data",
			"insert_after": "custom_floriday_delivery_point_id",
		},
	},
	{
		"dt": "Delivery Point",
		"df": {
			"fieldname": "custom_delivery_city",
			"label": "Delivery City",
			"fieldtype": "Data",
			"insert_after": "custom_delivery_address",
		},
	},
	{
		"dt": "Delivery Point",
		"df": {
			"fieldname": "custom_delivery_country",
			"label": "Delivery Country",
			"fieldtype": "Data",
			"insert_after": "custom_delivery_city",
		},
	},
	{
		"dt": "Delivery Point",
		"df": {
			"fieldname": "custom_delivery_postal_code",
			"label": "Delivery Postal Code",
			"fieldtype": "Data",
			"insert_after": "custom_delivery_country",
		},
	},
	{
		"dt": "Warehouse",
		"df": {
			"fieldname": "custom_farm",
			"label": "Farm",
			"fieldtype": "Link",
			"options": "Farm",
			"insert_after": "warehouse_name",
		},
	},
	# Stem-length columns: present on custom-field-model sites, intentionally
	# absent on variant sites where stem length is in the variant item_code.
	{
		"dt": "Stock Entry Detail",
		"df": {
			"fieldname": "custom_stem_length",
			"label": "Stem Length",
			"fieldtype": "Link",
			"options": "Stem Length",
			"insert_after": "uom",
		},
		"optional": True,
	},
	{
		"dt": "Stock Ledger Entry",
		"df": {
			"fieldname": "custom_stem_length",
			"label": "Stem Length",
			"fieldtype": "Link",
			"options": "Stem Length",
			"insert_after": "outgoing_rate",
		},
		"optional": True,
	},
]


def _field_id(dt, fieldname):
	return f"{dt}::{fieldname}"


def _has_field(dt, fieldname):
	"""Whether the field exists on dt (via has_column, so it reflects the real
	table). Returns None when dt itself doesn't exist on this site."""
	if not frappe.db.exists("DocType", dt):
		return None
	try:
		return frappe.db.has_column(dt, fieldname)
	except Exception:
		return False


def _missing_link_target(df):
	"""The Link/Table target of `df`, when that DocType is not on this site.

	Fields are created with `ignore_validate=True`, so nothing stops a Link whose
	target DocType is missing from being created dangling — the control renders
	broken, and Frappe's test-record dependency walk aborts on it ("DocType
	<target> not found"). Cross-app targets such as "Business Unit" / "Farm"
	(upande_core) or "Consignee" (upande_packhouse) are simply absent on sites
	that don't run those apps, so the field is skipped there instead.
	"""
	if df.get("fieldtype") not in ("Link", "Table", "Table MultiSelect"):
		return None

	target = (df.get("options") or "").strip()
	if not target or target == "[Select]":
		return None

	return None if frappe.db.exists("DocType", target) else target


@frappe.whitelist()
def check_floriday_custom_fields():
	"""Report presence of every expected field as a list of per-field dicts."""
	out = []
	for spec in FLORIDAY_CUSTOM_FIELDS:
		dt = spec["dt"]
		df = spec["df"]
		fieldname = df["fieldname"]
		present = _has_field(dt, fieldname)
		out.append(
			{
				"id": _field_id(dt, fieldname),
				"dt": dt,
				"fieldname": fieldname,
				"label": df.get("label") or fieldname,
				"fieldtype": df.get("fieldtype"),
				"options": df.get("options"),
				"present": bool(present),
				"doctype_missing": present is None,
				"link_target_missing": _missing_link_target(df),
				"optional": bool(spec.get("optional")),
			}
		)
	return out


@frappe.whitelist()
def create_missing_floriday_custom_fields(field_ids: str | list | None = None):
	"""Create the missing expected fields. `field_ids` is an optional JSON list /
	comma string of "<DocType>::<fieldname>" ids; omit it to create all missing."""
	import json

	if isinstance(field_ids, str):
		field_ids = field_ids.strip()
		if field_ids.startswith("["):
			field_ids = json.loads(field_ids)
		elif field_ids:
			field_ids = [s.strip() for s in field_ids.split(",") if s.strip()]
		else:
			field_ids = None

	wanted = set(field_ids) if field_ids else None

	created, skipped, errors = [], [], []
	for spec in FLORIDAY_CUSTOM_FIELDS:
		dt = spec["dt"]
		df = spec["df"]
		fid = _field_id(dt, df["fieldname"])

		if wanted is not None and fid not in wanted:
			continue

		if not frappe.db.exists("DocType", dt):
			errors.append({"id": fid, "error": f"DocType '{dt}' not found on this site"})
			continue

		if frappe.db.has_column(dt, df["fieldname"]):
			skipped.append({"id": fid, "reason": "already present"})
			continue

		link_target = _missing_link_target(df)
		if link_target:
			skipped.append({"id": fid, "reason": f"link target DocType '{link_target}' not on this site"})
			continue

		try:
			create_custom_field(dt, dict(df), ignore_validate=True)
			created.append({"id": fid})
		except Exception as e:
			errors.append({"id": fid, "error": str(e)[:200]})

	if created:
		frappe.clear_cache()
		# Custom fields are schema; commit so the cache clear above and the new
		# columns are visible to the form that reloads right after this response.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

	return {
		"status": "success",
		"created": created,
		"skipped": skipped,
		"errors": errors,
		"summary": {
			"created": len(created),
			"skipped": len(skipped),
			"errors": len(errors),
		},
	}


def ensure_floriday_custom_fields():
	"""Create any missing Floriday custom fields. Safe to run on every migrate."""
	return create_missing_floriday_custom_fields()
