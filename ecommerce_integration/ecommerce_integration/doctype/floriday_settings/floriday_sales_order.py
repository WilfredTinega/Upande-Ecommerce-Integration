import frappe
import requests
import json
from datetime import datetime, timedelta, timezone

from ecommerce_integration.ecommerce_integration.utils import create_orders_as_quotation

_logger = frappe.logger("floriday", allow_site=True)


def _floriday_setting(fieldname):
    """Read a value off the Floriday Settings single. Returns None if unset."""
    return frappe.db.get_single_value("Floriday Settings", fieldname) or None


def _floriday_order_target_doctype():
    """The doctype Floriday imports create — "Quotation" when the webshop is set
    to create orders as Quotations, otherwise "Sales Order"."""
    return "Quotation" if create_orders_as_quotation() else "Sales Order"


def _floriday_order_exists(floriday_order_id):
    """True if this Floriday order was already imported. Checks both a live
    Sales Order and a draft Quotation (by po_no), so the importer stays
    idempotent regardless of which doctype the site creates. Cancelled docs
    (docstatus 2) don't count, so a cancelled import can be re-created.

    po_no is a standard Sales Order field; on Quotation it must be added as a
    custom field for de-duplication to work in Quotation mode (guarded here so
    the check is simply skipped where the column is absent)."""
    if not floriday_order_id:
        return False
    for dt in ("Sales Order", "Quotation"):
        if not frappe.db.has_column(dt, "po_no"):
            continue
        if frappe.db.exists(dt, {"po_no": floriday_order_id, "docstatus": ["!=", 2]}):
            return True
    return False

def log_short(msg, title="Floriday", is_error=True):
    """Log errors and successful sales order creations"""
    if len(msg) > 135:
        msg = msg[:132] + "..."
    if is_error:
        frappe.log_error(msg, title)
    else:
        _logger.info(f"[{title}] {msg}")

def generate_custom_order_name(customer_name):
    """Generate a sequential per-customer order name: CustomerName-XXX."""
    try:
        clean_name = ''.join(c for c in customer_name if c.isalnum() or c == ' ').strip()
        clean_name = clean_name.replace(' ', '-')[:20]

        latest_order = frappe.db.sql("""
            SELECT custom_order_name
            FROM `tabSales Order`
            WHERE customer = %s
            AND custom_order_name LIKE %s
            AND docstatus < 2
            ORDER BY creation DESC
            LIMIT 1
        """, (customer_name, f"{clean_name}-%"), as_dict=True)

        if latest_order and latest_order[0].custom_order_name:
            try:
                last_number = int(latest_order[0].custom_order_name.split('-')[-1])
                new_number = last_number + 1
            except (ValueError, IndexError):
                new_number = 1
        else:
            new_number = 1

        return f"{clean_name}-{new_number:03d}"

    except Exception as e:
        log_short(f"Order name error: {str(e)[:30]}", "Floriday Order Name Error", True)
        from frappe.utils import now_datetime
        timestamp = now_datetime().strftime("%Y%m%d%H%M%S")
        return f"ORD-{timestamp}"

def get_delivery_point_from_floriday_gln(gln_code):
    """
    Maps a Floriday GLN code to an ERPNext Delivery Point using custom_floriday_delivery_id.
    Returns the Delivery Point name if found, otherwise None.
    """
    if not gln_code:
        return None
    try:
        return frappe.db.get_value(
            "Delivery Point",
            {"custom_floriday_delivery_id": gln_code},
            "name",
        )
    except Exception as e:
        log_short(f"GLN lookup error {gln_code}: {str(e)[:50]}", "Floriday Delivery Point Lookup", True)
        return None


def fetch_floriday_delivery_location_by_gln(gln_code, settings):
    """
    Fetch a DeliveryLocation from Floriday matching the given GLN by paging through
    /delivery-locations/sync/{sequenceNumber}. Returns the matching location dict or None.
    Logs a diagnostic so we can tell *why* a lookup failed.
    """
    if not (gln_code and settings):
        return None
    try:
        base_url = (settings.base_url or "").rstrip('/')
        if not (base_url and settings.api_key and settings.access_token):
            log_short(f"GLN {gln_code}: settings incomplete (base_url/api_key/token)",
                      "Floriday Delivery Location Lookup", True)
            return None

        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "X-Api-Key": settings.api_key,
            "Accept": "application/json",
        }

        seq = 0
        scanned = 0
        pages = 0
        deleted_match = None
        max_seq_seen = -1
        last_api_max = None
        # Safety cap: 50 pages of 1000 = 50k locations. Plenty for a single supplier.
        for _ in range(50):
            pages += 1
            r = requests.get(
                f"{base_url}/delivery-locations/sync/{seq}",
                headers=headers,
                timeout=30,
            )
            if r.status_code != 200:
                log_short(f"Delivery-locations sync HTTP {r.status_code} for GLN {gln_code}",
                          "Floriday Delivery Location Lookup", True)
                return None

            payload = r.json() or {}
            # The response is SyncResultOfDeliveryLocation: { results: [...], maximumSequenceNumber }
            # Be defensive in case the shape varies between API versions or environments.
            if isinstance(payload, dict):
                batch = payload.get("results") or []
                api_max_seq = payload.get("maximumSequenceNumber")
                last_api_max = api_max_seq
            elif isinstance(payload, list):
                batch = payload
                api_max_seq = None
            else:
                batch = []
                api_max_seq = None

            if not batch:
                break

            scanned += len(batch)
            for loc in batch:
                if not isinstance(loc, dict):
                    continue
                if loc.get("gln") == gln_code:
                    if loc.get("isDeleted"):
                        deleted_match = loc
                    else:
                        return loc
                seq_num = loc.get("sequenceNumber")
                if isinstance(seq_num, int) and seq_num > max_seq_seen:
                    max_seq_seen = seq_num

            # Stop once we've reached the API's reported maximum.
            if isinstance(api_max_seq, int) and max_seq_seen >= api_max_seq:
                break

            # Advance to next sequence-number window.
            if max_seq_seen < seq:
                # No progress made — avoid an infinite loop.
                break
            seq = max_seq_seen + 1

        if deleted_match:
            log_short(
                f"GLN {gln_code} deleted in /delivery-locations (pages={pages}, scanned={scanned}, apiMaxSeq={last_api_max}, seenMax={max_seq_seen})",
                "Floriday Delivery Location Lookup", True,
            )
        else:
            log_short(
                f"GLN {gln_code} not in /delivery-locations (pages={pages}, scanned={scanned}, apiMaxSeq={last_api_max}, seenMax={max_seq_seen})",
                "Floriday Delivery Location Lookup", True,
            )
        return None
    except Exception as e:
        log_short(f"Delivery-location fetch error: {str(e)[:50]}",
                  "Floriday Delivery Location Lookup", True)
        return None


