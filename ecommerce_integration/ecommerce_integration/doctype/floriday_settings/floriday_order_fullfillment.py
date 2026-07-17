import frappe
import requests
import json
import uuid
import math
from datetime import datetime
from frappe.utils import now_datetime, add_to_date

def safe_log(message, title=None, log_type="info"):
    """Log via the logger, truncating to avoid CharacterLengthExceededError;
    errors are also written to the Error Log."""
    message_str = str(message)
    if len(message_str) > 200:
        message_str = message_str[:200] + "..."

    logger = frappe.logger()

    if log_type == "error":
        logger.error(f"{title or 'ERROR'}: {message_str}")
        if title and "Error" in title:
            simple_title = title[:80] if len(title) > 80 else title
            simple_msg = message_str[:100] if len(message_str) > 100 else message_str
            frappe.log_error(simple_msg, simple_title)
    elif log_type == "warning":
        logger.warning(f"{title or 'WARNING'}: {message_str}")
    elif log_type == "debug":
        logger.debug(f"{title or 'DEBUG'}: {message_str}")
    else:
        logger.info(f"{title or 'INFO'}: {message_str}")

def get_delivery_gln_from_sales_order(sales_order):
    """Return the Sales Order's custom_floriday_delivery_id, or None if missing."""
    try:
        gln = sales_order.get('custom_floriday_delivery_id')
        if gln:
            return gln
        safe_log(f"Sales Order {sales_order.name} has no custom_floriday_delivery_id",
                 "Missing GLN", "warning")
        return None
    except Exception as e:
        safe_log(f"Error getting GLN for sales order: {str(e)}",
                 "GLN Lookup Error", "error")
        return None

def get_default_gln():
    """Get default GLN. Floriday Settings has no default_gln field, so this is
    a fixed fallback; reintroduce a configurable field if multiple GLNs are needed."""
    return "8713783461136"

def get_delivery_remarks(sales_order):
    """Get delivery remarks, ensuring minimum length of 1 character"""
    remarks = ""
    if hasattr(sales_order, 'delivery_notes') and sales_order.delivery_notes:
        remarks = sales_order.delivery_notes

    if not remarks or len(remarks.strip()) == 0:
        remarks = "Standard delivery"

    if len(remarks) > 100:
        remarks = remarks[:97] + "..."

    return remarks

def get_commercial_invoice_reference(floriday_order_id, sales_order_name):
    """Generate commercial invoice reference with max length of 26 characters"""
    short_uuid = floriday_order_id[-10:] if len(floriday_order_id) > 10 else floriday_order_id
    short_so = sales_order_name[-8:] if len(sales_order_name) > 8 else sales_order_name

    reference = f"{short_uuid}-{short_so}"

    if len(reference) > 26:
        reference = reference[:26]

    return reference

def get_load_carrier_reference(sales_order_name):
    """Generate load carrier reference with max length of 14 characters"""
    if len(sales_order_name) > 14:
        reference = sales_order_name[-14:]
    else:
        reference = sales_order_name.zfill(14)

    return reference

def get_fulfillment_request_id(base_url, headers, sales_order_id):
    """
    Fetch the fulfillmentRequestId from Floriday for a given salesOrderId.
    Calls GET /fulfillment-requests and finds the matching request.
    """
    try:
        url = f"{base_url}/fulfillment-requests"
        params = {"salesOrderId": sales_order_id}
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            # Response may be a list or a dict with results key
            items = data if isinstance(data, list) else data.get("results", data.get("items", []))
            for item in items:
                if item.get("salesOrderId") == sales_order_id:
                    req_id = item.get("fulfillmentRequestId")
                    if req_id:
                        safe_log(f"Found fulfillmentRequestId {req_id} for salesOrderId {sales_order_id}",
                                 "Fulfillment Request Lookup", "info")
                        return req_id
            # If only one result and no salesOrderId field to match, return its ID
            if len(items) == 1:
                req_id = items[0].get("fulfillmentRequestId")
                if req_id:
                    return req_id
            safe_log(f"No fulfillmentRequestId found for salesOrderId {sales_order_id}. Response: {str(data)[:300]}",
                     "Fulfillment Request Lookup Warning", "warning")
        else:
            safe_log(f"GET /fulfillment-requests failed: {response.status_code} {response.text[:200]}",
                     "Fulfillment Request Lookup Error", "error")
    except Exception as e:
        safe_log(f"Error fetching fulfillment requests: {str(e)}", "Fulfillment Request Lookup Error", "error")

    return None


