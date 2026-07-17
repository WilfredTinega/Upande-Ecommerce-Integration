# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field


# Custom fields the Biflorica deal/predeal -> Sales Order flow relies on, keyed by
# "<DocType>::<fieldname>". Each `df` is passed to create_custom_field(dt, df).
#
# Fields marked optional are shared with the Karen Roses / Floriday setup; on
# sites that already own them the creator skips them, and the Biflorica code
# guards every field with has_field, so a missing optional field never breaks
# the flow — it just leaves that column blank.
BIFLORICA_CUSTOM_FIELDS = [
    # --- Biflorica-owned (core to the integration) ---
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_is_preorder",
            "label": "Is Preorder",
            "fieldtype": "Check",
            "insert_after": "custom_sales_order_type",
            "read_only": 1,
            "no_copy": 1,
            "description": "Set on Sales Orders created from Biflorica predeals (preorders).",
        },
    },
    {
        "dt": "Sales Order Item",
        "df": {
            "fieldname": "custom_box_label",
            "label": "Box Label",
            "fieldtype": "Data",
            "insert_after": "custom_number_of_boxes",
            "description": "Biflorica buyer code used as the box label.",
        },
    },
    # --- Shared with Karen Roses / Floriday (optional; created if absent) ---
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_sales_order_type",
            "label": "Sales Order Type",
            "fieldtype": "Select",
            "options": "\nRoses\nYoghurt\nMilk\nPoultry\nAvocados\nCoffee\nShopify Roses",
            "insert_after": "naming_series",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_business_unit",
            "label": "Business Unit",
            "fieldtype": "Link",
            "options": "Business Unit",
            "insert_after": "company",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_farm",
            "label": "Farm",
            "fieldtype": "Link",
            "options": "Farm",
            "insert_after": "custom_business_unit",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_order_name",
            "label": "Order Name",
            "fieldtype": "Data",
            "insert_after": "custom_sales_order_type",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_ordered_stems",
            "label": "Ordered Stems",
            "fieldtype": "Float",
            "insert_after": "custom_order_name",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_expected_delivery_date",
            "label": "Expected Delivery Date",
            "fieldtype": "Date",
            "insert_after": "delivery_date",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_week",
            "label": "Week",
            "fieldtype": "Data",
            "insert_after": "custom_expected_delivery_date",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_mode_of_transport",
            "label": "Mode of Transport",
            "fieldtype": "Select",
            "options": "Air\nSea Freight",
            "insert_after": "custom_week",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_statescountry",
            "label": "Country",
            "fieldtype": "Data",
            "insert_after": "custom_mode_of_transport",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_consignee",
            "label": "Consignee",
            "fieldtype": "Link",
            "options": "Consignee",
            "insert_after": "custom_statescountry",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_consignee_country",
            "label": "Consignee Country",
            "fieldtype": "Data",
            "insert_after": "custom_consignee",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order",
        "df": {
            "fieldname": "custom_delivery_point",
            "label": "Delivery Point",
            "fieldtype": "Link",
            "options": "Delivery Point",
            "insert_after": "delivery_date",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order Item",
        "df": {
            "fieldname": "custom_length",
            "label": "Stem Length",
            "fieldtype": "Link",
            "options": "Stem Length",
            "insert_after": "uom",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order Item",
        "df": {
            "fieldname": "custom_packrate",
            "label": "Packrate",
            "fieldtype": "Link",
            "options": "Packrate",
            "insert_after": "custom_length",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order Item",
        "df": {
            "fieldname": "custom_number_of_boxes",
            "label": "Number of Boxes",
            "fieldtype": "Int",
            "insert_after": "qty",
        },
        "optional": True,
    },
    {
        "dt": "Sales Order Item",
        "df": {
            "fieldname": "custom_ordered_quantity",
            "label": "Ordered Stems",
            "fieldtype": "Float",
            "insert_after": "stock_qty",
        },
        "optional": True,
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
        "optional": True,
    },
]


def _field_id(dt, fieldname):
    return f"{dt}::{fieldname}"


def _has_field(dt, fieldname):
    if not frappe.db.exists("DocType", dt):
        return None
    try:
        return frappe.db.has_column(dt, fieldname)
    except Exception:
        return False


@frappe.whitelist()
def check_biflorica_custom_fields():
    """Report presence of every expected field as a list of per-field dicts."""
    out = []
    for spec in BIFLORICA_CUSTOM_FIELDS:
        dt = spec["dt"]
        df = spec["df"]
        fieldname = df["fieldname"]
        present = _has_field(dt, fieldname)
        out.append({
            "id": _field_id(dt, fieldname),
            "dt": dt,
            "fieldname": fieldname,
            "label": df.get("label") or fieldname,
            "fieldtype": df.get("fieldtype"),
            "options": df.get("options"),
            "present": bool(present),
            "doctype_missing": present is None,
            "optional": bool(spec.get("optional")),
        })
    return out


@frappe.whitelist()
def create_missing_biflorica_custom_fields(field_ids=None):
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
    for spec in BIFLORICA_CUSTOM_FIELDS:
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

        try:
            create_custom_field(dt, dict(df), ignore_validate=True)
            created.append({"id": fid})
        except Exception as e:
            errors.append({"id": fid, "error": str(e)[:200]})

    if created:
        frappe.clear_cache()
        frappe.db.commit()

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


def ensure_biflorica_custom_fields():
    """Create any missing Biflorica custom fields. Safe to run on every migrate."""
    return create_missing_biflorica_custom_fields()