def fetch_floriday_organization_by_gln(gln_code, settings):
    """
    Fetch an Organization from Floriday by GLN via GET /organizations?companyGln={gln}.
    Used as a fallback when the GLN doesn't match any DeliveryLocation — sometimes the
    order's delivery.location.gln is actually the customer organization's companyGln.
    Returns the org dict or None.
    """
    if not (gln_code and settings):
        return None
    try:
        base_url = (settings.base_url or "").rstrip('/')
        if not (base_url and settings.api_key and settings.access_token):
            return None
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "X-Api-Key": settings.api_key,
            "Accept": "application/json",
        }
        r = requests.get(
            f"{base_url}/organizations",
            headers=headers,
            params={"companyGln": gln_code},
            timeout=15,
        )
        if r.status_code != 200:
            log_short(f"Organizations by GLN {gln_code}: HTTP {r.status_code}",
                      "Floriday Organization Lookup", True)
            return None
        payload = r.json()
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        log_short(f"Organization-by-GLN fetch error: {str(e)[:50]}",
                  "Floriday Organization Lookup", True)
        return None


def ensure_delivery_point_for_gln(gln_code, settings, address_fallback=None):
    """
    Ensure a Delivery Point exists for the given Floriday GLN.
    - If one already exists with custom_floriday_delivery_id == gln_code, return its name.
    - Otherwise fetch the DeliveryLocation from Floriday and create a Delivery Point
      using the data the API returns (name + address). Returns None if the GLN
      cannot be resolved (no GLN, no Floriday match, and no fallback name).

    address_fallback is a dict of {addressLine, city, countryCode, postalCode} extracted
    from the sales order's delivery.location.address — used only when Floriday's
    /delivery-locations does not return a match.
    """
    if not gln_code:
        return None

    has_gln_field = frappe.db.has_column("Delivery Point", "custom_floriday_delivery_id")

    def _ensure_gln_tagged(dp_name):
        """Make sure the Delivery Point's custom_floriday_delivery_id matches gln_code."""
        if not (has_gln_field and dp_name):
            return
        current = frappe.db.get_value("Delivery Point", dp_name, "custom_floriday_delivery_id")
        if current != gln_code:
            frappe.db.set_value("Delivery Point", dp_name, "custom_floriday_delivery_id", gln_code)
            log_short(f"Tagged Delivery Point '{dp_name}' with GLN {gln_code}",
                      "Floriday Delivery Point Tagged", False)

    # 1. Already mapped via custom_floriday_delivery_id?
    existing = get_delivery_point_from_floriday_gln(gln_code)
    if existing:
        # Defensive: ensure the field value still matches (no-op if already correct)
        _ensure_gln_tagged(existing)
        return existing

    # 2. Look up the location in Floriday's /delivery-locations
    location = fetch_floriday_delivery_location_by_gln(gln_code, settings)

    # 3. Determine the Delivery Point name and address
    address = None
    if location:
        dp_name = (location.get("name") or "").strip()
        address = location.get("address") or None
    else:
        dp_name = ""

    # 3b. If /delivery-locations didn't yield a name, try /organizations?companyGln=...
    # — sometimes the order's delivery.location.gln is the customer organization's GLN.
    name_source = "delivery_location" if dp_name else None
    if not dp_name:
        org = fetch_floriday_organization_by_gln(gln_code, settings)
        if org:
            dp_name = (org.get("commercialName") or org.get("name") or "").strip()
            if dp_name:
                name_source = "organization_by_gln"
            if not address:
                # Organization has mailingAddress / physicalAddress; prefer physical.
                phys = org.get("physicalAddress") or org.get("mailingAddress") or None
                if phys:
                    address = phys
        else:
            log_short(f"GLN {gln_code} not found in /organizations either",
                      "Floriday Organization Lookup", True)

    if not dp_name:
        # Fall back to the order payload itself: address line, then city.
        if address_fallback and address_fallback.get("addressLine"):
            dp_name = address_fallback["addressLine"].strip()
            name_source = "order_address_line"
        elif address_fallback and address_fallback.get("city"):
            dp_name = address_fallback["city"].strip()
            name_source = "order_city"
        if not address and address_fallback:
            address = address_fallback

    # Final fallback: use the existing JKIA Delivery Point if it exists.
    # Do NOT tag it with the GLN — JKIA is a shared catch-all, tagging would corrupt
    # the GLN→Delivery Point mapping for whatever JKIA legitimately represents.
    if not dp_name:
        if frappe.db.exists("Delivery Point", "JKIA"):
            log_short(f"GLN {gln_code} unresolved — falling back to JKIA Delivery Point",
                      "Floriday Delivery Point Resolution", False)
            return "JKIA"
        log_short(f"GLN {gln_code} unresolved and JKIA Delivery Point does not exist",
                  "Floriday Delivery Point Resolution", True)
        return None

    log_short(f"GLN {gln_code} resolved name '{dp_name}' from {name_source}",
              "Floriday Delivery Point Resolution", False)

    # Truncate to the Delivery Point field's max length (Data field default 140)
    dp_name = dp_name[:140]

    # If a Delivery Point with this name already exists but isn't tagged with the GLN
    # (or has a stale GLN), tag it and reuse instead of creating a duplicate.
    if frappe.db.exists("Delivery Point", dp_name):
        _ensure_gln_tagged(dp_name)
        return dp_name

    # 4. Create the Delivery Point
    try:
        doc = frappe.new_doc("Delivery Point")
        doc.delivery_point = dp_name

        if has_gln_field:
            doc.custom_floriday_delivery_id = gln_code

        if address:
            for src, target in (
                ("addressLine", "custom_delivery_address"),
                ("city", "custom_delivery_city"),
                ("countryCode", "custom_delivery_country"),
                ("postalCode", "custom_delivery_postal_code"),
            ):
                value = address.get(src)
                if value and frappe.db.has_column("Delivery Point", target):
                    doc.set(target, value)

        doc.insert(ignore_permissions=True)
        log_short(f"Created Delivery Point '{dp_name}' for GLN {gln_code}",
                  "Floriday Delivery Point Created", False)
        # Defensive: confirm the GLN landed (in case of stripping or before_insert hooks)
        _ensure_gln_tagged(doc.name)
        return doc.name
    except frappe.exceptions.DuplicateEntryError:
        frappe.db.rollback()
        if frappe.db.exists("Delivery Point", dp_name):
            _ensure_gln_tagged(dp_name)
            return dp_name
        return None
    except Exception as e:
        log_short(f"Delivery Point create error for GLN {gln_code}: {str(e)[:50]}",
                  "Floriday Delivery Point Create Error", True)
        return None