def build_delivery_order_index(base_url, headers):
    """Page GET /delivery-orders/sync and index every fulfillment request by its
    salesOrderId.

    In Floriday 2025v2 a committed sales order produces a *delivery order*
    containing one or more *fulfillment requests*. A fulfillment order (what we
    POST) must reference an existing fulfillment request AND carry that delivery
    order's own delivery-location GLN — sending a different GLN fails with
    "different country" (and an order with no delivery order fails with "No
    delivery orders found"). So we read the delivery orders first and key them by
    salesOrderId (which equals the SO's po_no).

    Returns { salesOrderId: {
        "delivery_order_id", "gln", "fulfillment_request_id",
        "number_of_packages", "fulfilled"
    } }.
    """
    index = {}
    seq = 0
    pages = 0
    while pages < 200:
        try:
            r = requests.get(f"{base_url}/delivery-orders/sync/{seq}", headers=headers, timeout=40)
        except Exception as e:
            safe_log(f"delivery-orders/sync error at seq {seq}: {str(e)[:120]}", "Floriday Delivery Order Sync", "error")
            break
        if r.status_code != 200:
            safe_log(f"delivery-orders/sync HTTP {r.status_code} at seq {seq}: {r.text[:150]}",
                     "Floriday Delivery Order Sync", "error")
            break
        data = r.json() or {}
        results = data.get("results", [])
        if not results:
            break
        for do in results:
            if do.get("isDeleted"):
                continue
            gln = ((do.get("destination") or {}).get("location") or {}).get("gln")
            for fr in do.get("fulfillmentRequests", []):
                sales_order_id = fr.get("salesOrderId")
                if not sales_order_id:
                    continue
                index[sales_order_id] = {
                    "delivery_order_id": do.get("deliveryOrderId"),
                    "gln": gln,
                    "fulfillment_request_id": fr.get("fulfillmentRequestId"),
                    "number_of_packages": fr.get("numberOfPackages"),
                    "fulfilled": do.get("fulfilled"),
                }
        max_seq = data.get("maximumSequenceNumber", seq)
        if max_seq <= seq:
            break
        seq = max_seq
        pages += 1
    return index


def update_delivery_note_with_fulfillment(sales_order_name, fulfillment_id):
    """Update Delivery Note with fulfillment information"""
    try:
        delivery_notes = frappe.get_all(
            "Delivery Note",
            filters={"docstatus": 1, "against_sales_order": sales_order_name},
            fields=["name"],
            limit=1
        )

        if delivery_notes:
            delivery_note = frappe.get_doc("Delivery Note", delivery_notes[0].name)
            current_remarks = delivery_note.remarks or ""
            fulfillment_info = f"\n[Floriday Fulfillment ID: {fulfillment_id}]"
            delivery_note.remarks = current_remarks + fulfillment_info
            delivery_note.save()
            frappe.db.commit()
            safe_log(f"Updated Delivery Note {delivery_note.name} with fulfillment ID", "Delivery Note Update", "info")

    except Exception as e:
        safe_log(f"Could not update delivery note: {str(e)}", "Delivery Note Update Error", "warning")

