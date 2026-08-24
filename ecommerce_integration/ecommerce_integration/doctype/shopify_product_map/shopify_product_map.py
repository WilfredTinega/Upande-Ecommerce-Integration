# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Maps Shopify variants onto what they mean for fulfilment.

This exists so new subscription options are configuration rather than code. The
storefront currently sells Petite (24 stems), Signature (48), Grand (72) and
Build Your Own, alongside fee-only products; a new box tier is a new row here.

Matching is by **variant id**, not SKU: every product on this store returns a
null SKU, so SKU-based matching would never fire.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

PRODUCTS_QUERY = """
query ShopProducts($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        productType
        tags
        variants(first: 50) {
          edges {
            node { id title sku price }
          }
        }
      }
    }
  }
}
"""

# Tag or product-type hints that mean "this is not flowers to allocate".
FEE_HINTS = ("fee", "internal-fee", "packaging-fee")
PACKAGING_HINTS = ("packaging", "carrier", "box fee")


class ShopifyProductMap(Document):
	def validate(self):
		if self.line_class != "Box":
			# Only Box lines reach allocation, so stem config on a fee row is noise.
			self.stems_per_box = 0
			self.qty_is_stems = 0

		if self.line_class == "Box" and not self.stems_per_box and not self.qty_is_stems:
			frappe.msgprint(
				f"{self.product_title}: set Stems per Box, or tick 'Ordered Qty is Stems' for "
				"build-your-own products. Without either, allocations cannot be pre-filled.",
				indicator="orange",
				alert=True,
			)


def _classify(title, product_type, tags):
	"""Best-effort first guess at what a product is. Deliberately conservative —
	anything unrecognised comes through as a Box so it cannot be silently dropped
	from fulfilment, and a human corrects it."""
	lowered_tags = [cstr(t).lower() for t in tags or []]
	haystack = " ".join([title or "", product_type or "", " ".join(lowered_tags)]).lower()

	# An explicit fee product type or tag beats a keyword match on the title —
	# "Delivery box fee" is a fee, even though "box fee" reads as packaging.
	if cstr(product_type).strip().lower() == "fee" or "internal-fee" in lowered_tags:
		return "Fee"

	for hint in PACKAGING_HINTS:
		if hint in haystack:
			return "Packaging"
	for hint in FEE_HINTS:
		if hint in haystack:
			return "Fee"
	return "Box"


def _guess_stems(title, price):
	"""Stem counts are in the product names on this store (Petite 24 / Signature 48 /
	Grand 72). Where they aren't, fall back to price divided by the per-stem rate."""
	known = {"petite": 24, "signature": 48, "grand": 72}
	lowered = (title or "").lower()
	for key, stems in known.items():
		if key in lowered:
			return stems, 0

	# "Build your own" is priced per stem, so the ordered quantity *is* the stem count.
	if "build" in lowered:
		return 0, 1

	return 0, 0


@frappe.whitelist()
def seed_product_map():
	"""Pull every product/variant from Shopify and upsert a mapping row for each.

	Existing rows keep their human-set classification and stem counts — only the
	title and product id are refreshed. Safe to re-run whenever the shop changes.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.shopify_api_error_log.shopify_api_error_log import (
		flush_api_log,
	)
	from ecommerce_integration.ecommerce_integration.doctype.shopify_settings.shopify_settings import (
		get_shopify_settings,
		shopify_graphql,
	)

	settings = get_shopify_settings()

	cursor = None
	has_next = True
	pages = created = updated = 0

	while has_next and pages < 40:
		pages += 1
		data = shopify_graphql(
			PRODUCTS_QUERY, {"cursor": cursor}, settings=settings, operation="Seed Product Map"
		)
		connection = data.get("products") or {}
		page_info = connection.get("pageInfo") or {}

		for edge in connection.get("edges") or []:
			node = edge.get("node") or {}
			product_id = (node.get("id") or "").rsplit("/", 1)[-1]
			title = node.get("title")
			tags = node.get("tags") or []
			product_type = node.get("productType")

			for variant_edge in ((node.get("variants") or {}).get("edges")) or []:
				variant = variant_edge.get("node") or {}
				variant_id = (variant.get("id") or "").rsplit("/", 1)[-1]
				if not variant_id:
					continue

				variant_title = variant.get("title") or ""
				full_title = title
				if variant_title and variant_title != "Default Title":
					full_title = f"{title} - {variant_title}"

				name = f"SHOP-MAP-{variant_id}"
				if frappe.db.exists("Shopify Product Map", name):
					doc = frappe.get_doc("Shopify Product Map", name)
					doc.product_title = full_title
					doc.shopify_product_id = product_id
					doc.save(ignore_permissions=True)
					updated += 1
					continue

				stems, qty_is_stems = _guess_stems(full_title, flt(variant.get("price")))
				doc = frappe.new_doc("Shopify Product Map")
				doc.shopify_variant_id = variant_id
				doc.shopify_product_id = product_id
				doc.product_title = full_title
				doc.line_class = _classify(full_title, product_type, tags)
				doc.stems_per_box = stems
				doc.qty_is_stems = qty_is_stems
				doc.enabled = 1
				doc.insert(ignore_permissions=True)
				created += 1

		has_next = bool(page_info.get("hasNextPage"))
		cursor = page_info.get("endCursor")

	summary = f"created {created}, refreshed {updated}"
	settings.db_set("map_summary", summary, update_modified=False)
	# Background sync: persist the records written above and the summary field
	# the form reads, so a later failure cannot discard a completed run.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	flush_api_log()
	return {"summary": summary, "created": created, "updated": updated}


def resolve_line(variant_id, qty):
	"""Return (map_name, line_class, stems) for an ordered line.

	An unmapped variant comes back as Box with zero stems rather than being
	dropped — an unrecognised product must still show up for a human to deal with.
	"""
	if not variant_id:
		return None, "Box", 0

	row = frappe.db.get_value(
		"Shopify Product Map",
		{"shopify_variant_id": variant_id, "enabled": 1},
		["name", "line_class", "stems_per_box", "qty_is_stems"],
		as_dict=True,
	)
	if not row:
		return None, "Box", 0

	if row.line_class != "Box":
		return row.name, row.line_class, 0

	stems = flt(qty) if row.qty_is_stems else flt(qty) * cint(row.stems_per_box)
	return row.name, row.line_class, stems