def get_item_sales_uom_and_factor(item_code):
    """
    Return (sales_uom, conversion_factor) from the Item's master data.
    Raises if either is missing.
    """
    item = frappe.get_cached_doc("Item", item_code)
    sales_uom = item.sales_uom
    if not sales_uom:
        raise Exception(f"Item {item_code} has no Default Sales UOM (sales_uom) configured")
    for row in (item.uoms or []):
        if row.uom == sales_uom:
            return sales_uom, row.conversion_factor
    raise Exception(
        f"Item {item_code} has sales_uom={sales_uom} but no matching row in its UOM Conversion table"
    )


def _force_floriday_amounts_in_db(sales_order):
    """
    The site's calculate_taxes_and_totals override forces amount = rate × stock_qty
    for every Sales Order. For Floriday-sourced orders we want amount = rate × qty
    (where qty is in bunches and rate is per bunch).

    This helper writes the correct per-line amounts and order-level totals directly
    to the DB via frappe.db.set_value, bypassing the override entirely. Must be
    called AFTER any save/submit operation that would re-run the override.
    """
    conversion_rate = sales_order.conversion_rate or 1.0
    total = 0.0
    base_total = 0.0

    for item in sales_order.items:
        rate = float(item.rate or 0)
        qty = float(item.qty or 0)
        amount = round(rate * qty, item.precision("amount"))
        base_amount = round(amount * conversion_rate, item.precision("base_amount"))

        frappe.db.set_value(
            "Sales Order Item",
            item.name,
            {
                "amount": amount,
                "net_amount": amount,
                "base_amount": base_amount,
                "base_net_amount": base_amount,
            },
            update_modified=False,
        )
        item.amount = amount
        item.net_amount = amount
        item.base_amount = base_amount
        item.base_net_amount = base_amount

        total += amount
        base_total += base_amount

    total = round(total, sales_order.precision("total"))
    base_total = round(base_total, sales_order.precision("base_total"))

    frappe.db.set_value(
        "Sales Order",
        sales_order.name,
        {
            "total": total,
            "net_total": total,
            "base_total": base_total,
            "base_net_total": base_total,
            "grand_total": total,
            "base_grand_total": base_total,
            "rounded_total": total,
            "base_rounded_total": base_total,
            "amount_eligible_for_commission": base_total,
        },
        update_modified=False,
    )

    sales_order.total = total
    sales_order.net_total = total
    sales_order.base_total = base_total
    sales_order.base_net_total = base_total
    sales_order.grand_total = total
    sales_order.base_grand_total = base_total
    sales_order.rounded_total = total
    sales_order.base_rounded_total = base_total

    # Update payment_schedule rows in DB so they don't display the inflated amount.
    for ps in (sales_order.payment_schedule or []):
        portion = float(ps.invoice_portion or 100) / 100.0
        payment_amount = round(total * portion, ps.precision("payment_amount"))
        base_payment_amount = round(base_total * portion, ps.precision("base_payment_amount"))
        frappe.db.set_value(
            "Payment Schedule",
            ps.name,
            {
                "payment_amount": payment_amount,
                "outstanding": payment_amount,
                "base_payment_amount": base_payment_amount,
                "base_outstanding": base_payment_amount,
            },
            update_modified=False,
        )
        ps.payment_amount = payment_amount
        ps.outstanding = payment_amount
        ps.base_payment_amount = base_payment_amount
        ps.base_outstanding = base_payment_amount