@frappe.whitelist()
def order_fullment():
    """Create Floriday fulfillment orders (POST /fulfillment-orders) for Sales
    Orders submitted within the last `of_period` hours (Floriday Settings;
    defaults to 24h when unset)."""
    logger = frappe.logger()

    def step(msg):
        logger.info(f"[Floriday Fulfillment] {msg}")

    try:
        # ── Settings ────────────────────────────────────────────────────────
        settings = frappe.get_single("Floriday Settings")

        # Look back `of_period` hours from now (configurable on Floriday Settings).
        # Fall back to 24h if the field is unset/zero/invalid.
        try:
            period_hours = int(settings.of_period or 0)
        except (TypeError, ValueError):
            period_hours = 0
        if period_hours <= 0:
            period_hours = 24

        now = now_datetime()
        start_time = add_to_date(now, hours=-period_hours)
        step(f"STEP 1: Started. now={now}, looking back {period_hours}h to {start_time}")

        API_KEY = settings.api_key
        BASE_URL = settings.base_url.rstrip('/')
        ACCESS_TOKEN = settings.access_token
        SUPPLIER_ORG_ID = settings.organization_supplier_id

        missing = [k for k, v in {"API_KEY": API_KEY, "BASE_URL": BASE_URL,
                                   "ACCESS_TOKEN": ACCESS_TOKEN, "SUPPLIER_ORG_ID": SUPPLIER_ORG_ID}.items() if not v]
        if missing:
            step(f"STEP 2 FAILED: Missing settings: {missing}")
            return {"status": "error", "message": f"Floriday Settings incomplete: {missing}"}

        step(f"STEP 2 OK: Settings loaded. BASE_URL={BASE_URL}, SUPPLIER_ORG_ID='{SUPPLIER_ORG_ID}'")

        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "text/plain"
        }

        # ── Query Sales Orders (Last 24 hours) ──────────────────────────────
        # Floriday Sales Orders all book under the customer configured on Floriday
        # Settings (e.g. Royal FloraHolland) and carry the Floriday salesOrderId in
        # po_no — that pair identifies them. (The legacy filter on the customer's
        # custom_floriday_id no longer works now that every order shares one
        # customer.) Pull the SO's own custom_floriday_delivery_id when present so
        # fulfillment can use the buyer GLN without relying on the Delivery Point.
        floriday_customer = settings.get("customer")
        if not floriday_customer:
            step("STEP 3 FAILED: Customer not configured in Floriday Settings")
            return {"status": "error", "message": "Customer not configured in Floriday Settings"}

        has_so_gln = frappe.db.has_column("Sales Order", "custom_floriday_delivery_id")
        gln_select = "so.custom_floriday_delivery_id" if has_so_gln else "NULL"
        sales_orders = frappe.db.sql(f"""
            SELECT so.name, so.po_no, so.customer, so.delivery_date, so.status,
                   so.creation, so.custom_delivery_point,
                   {gln_select} AS custom_floriday_delivery_id
            FROM `tabSales Order` so
            WHERE so.docstatus = 1
              AND so.po_no != ''
              AND so.creation >= %(start_time)s
              AND so.customer = %(customer)s
            ORDER BY so.creation DESC
        """, {"start_time": start_time, "customer": floriday_customer}, as_dict=True)
        step(f"STEP 3: Orders in last {period_hours}h: {len(sales_orders)}")

        if not sales_orders:
            step(f"STEP 3: No orders in last {period_hours}h — nothing to fulfill")
            return {
                "status": "success",
                "message": (
                    f"No Floriday Sales Orders found in the last {period_hours} hours. "
                    f"Tip: only orders submitted within the past {period_hours} hours, with a customer "
                    "tagged with custom_floriday_id, are eligible."
                ),
                "results": [],
                "summary": {
                    "total": 0,
                    "successful": 0,
                    "errors": 0,
                    "fulfilled_orders": [],
                    "failed_orders": [],
                },
            }

        # ── Process Each Order ──────────────────────────────────────────────
        endpoint = f"{BASE_URL}/fulfillment-orders"
        results = []
        success_count = 0
        error_count = 0

        # Build the delivery-order index once: maps salesOrderId → its delivery
        # order GLN + fulfillment request. An order absent from this index has no
        # delivery order on Floriday yet and cannot be fulfilled.
        delivery_order_index = build_delivery_order_index(BASE_URL, headers)
        step(f"STEP 3b: Indexed {len(delivery_order_index)} delivery-order fulfillment requests")

        for so in sales_orders:
            sales_order_name = so.name
            floriday_order_id = so.po_no
            step(f"STEP 4: Processing {sales_order_name} | fulfillmentOrderId={floriday_order_id}")

            try:
                sales_order = frappe.get_doc("Sales Order", sales_order_name)
                step(f"  STEP 4a: Loaded SO. items={len(sales_order.items)}")

                if not sales_order.items:
                    step(f"  STEP 4b: No items in {sales_order_name} — skipping")
                    error_count += 1
                    results.append({
                        "sales_order": sales_order_name,
                        "status": "error",
                        "message": "Sales Order has no items"
                    })
                    continue

                # Calculate total stems and packages.
                # item.qty is in the selling UOM (bunches); stems = qty * conversion_factor.
                # stock_qty already holds this product, so prefer it when present.
                total_stems = sum(
                    float(item.stock_qty or (item.qty or 0) * (item.conversion_factor or 1))
                    for item in sales_order.items
                )

                if total_stems <= 0:
                    step(f"  STEP 4b ERROR: Total stems is 0 for {sales_order_name}")
                    error_count += 1
                    results.append({
                        "sales_order": sales_order_name,
                        "status": "error",
                        "message": "Total stems quantity is 0"
                    })
                    continue

                # Look the order up in the delivery-order index. No entry → Floriday
                # has no delivery order for it yet, so it can't be fulfilled; skip
                # with a clear status instead of a guaranteed-to-fail POST.
                do_entry = delivery_order_index.get(floriday_order_id)
                if not do_entry:
                    step(f"  STEP 4b SKIP: no delivery order for {sales_order_name} ({floriday_order_id})")
                    error_count += 1
                    results.append({
                        "sales_order": sales_order_name,
                        "floriday_order_id": floriday_order_id,
                        "status": "error",
                        "message": "No delivery order on Floriday yet (not committed / not ready to fulfill)",
                    })
                    continue

                # Package count and delivery GLN come from the delivery order itself.
                # Floriday rejects a mismatched GLN ("different country") and a wrong
                # package count, so prefer the delivery order's own values; fall back
                # to the stem-derived count / JKIA default only if absent.
                number_of_packages = do_entry.get("number_of_packages") or math.ceil(total_stems / 200)
                delivery_gln = do_entry.get("gln")
                if not delivery_gln:
                    delivery_gln = get_delivery_gln_from_sales_order(sales_order) or get_default_gln()
                    safe_log(f"Delivery order {do_entry.get('delivery_order_id')} had no GLN; using fallback {delivery_gln}",
                             "Default GLN Used", "warning")
                step(f"  STEP 4b: Packages = {number_of_packages}, GLN = {delivery_gln} (DO {do_entry.get('delivery_order_id')})")

                # Fulfillment request id from the delivery order (equals salesOrderId
                # in DIRECT_SALES, but use the value Floriday gave us to be safe).
                fulfillment_request_id = do_entry.get("fulfillment_request_id") or floriday_order_id

                load_carrier_reference = get_load_carrier_reference(sales_order_name)
                commercial_invoice_ref = get_commercial_invoice_reference(floriday_order_id, sales_order_name)
                delivery_remarks = get_delivery_remarks(sales_order)

                # Generate a new UUID for the fulfillmentOrderId — this is OUR identifier for this fulfillment,
                # NOT the buyer's salesOrderId (floriday_order_id). Reusing po_no causes the 400 error.
                new_fulfillment_order_id = str(uuid.uuid4())
                step(f"  STEP 4c: fulfillmentRequestId={fulfillment_request_id}, new fulfillmentOrderId={new_fulfillment_order_id}")

                # Floriday rejects empty strings for these optional fields with a
                # 400 validation error ("must be a string ... with a minimum length
                # of '1'"). Only include them when they actually carry a value.
                load_carrier_item = {
                    "fulfillmentRequestId": fulfillment_request_id,
                    "numberOfPackages": number_of_packages,
                    "serviceCode": 1,  # Standard service code; 9999 was the spec's max-value example, not a valid code
                    "packingAgentOrganizationId": SUPPLIER_ORG_ID,
                    "sortIndex": 0,
                }
                if delivery_remarks:
                    load_carrier_item["deliveryRemarks"] = delivery_remarks
                if commercial_invoice_ref:
                    load_carrier_item["commercialInvoiceReference"] = commercial_invoice_ref

                load_carrier = {
                    "loadCarrierItems": [load_carrier_item],
                    "loadCarrierType": "NONE",
                    "numberOfAdditionalLayers": 0,
                    "sortIndex": 0,
                }
                if load_carrier_reference:
                    load_carrier["loadCarrierReference"] = load_carrier_reference

                fulfillment_payload = {
                    "fulfillmentOrderId": new_fulfillment_order_id,
                    "carrierOrganizationId": SUPPLIER_ORG_ID,
                    "logisticHub": "NONE",
                    "oneLabelOnly": False,
                    "loadCarriers": [load_carrier],
                    "deliveryLocationGln": delivery_gln
                }

                step(f"  STEP 4d: Sending POST to {endpoint}")
                response = requests.post(endpoint, headers=headers, json=fulfillment_payload, timeout=30)

                step(f"  STEP 4e: Response status={response.status_code}")

                if response.status_code in (200, 201):
                    response_data = response.json() if response.text else {}
                    fulfillment_id = response_data.get("fulfillmentOrderId", new_fulfillment_order_id)
                    step(f"  STEP 4f SUCCESS: fulfillment_id={fulfillment_id}")

                    current_remarks = sales_order.get("remarks") or ""
                    sales_order.remarks = (
                        current_remarks
                        + f"\n[Floriday Fulfillment Created: {fulfillment_id} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                        + f"\nTotal Stems: {total_stems}, Packages: {number_of_packages} (200 stems/package)"
                        + f"\nDelivery GLN: {delivery_gln}"
                        + f"\nFulfillment Request ID: {fulfillment_request_id}"
                    )
                    sales_order.save(ignore_permissions=True)
                    frappe.db.commit()

                    update_delivery_note_with_fulfillment(sales_order_name, fulfillment_id)

                    success_count += 1
                    results.append({
                        "sales_order": sales_order_name,
                        "floriday_order_id": floriday_order_id,
                        "fulfillment_id": fulfillment_id,
                        "fulfillment_request_id": fulfillment_request_id,
                        "total_stems": total_stems,
                        "number_of_packages": number_of_packages,
                        "delivery_gln": delivery_gln,
                        "status": "success",
                        "message": f"Fulfillment order created successfully"
                    })
                else:
                    error_detail = ""
                    try:
                        error_response = response.json()
                        error_detail = json.dumps(error_response, indent=2)
                    except:
                        error_detail = response.text

                    # "already fulfilled" means the order was successfully fulfilled in a prior run
                    if response.status_code == 400 and "has already been fulfilled" in error_detail:
                        step(f"  STEP 4f ALREADY FULFILLED: {sales_order_name}")
                        success_count += 1
                        results.append({
                            "sales_order": sales_order_name,
                            "floriday_order_id": floriday_order_id,
                            "fulfillment_request_id": fulfillment_request_id,
                            "status": "success",
                            "message": "Already fulfilled in Floriday"
                        })
                    else:
                        step(f"  STEP 4f ERROR: {error_detail[:200]}")
                        error_count += 1
                        results.append({
                            "sales_order": sales_order_name,
                            "floriday_order_id": floriday_order_id,
                            "new_fulfillment_order_id": new_fulfillment_order_id,
                            "fulfillment_request_id": fulfillment_request_id,
                            "status": "error",
                            "status_code": response.status_code,
                            "message": f"HTTP {response.status_code}: {error_detail[:200]}"
                        })

            except Exception as e:
                import traceback
                step(f"  STEP 4 EXCEPTION: {str(e)[:200]}")
                frappe.log_error(
                    title=f"Floriday Fulfillment Exception - {sales_order_name}",
                    message=f"Error: {str(e)}\n\nTraceback: {traceback.format_exc()[:500]}"
                )
                error_count += 1
                results.append({
                    "sales_order": sales_order_name,
                    "status": "error",
                    "message": str(e)[:300]
                })

        step(f"STEP 5 DONE: total={len(sales_orders)} success={success_count} errors={error_count}")

        # Build a short message: just the Sales Order name and a short status word.
        fulfilled = [r.get("sales_order") for r in results if r.get("status") == "success"]
        failed = [r.get("sales_order") for r in results if r.get("status") == "error"]

        parts = []
        for name in fulfilled:
            parts.append(f"{name} fulfilled")
        for name in failed:
            parts.append(f"{name} failed")

        message = ", ".join(parts) if parts else "No orders processed"

        return {
            "status": "success",
            "message": message,
            "summary": {
                "total": len(sales_orders),
                "successful": success_count,
                "errors": error_count,
                "fulfilled_orders": fulfilled,
                "failed_orders": failed,
            },
        }

    except Exception as e:
        import traceback
        frappe.log_error(f"Floriday Fulfillment Fatal Error: {str(e)[:150]}\n{traceback.format_exc()[:200]}", "Floriday Fulfillment Fatal Error")
        return {"status": "error", "message": str(e)[:300]}