@frappe.whitelist()
def create_sales_orders_from_floriday():
    """Fetch Floriday orders from the last 24 hours and create matching Sales Orders."""
    try:
        settings = frappe.get_single("Floriday Settings")

        API_KEY = settings.api_key
        BASE_URL = settings.base_url.rstrip('/')
        ACCESS_TOKEN = settings.access_token
        SUPPLIER_ORG_ID = settings.organization_supplier_id
        WAREHOUSE = settings.warehouse

        if not all([API_KEY, BASE_URL, ACCESS_TOKEN, SUPPLIER_ORG_ID]):
            frappe.throw("Floriday Settings incomplete")

        if not WAREHOUSE:
            frappe.throw("Warehouse not configured in Floriday Settings")

        # Window goes back `period` hours from now (configurable on Floriday Settings).
        # Fall back to 24h if the field is unset/zero/invalid.
        try:
            period_hours = int(settings.period or 0)
        except (TypeError, ValueError):
            period_hours = 0
        if period_hours <= 0:
            period_hours = 24

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(hours=period_hours)

        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        endpoint = f"{BASE_URL}/sales-orders"

        params = {
            "supplierOrganizationId": SUPPLIER_ORG_ID,
            "pageSize": 100,
            "startDateTime": start_date.isoformat(),
            "endDateTime": end_date.isoformat(),
            "limitResult": 1000
        }

        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            error_msg = f"API failed: {response.status_code}"
            log_short(error_msg, "Floriday Fetch Error", True)
            return {"status": "error", "message": error_msg}

        orders = response.json()

        # Dump the first order's raw JSON to the error log so the payload shape
        # can be inspected when mappings change.
        if orders and len(orders) > 0:
            first_order = orders[0]
            first_order_json = json.dumps(first_order, indent=2, default=str)

            frappe.log_error(
                title="Floriday - First Sales Order JSON",
                message=f"First sales order received from Floriday API:\n\n{first_order_json}\n\n"
                        f"Total orders received: {len(orders)}\n"
                        f"Date range: {start_date.isoformat()} to {end_date.isoformat()}"
            )

            log_short(f"First order ID: {first_order.get('salesOrderId', 'N/A')} - Total orders: {len(orders)}",
                     "Floriday First Order", False)

        if len(orders) >= 1000:
            log_short(f"API returned {len(orders)} orders — pagination limit may be hit", "Floriday Pagination Warning", True)

        if not isinstance(orders, list):
            error_msg = "Invalid response format"
            log_short(error_msg, "Floriday Format Error", True)
            frappe.throw(error_msg)

        results = []
        processed_count = 0
        skipped_count = 0
        error_count = 0
        date_filtered_count = 0
        duplicate_count = 0

        for order in orders:
            order_dt_str = order.get("orderDateTime")
            order_id = order.get('salesOrderId', 'Unknown')[-8:]

            if not order_dt_str:
                skipped_count += 1
                continue

            try:
                order_dt = parse_floriday_datetime(order_dt_str)
            except ValueError:
                skipped_count += 1
                continue

            if not (start_date <= order_dt <= end_date):
                date_filtered_count += 1
                continue

            if order.get("status") != "COMMITTED":
                skipped_count += 1
                continue

            # Skip orders that already exist — silent, no popup, no error log.
            # Cancelled (docstatus=2) docs don't count as duplicates: re-create
            # so the user can replace a previously-cancelled order with a fresh one.
            floriday_sales_order_id = order.get("salesOrderId")
            if _floriday_order_exists(floriday_sales_order_id):
                duplicate_count += 1
                continue

            try:
                sales_order = create_sales_order_from_floriday(order, WAREHOUSE, settings)
                processed_count += 1
                log_short(f"SO {sales_order.name} created for Floriday order {order_id}", "Floriday Success", False)

                results.append({
                    "floriday_order_id": order.get("salesOrderId"),
                    "sales_channel_order_id": order.get("salesChannelOrderId"),
                    "erpnext_sales_order": sales_order.name,
                    "status": "success"
                })

            except Exception as e:
                error_count += 1
                error_short = str(e)[:50] + "..." if len(str(e)) > 50 else str(e)
                log_short(f"Order {order_id}: {error_short}", "Floriday Order Error", True)
                results.append({
                    "floriday_order_id": order.get("salesOrderId"),
                    "status": "error",
                    "error": str(e)
                })

        if error_count > 0 or processed_count == 0:
            log_short(
                f"Sync: P={processed_count}, F={date_filtered_count}, S={skipped_count}, D={duplicate_count}, E={error_count}",
                "Floriday Summary", True,
            )
        else:
            log_short(f"Success: {processed_count} orders created", "Floriday Success", False)

        return {
            "status": "success",
            "results": results,
            "summary": {
                "total_from_api": len(orders),
                "processed": processed_count,
                "date_filtered": date_filtered_count,
                "skipped": skipped_count,
                "duplicates": duplicate_count,
                "errors": error_count,
                "supplier_organization": SUPPLIER_ORG_ID,
                "warehouse": WAREHOUSE
            }
        }

    except Exception as e:
        error_short = str(e)[:100] + "..." if len(str(e)) > 100 else str(e)
        log_short(f"Sync failed: {error_short}", "Floriday Sync Error", True)
        return {"status": "error", "message": str(e)}


def create_sales_order_from_floriday(floriday_order, warehouse, settings=None):
    """Create one ERPNext order document from a Floriday order, resolving the
    delivery GLN to a Delivery Point and writing per-bunch amounts directly.

    Creates a Sales Order by default, or a draft Quotation when the webshop is
    configured to create orders as Quotations (Webshop Settings > "Create Orders
    as Quotation"). In Quotation mode the document is left as a draft for staff
    to review and convert to a Sales Order; all the integration custom fields are
    still stamped where they exist on Quotation (guarded per field)."""
    floriday_order_id = floriday_order.get("salesOrderId")
    if not floriday_order_id:
        frappe.throw("Floriday order missing salesOrderId")

    target_dt = _floriday_order_target_doctype()

    # Treat cancelled (docstatus=2) docs as not existing, so a new one can be
    # created if the previous import was cancelled.
    if _floriday_order_exists(floriday_order_id):
        # Raise plain Exception (not frappe.throw) so it doesn't queue a UI popup.
        # The outer sync loop catches this and counts it.
        raise Exception(f"{target_dt} already exists for Floriday order {floriday_order_id}")

    customer = get_or_create_customer(floriday_order, settings=settings)

    delivery_datetime = parse_floriday_datetime(floriday_order.get("delivery", {}).get("latestDeliveryDateTime"), default=datetime.now(timezone.utc) + timedelta(days=1))
    order_datetime = parse_floriday_datetime(floriday_order.get("orderDateTime"))

    delivery_gln = None
    delivery_address = None
    delivery_city = None
    delivery_country = None
    delivery_postal_code = None

    delivery_info = floriday_order.get("delivery", {})
    if delivery_info:
        location_info = delivery_info.get("location", {})
        if location_info:
            delivery_gln = location_info.get("gln")
            address_info = location_info.get("address", {})
            if address_info:
                delivery_address = address_info.get("addressLine")
                delivery_city = address_info.get("city")
                delivery_country = address_info.get("countryCode")
                delivery_postal_code = address_info.get("postalCode")

    sales_order = frappe.new_doc(target_dt)
    if target_dt == "Quotation":
        # Quotation has no `customer`; it uses quotation_to + party_name.
        sales_order.quotation_to = "Customer"
        sales_order.party_name = customer
    else:
        sales_order.customer = customer
    sales_order.transaction_date = order_datetime.date()
    sales_order.order_type = "Sales"
    # delivery_date / po_no / po_date are Sales Order fields; on Quotation they
    # only apply if added as custom fields (stamped where present).
    if sales_order.meta.has_field("delivery_date"):
        sales_order.delivery_date = delivery_datetime.date()
    if sales_order.meta.has_field("po_no"):
        sales_order.po_no = floriday_order_id
    if sales_order.meta.has_field("po_date"):
        sales_order.po_date = order_datetime.date()

    if sales_order.meta.has_field("custom_sales_order_type"):
        sales_order.custom_sales_order_type = _floriday_setting("sales_order_type")
    if sales_order.meta.has_field("custom_order_name"):
        sales_order.custom_order_name = generate_custom_order_name(customer)

    # Resolve (or auto-create) the Delivery Point from the Floriday GLN.
    # Falls back to the order's own address fields if Floriday doesn't return a name.
    address_fallback = {
        "addressLine": delivery_address,
        "city": delivery_city,
        "countryCode": delivery_country,
        "postalCode": delivery_postal_code,
    } if any([delivery_address, delivery_city, delivery_country, delivery_postal_code]) else None

    delivery_point_name = ensure_delivery_point_for_gln(delivery_gln, settings, address_fallback=address_fallback)

    if delivery_point_name:
        sales_order.custom_delivery_point = delivery_point_name

    # Persist the Floriday delivery GLN directly on the order document.
    # Order fulfillment reads from this field — independent of the Delivery Point,
    # so JKIA fallback orders still get fulfilled with the correct buyer GLN.
    # (On Quotation this only applies where the custom field has been added.)
    if delivery_gln and frappe.db.has_column(target_dt, "custom_floriday_delivery_id"):
        sales_order.custom_floriday_delivery_id = delivery_gln

    # Save address details to their Custom Fields when present on this site.
    if hasattr(sales_order, 'custom_delivery_address') and delivery_address:
        sales_order.custom_delivery_address = delivery_address
    if hasattr(sales_order, 'custom_delivery_city') and delivery_city:
        sales_order.custom_delivery_city = delivery_city
    if hasattr(sales_order, 'custom_delivery_country') and delivery_country:
        sales_order.custom_delivery_country = delivery_country
    if hasattr(sales_order, 'custom_delivery_postal_code') and delivery_postal_code:
        sales_order.custom_delivery_postal_code = delivery_postal_code

    price_info = floriday_order.get("pricePerPiece", {})
    transaction_currency = price_info.get("currency", "EUR")
    sales_order.currency = transaction_currency

    sales_order.notes = f"""Floriday Order: {floriday_order.get("salesOrderId")}
Channel: {floriday_order.get("salesChannel")}
Supplier: {floriday_order.get("supplierOrganizationId")}
Delivery GLN: {delivery_gln if delivery_gln else 'Not provided'}
Delivery Point: {delivery_point_name or 'Not resolved'}"""

    trade_item_id = floriday_order.get("tradeItemId")

    if trade_item_id:
        item_code = get_erpnext_item_code(trade_item_id)
        if item_code:
            number_of_pieces = floriday_order.get("numberOfPieces", 0)

            calculated = floriday_order.get("calculatedFields", {})
            total_price_per_piece = calculated.get("totalPricePerPiece", {}).get("value", price_info.get("value", 0))

            farm, business_unit, _company_from_stock_entry = get_farm_business_unit_company_from_stock_entry(trade_item_id, item_code)

            # The Sales Order company comes from Floriday Settings.company (set to
            # Karen Roses). We do NOT use the stock-entry resolver's company: it can
            # return Kaitet Group (group-level transfers), which then fails the
            # "warehouse does not belong to company" check. If the setting is unset,
            # throw a clear error rather than guessing a company.
            sales_order.company = settings.get("company") if settings else None
            if not sales_order.company:
                frappe.throw("Company not configured in Floriday Settings")

            item_warehouse = warehouse

            if not item_warehouse:
                item_defaults = frappe.get_all(
                    "Item Default",
                    fields=["default_warehouse"],
                    filters={"parent": item_code, "company": sales_order.company}
                )
                if item_defaults and item_defaults[0].default_warehouse:
                    item_warehouse = item_defaults[0].default_warehouse
                else:
                    warehouses = frappe.get_all(
                        "Warehouse",
                        filters={"company": sales_order.company, "is_group": 0},
                        fields=["name"],
                        limit_page_length=1
                    )
                    if warehouses:
                        item_warehouse = warehouses[0].name
                    else:
                        frappe.throw(f"No warehouse found for item {item_code}")

            # Sell in the Item's default sales UOM (e.g. 'Bunch (10)'): qty in bunches,
            # rate per bunch. The site override computes amount = rate × stock_qty
            # (wrong here), so we let it run and overwrite the amounts afterwards via
            # _force_floriday_amounts_in_db().
            sales_uom, conversion_factor = get_item_sales_uom_and_factor(item_code)
            if conversion_factor <= 0:
                raise Exception(
                    f"Item {item_code} has invalid conversion_factor={conversion_factor} for {sales_uom}"
                )
            if number_of_pieces % conversion_factor != 0:
                raise Exception(
                    f"Floriday order has {number_of_pieces} pieces — not divisible by "
                    f"Item {item_code} conversion_factor={conversion_factor} (UOM {sales_uom})"
                )
            qty_in_bunches = number_of_pieces // conversion_factor
            rate_per_bunch = total_price_per_piece * conversion_factor

            item = sales_order.append("items", {})
            item.item_code = item_code
            item.qty = qty_in_bunches
            item.uom = sales_uom
            item.conversion_factor = conversion_factor
            item.rate = rate_per_bunch
            item.delivery_date = delivery_datetime.date()
            item.warehouse = item_warehouse
            item.custom_ordered_quantity = number_of_pieces
            item.custom_source_warehouse = item_warehouse

            # Prefer the farm resolved from the actual source transfer; the
            # configured Default Farm is only a fallback (set below).
            if farm:
                sales_order.custom_farm = farm

    if not sales_order.items:
        frappe.throw(f"No valid items found")

    # custom_sales_order_type / custom_business_unit / custom_order_name / custom_farm
    # are mandatory on this site. All come from Floriday Settings (no hardcoded
    # values): business_unit always from the setting; farm falls back to the
    # configured Default Farm when the source transfer didn't resolve one.
    sales_order.custom_business_unit = _floriday_setting("business_unit")
    if not sales_order.get("custom_farm"):
        sales_order.custom_farm = _floriday_setting("default_farm")

    # Set ordered stems (in stock UOM = stems, not bunches).
    total_ordered_stems = floriday_order.get("numberOfPieces", 0)
    if total_ordered_stems == 0:
        for item in sales_order.items:
            total_ordered_stems += (item.qty or 0) * (item.conversion_factor or 1)

    if hasattr(sales_order, 'custom_ordered_stems'):
        sales_order.custom_ordered_stems = total_ordered_stems

    if sales_order.company:
        company_currency = frappe.get_cached_value('Company', sales_order.company, 'default_currency')

        if transaction_currency != company_currency:
            exchange_rate = get_exchange_rate(transaction_currency, company_currency, order_datetime)
            sales_order.conversion_rate = exchange_rate or 1.0

    sales_order.insert(ignore_permissions=True)

    if target_dt == "Sales Order":
        sales_order.submit()
        # The kaitet Sales Order Item override forces amount = rate × stock_qty
        # during validate/submit. After submit we have docstatus=1 and a stable
        # PK; rewrite the per-line amounts and order totals directly in the DB to
        # the correct rate × qty values. That override doesn't touch Quotation
        # Item, so a draft Quotation already carries the correct rate × qty amount
        # and is left as-is for staff to review and convert.
        _force_floriday_amounts_in_db(sales_order)
    frappe.db.commit()

    log_short(
        f"{target_dt} {sales_order.name} created (Delivery Point: {delivery_point_name or 'unresolved'}, GLN: {delivery_gln or 'none'})",
        "Floriday Order Complete", False,
    )

    return sales_order


def get_farm_business_unit_company_from_stock_entry(trade_item_id, item_code):
    """
    Resolve farm + business_unit + company for a Floriday Sales Order line.

    Rule: walk the most recent submitted Stock Entry for this item where the
    target warehouse is the configured Floriday warehouse (Online Available
    for Sale). Take that transfer's source warehouse and read
    `Warehouse.custom_farm` — that's the farm the stock physically came from.
    Business Unit is always "Roses".
    """
    try:
        floriday_warehouse = frappe.db.get_single_value("Floriday Settings", "warehouse")
        if not floriday_warehouse:
            return None, "Roses", None

        rows = frappe.db.sql(
            """
            SELECT sed.s_warehouse, se.company
            FROM `tabStock Entry Detail` sed
            INNER JOIN `tabStock Entry` se ON se.name = sed.parent
            WHERE sed.item_code = %(item_code)s
              AND sed.docstatus = 1
              AND sed.t_warehouse = %(t_wh)s
              AND sed.s_warehouse IS NOT NULL
              AND sed.s_warehouse != ''
            ORDER BY se.creation DESC
            LIMIT 1
            """,
            {"item_code": item_code, "t_wh": floriday_warehouse},
            as_dict=True,
        )

        farm = None
        company = None
        if rows:
            s_warehouse = rows[0].s_warehouse
            company = rows[0].company
            farm = frappe.db.get_value("Warehouse", s_warehouse, "custom_farm") or None

        return farm, "Roses", company

    except Exception:
        return None, "Roses", None


def get_exchange_rate(from_currency, to_currency, date):
    """Latest Currency Exchange rate on or before `date`, or None."""
    try:
        exchange_rate = frappe.db.sql("""
            SELECT exchange_rate
            FROM `tabCurrency Exchange`
            WHERE from_currency = %s AND to_currency = %s AND date <= %s
            ORDER BY date DESC
            LIMIT 1
        """, (from_currency, to_currency, date), as_dict=True)

        return exchange_rate[0].exchange_rate if exchange_rate else None
    except Exception:
        return None


def get_or_create_customer(floriday_order, settings=None):
    """
    Every Floriday Sales Order is booked under the single fixed customer
    `Royal FloraHolland` (the auction party we sell to). We do NOT create a
    per-organization customer anymore — Floriday's buyer orgs all settle through
    Royal FloraHolland, and creating new customers tripped the mandatory
    `default_currency` field on this site. `Royal FloraHolland` already carries
    EUR / EUR Price List / Netherlands / 14-day terms, so the SO inherits the
    correct currency, price list and address.
    """
    return get_default_customer()


def _get_or_create_customer_legacy(floriday_order, settings=None):
    """
    Original per-organization match/create logic, kept for reference. No longer
    called — see get_or_create_customer above.

    Match priority:
    1. custom_floriday_id (UUID from Floriday) — most reliable.
    2. Exact match on customer_name (after fetching the real name from Floriday).
    3. Otherwise create a new customer.
    """
    if not floriday_order:
        frappe.throw("No order data provided")

    # Get the Floriday customer/organization ID (UUID format)
    customer_org_id = floriday_order.get('customerOrganizationId')
    if not customer_org_id:
        log_short("No customerOrganizationId in Floriday order", "Floriday Customer Warning", True)
        return get_default_customer()

    # STEP 1: Match by Floriday UUID — the canonical key.
    existing = frappe.db.get_value("Customer", {"custom_floriday_id": customer_org_id}, "name")
    if existing:
        log_short(f"Found customer {existing} with Floriday ID", "Floriday Customer Match", False)
        return existing

    # STEP 2: Determine the customer's real name. Order payload first, then /organizations/{id}.
    floriday_customer_name = (
        floriday_order.get('customerName')
        or floriday_order.get('consigneeName')
        or fetch_floriday_organization_name(customer_org_id, settings)
    )

    if floriday_customer_name:
        floriday_customer_name = floriday_customer_name.strip()

        # Match on the customer_name field (NOT the doc's name/PK — Customer's autoname
        # is naming_series:, so doc names look like "CUST-…" and won't match the human name).
        existing = frappe.db.get_value("Customer", {"customer_name": floriday_customer_name}, "name")
        if existing:
            frappe.db.set_value("Customer", existing, "custom_floriday_id", customer_org_id)
            log_short(f"Tagged existing customer {existing} ('{floriday_customer_name}') with Floriday ID",
                      "Floriday Customer Updated", False)
            return existing

    # STEP 3: Create a new customer (passing the resolved name through to avoid a 2nd API call).
    return create_new_customer(floriday_order, customer_org_id,
                               settings=settings, resolved_name=floriday_customer_name)


def fetch_floriday_organization_name(organization_id, settings):
    """
    Fetch an organization's name from Floriday using GET /organizations/{organizationId}.
    Returns None on any failure so the caller can fall back to a placeholder.
    """
    if not settings or not organization_id:
        return None
    try:
        base_url = (settings.base_url or "").rstrip('/')
        if not (base_url and settings.api_key and settings.access_token):
            return None

        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "X-Api-Key": settings.api_key,
            "Accept": "application/json",
        }
        response = requests.get(
            f"{base_url}/organizations/{organization_id}",
            headers=headers,
            timeout=15,
        )
        if response.status_code != 200:
            log_short(f"Org lookup {organization_id[:8]}: HTTP {response.status_code}",
                      "Floriday Org Lookup", True)
            return None

        data = response.json() or {}
        return data.get("commercialName") or data.get("name")
    except Exception as e:
        log_short(f"Org lookup error: {str(e)[:50]}", "Floriday Org Lookup Error", True)
        return None


def create_new_customer(floriday_order, customer_org_id, settings=None, resolved_name=None):
    """
    Creates a new customer with Floriday ID. Caller can pass `resolved_name` to skip
    re-fetching the name from the Floriday API.
    """
    # Resolve the name if not provided.
    floriday_customer_name = (
        resolved_name
        or floriday_order.get('customerName')
        or floriday_order.get('consigneeName')
        or fetch_floriday_organization_name(customer_org_id, settings)
    )
    if floriday_customer_name:
        floriday_customer_name = floriday_customer_name.strip()
    if not floriday_customer_name:
        floriday_customer_name = f"Consignee {customer_org_id[:8]}"

    # Final dedup check — guard against a race or the caller skipping the lookup.
    existing = frappe.db.get_value("Customer", {"customer_name": floriday_customer_name}, "name")
    if existing:
        # Tag the existing customer with the Floriday UUID and reuse it.
        frappe.db.set_value("Customer", existing, "custom_floriday_id", customer_org_id)
        log_short(f"Reused existing customer {existing} ('{floriday_customer_name}') with Floriday ID",
                  "Floriday Customer Updated", False)
        return existing

    try:
        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": floriday_customer_name,
            "custom_floriday_id": customer_org_id,
            "customer_type": "Company",
            "customer_group": "Commercial",
            "territory": "Netherlands",
        })
        customer.insert(ignore_permissions=True)

        log_short(f"Created new customer: {floriday_customer_name} with Floriday ID", "Floriday Customer Created", False)
        return customer.name

    except frappe.exceptions.DuplicateEntryError:
        # Lost a race — another worker created the same customer. Look it up and reuse.
        frappe.db.rollback()
        existing = frappe.db.get_value("Customer", {"customer_name": floriday_customer_name}, "name")
        if existing:
            frappe.db.set_value("Customer", existing, "custom_floriday_id", customer_org_id)
            return existing
        log_short(f"DuplicateEntryError for '{floriday_customer_name}' but no customer found",
                  "Floriday Customer Error", True)
        return get_default_customer()
    except Exception as e:
        log_short(f"Customer creation error: {str(e)[:50]}", "Floriday Customer Error", True)
        return get_default_customer()


def get_default_customer():
    """The single customer every Floriday Sales Order is booked under.

    Reads `Floriday Settings.customer` (e.g. Royal FloraHolland, which carries
    EUR / EUR Price List / Netherlands / 14-day terms). Does NOT create a
    customer: if the setting is unset or the customer is missing we throw, because
    importing an order under a half-built customer would only re-trigger the
    mandatory-field failures this change exists to fix.
    """
    customer = _floriday_setting("customer")
    if not customer:
        frappe.throw("Customer not configured in Floriday Settings")
    if frappe.db.exists("Customer", customer):
        return customer
    # Fall back to a customer_name match (in case the PK differs from the label).
    by_name = frappe.db.get_value("Customer", {"customer_name": customer}, "name")
    if by_name:
        return by_name
    frappe.throw(f"Floriday customer '{customer}' not found — create it before importing Floriday orders")


def get_erpnext_item_code(floriday_trade_item_id):
    """
    Get ERPNext item code from Floriday trade item ID via Floriday Items / Stem Length Price.
    Falls back to Item.floriday_trade_item_id for legacy items.
    """
    try:
        from ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items import (
            get_item_code_from_trade_item_id,
        )
        item_code = get_item_code_from_trade_item_id(floriday_trade_item_id)
        if item_code:
            return item_code

        item = frappe.db.get_value("Item", {"floriday_trade_item_id": floriday_trade_item_id}, "name")
        if item:
            return item

        frappe.throw(f"No item mapping for {floriday_trade_item_id}")
    except Exception as e:
        log_short(f"Item error: {str(e)[:30]}", "Floriday Item Error", True)
        raise


def parse_floriday_datetime(date_str, default=None):
    """Parse a Floriday datetime string, returning default (or datetime.now(utc)) on failure"""
    if default is None:
        default = datetime.now(timezone.utc)
    if not date_str:
        return default
    try:
        if '.' in date_str and 'Z' in date_str:
            return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        elif 'T' in date_str and 'Z' in date_str:
            return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        else:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except Exception:
        return default


@frappe.whitelist()
def get_sync_status():
    """Return the most recent Floriday-related Error Log entry."""
    try:
        latest_log = frappe.get_all("Error Log",
            filters={"method": ["like", "%Floriday%"]},
            fields=["name", "creation", "method", "error"],
            order_by="creation DESC",
            limit_page_length=1
        )

        return {
            "status": "success",
            "latest_log": latest_log[0] if latest_log else None
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def map_delivery_point(floriday_gln, delivery_point_name):
    """Manually tag a Delivery Point with a Floriday GLN (custom_floriday_delivery_id)."""
    try:
        if not floriday_gln or not delivery_point_name:
            return {"status": "error", "message": "Missing GLN or Delivery Point name"}

        if not frappe.db.exists("Delivery Point", delivery_point_name):
            return {"status": "error", "message": f"Delivery Point {delivery_point_name} not found"}

        frappe.db.set_value("Delivery Point", delivery_point_name, "custom_floriday_delivery_id", floriday_gln)

        log_short(f"Mapped Floriday GLN {floriday_gln} to Delivery Point {delivery_point_name}",
                 "Floriday Delivery Point Mapping", False)

        return {"status": "success", "message": f"Mapped GLN {floriday_gln} to {delivery_point_name}"}

    except Exception as e:
        log_short(f"Error mapping delivery point: {str(e)[:50]}", "Floriday Mapping Error", True)
        return {"status": "error", "message": str(e)}


@frappe.whitelist()
def get_mapped_delivery_points():
    """Return all Delivery Points that have custom_floriday_delivery_id set."""
    try:
        delivery_points = frappe.get_all(
            "Delivery Point",
            filters={"custom_floriday_delivery_id": ["!=", ""]},
            fields=["name", "custom_floriday_delivery_id"]
        )
        return {
            "status": "success",
            "mappings": delivery_points,
            "count": len(delivery_points)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}