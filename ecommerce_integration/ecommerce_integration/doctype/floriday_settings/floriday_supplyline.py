import frappe
import requests
import uuid
import json
from datetime import datetime, timezone, timedelta

SUPPLY_LINE_CURRENCY = "EUR"

EAT_OFFSET = timedelta(hours=3)

# Maximum log message length to prevent truncation errors
MAX_LOG_LENGTH = 100

# Process only the latest N batches (sorted by batchDate desc) — older batches are typically depleted.
# Set generous enough to cover today + a few days of future-dated stock.
LATEST_BATCH_LIMIT = 1000

def safe_log(message, title="Floriday Log"):
    """Log to Error Log, truncating message/title to stay under the length limit."""
    if not message:
        return
    if not title:
        title = "Floriday Log"

    if len(message) > MAX_LOG_LENGTH:
        message = message[:MAX_LOG_LENGTH-3] + "..."
    if len(title) > 100:
        title = title[:97] + "..."

    try:
        frappe.log_error(message, title)
    except:
        pass

@frappe.whitelist()
def create_supply_lines_only_from_batches():
    """Create supply lines (no customer offers) from today's available batches,
    filtered to the current EAT date."""
    try:
        safe_log("Starting supply line creation", "Floriday Supply Lines")

        settings = frappe.get_single("Floriday Settings")

        API_KEY = settings.api_key
        BASE_URL = settings.base_url
        ACCESS_TOKEN = settings.access_token
        SUPPLIER_ORG_ID = settings.organization_supplier_id

        current_date = (datetime.now(timezone.utc) + EAT_OFFSET).strftime('%Y-%m-%d')
        safe_log(f"Filtering for EAT date: {current_date}", "Floriday Date")

        # Fetch all batches. Floriday paginates oldest-first by sequenceNumber, but for
        # today's stock we filter by EAT date below — order of arrival doesn't matter.
        all_batches, fetch_error = get_your_floriday_batches(
            BASE_URL, API_KEY, ACCESS_TOKEN, SUPPLIER_ORG_ID, return_error=True
        )

        if not all_batches:
            error_msg = fetch_error or "No batches found for your organization"
            safe_log(error_msg, "Floriday Batches")
            return {
                "status": "failed",
                "message": error_msg,
                "date_applied": current_date,
            }

        safe_log(f"Retrieved {len(all_batches)} total batches", "Floriday Batches")

        # Drop soft-deleted batches
        active_batches = [b for b in all_batches if not b.get("isDeleted")]
        safe_log(f"{len(active_batches)} active (non-deleted) of {len(all_batches)}", "Floriday Batches")

        # Filter to today's EAT batches first (mirror UI's get_available_batches logic)
        todays_batches = filter_batches_by_date_eat(active_batches, current_date)
        safe_log(f"{len(todays_batches)} batches dated EAT today", "Floriday Batches")

        # Sort newest-first within today's set
        todays_batches = sort_batches_newest_first(todays_batches)

        # Keep batches with available stock
        available_batches = filter_available_batches_fixed(todays_batches)
        safe_log(f"Found {len(available_batches)} batches with available pieces", "Floriday Available")

        if not available_batches:
            result_msg = {
                "status": "failed",
                "message": f"No batches with available pieces dated {current_date} (EAT)",
                "total_batches": len(all_batches),
                "active_batches": len(active_batches),
                "todays_batches": len(todays_batches),
                "available_batches": 0,
                "date_applied": current_date,
            }
            return result_msg

        safe_log("Creating supply lines", "Floriday Creation")
        results = create_supply_lines_only(BASE_URL, API_KEY, ACCESS_TOKEN, available_batches)

        successful_supply_lines = [r for r in results if r.get('status') == 'success']
        failed_supply_lines = [r for r in results if r.get('status') != 'success']

        safe_log(f"Results: {len(successful_supply_lines)} success, {len(failed_supply_lines)} failed", "Floriday Complete")

        if not successful_supply_lines:
            result_msg = {
                "status": "failed",
                "message": "Failed to create any supply lines",
                "details": results,
                "available_batches_processed": len(available_batches),
                "date_applied": current_date
            }
            return result_msg

        success_result = {
            "status": "success",
            "message": f"Created {len(successful_supply_lines)} supply lines from {len(available_batches)} available batches",
            "failed_supply_lines": failed_supply_lines,
            "total_processed": len(results),
            "total_batches": len(all_batches),
            "active_batches": len(active_batches),
            "todays_batches": len(todays_batches),
            "available_batches": len(available_batches),
            "date_applied": current_date,
            "currency_used": SUPPLY_LINE_CURRENCY,
        }

        return success_result

    except Exception as e:
        error_msg = f"Unexpected error"
        safe_log(error_msg, "Floriday Error")
        return {"status": "error", "message": f"Error: {str(e)[:100]}"}

def filter_batches_by_date_eat(batches, target_date):
    """Keep batches whose batchDate falls on target_date in EAT (UTC+3)."""
    try:
        todays_batches = []

        for batch in batches:
            batch_date_str = batch.get("batchDate")
            if batch_date_str:
                try:
                    if batch_date_str.endswith('Z'):
                        batch_dt_utc = datetime.fromisoformat(batch_date_str.replace('Z', '+00:00'))
                    else:
                        batch_dt_utc = datetime.fromisoformat(batch_date_str)
                    if batch_dt_utc.tzinfo is None:
                        batch_dt_utc = batch_dt_utc.replace(tzinfo=timezone.utc)

                    batch_eat_date = (batch_dt_utc + EAT_OFFSET).strftime('%Y-%m-%d')
                    if batch_eat_date == target_date:
                        todays_batches.append(batch)
                except Exception:
                    # Unparseable date — fall back to a substring match on the raw string.
                    if target_date in batch_date_str:
                        todays_batches.append(batch)

        return todays_batches
    except Exception:
        return []

def filter_available_batches_fixed(batches):
    """
    Keep only batches with live stock to offer.

    `numberOfPieces` is the current remaining stock (decremented as orders fill).
    Batches with 0 remaining are skipped — we don't fall back to
    `initialNumberOfPieces` because that's the original batch size, not what's
    actually in the warehouse.
    """
    try:
        available_batches = []
        for batch in batches:
            pieces = batch.get("numberOfPieces") or 0
            if pieces > 0:
                batch["available_pieces"] = pieces
                available_batches.append(batch)
        return available_batches
    except Exception:
        return []

def sort_batches_newest_first(batches):
    """
    Sort batches by batchDate descending (newest first), falling back to
    sequenceNumber when batchDate is missing or unparseable.
    """
    def sort_key(batch):
        batch_date_str = batch.get("batchDate")
        parsed = None
        if batch_date_str:
            try:
                if batch_date_str.endswith("Z"):
                    parsed = datetime.fromisoformat(batch_date_str.replace("Z", "+00:00"))
                else:
                    parsed = datetime.fromisoformat(batch_date_str)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                parsed = None
        primary = parsed.timestamp() if parsed else float("-inf")
        secondary = batch.get("sequenceNumber") or 0
        return (primary, secondary)

    try:
        return sorted(batches, key=sort_key, reverse=True)
    except Exception:
        return batches

def create_supply_lines_only(BASE_URL, API_KEY, ACCESS_TOKEN, batches):
    """POST a supply line per batch (capped at 10), without customer offers."""
    try:
        results = []

        for batch in batches[:10]:
            result = create_single_supply_line(BASE_URL, API_KEY, ACCESS_TOKEN, batch)
            results.append(result)

            frappe.db.commit()
            import time
            time.sleep(1)  # throttle between API calls

        return results
    except Exception:
        safe_log("Error in supply line creation", "Floriday Error")
        return []

def create_single_supply_line(BASE_URL, API_KEY, ACCESS_TOKEN, batch):
    """Build and POST one supply line for a batch; returns a status dict."""
    try:
        batch_id = batch.get("batchId")
        trade_item_id = batch.get("tradeItemId")
        available_pieces = batch.get("available_pieces", 0)
        warehouse_id = batch.get("warehouseId")

        if available_pieces <= 0:
            return {"status": "failed", "message": "No pieces", "batch_id": batch_id}

        if not warehouse_id:
            return {"status": "failed", "message": "No warehouse", "batch_id": batch_id}

        # Per-stem rate from Floriday Items > Stem Length Price (by trade_item_id)
        offer_price = get_item_price_from_erpnext(trade_item_id)
        if not offer_price:
            return {
                "status": "skipped",
                "message": f"No Stem Length Price for trade_item_id {trade_item_id}",
                "batch_id": batch_id,
            }

        now = datetime.now(timezone.utc)
        order_end = now + timedelta(days=7)

        packing_config = batch.get("packingConfiguration", get_default_packing_config())

        supply_line_payload = {
            "supplyLineId": str(uuid.uuid4()),
            "tradeItemId": trade_item_id,
            "warehouseId": warehouse_id,
            "numberOfPieces": available_pieces,
            "pricePerPiece": {
                "currency": SUPPLY_LINE_CURRENCY,
                "value": float(offer_price)
            },
            "orderPeriod": {
                "startDateTime": now.isoformat(),
                "endDateTime": order_end.isoformat()
            },
            "deliveryPeriod": {
                "startDateTime": now.isoformat(),
                "endDateTime": order_end.isoformat()
            },
            "allowedCustomerOrganizationIds": [],  # Empty for public
            "batchId": batch_id,
            "salesUnit": "PIECE",
            "packingConfigurations": [packing_config],
            "includedServices": ["DELIVERY"],
            "availability": "LIMITED"
        }

        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        base_url_clean = BASE_URL.rstrip('/')
        supply_line_endpoint = f"{base_url_clean}/supply-lines"

        response = requests.post(
            supply_line_endpoint,
            json=supply_line_payload,
            headers=headers,
            timeout=30
        )

        if response.status_code in (200, 201):
            supply_line_id = supply_line_payload["supplyLineId"]

            return {
                "status": "success",
                "supply_line_id": supply_line_id,
                "batch_id": batch_id,
                "trade_item_id": trade_item_id,
                "offered_quantity": available_pieces,
                "offer_price": offer_price,
                "currency": SUPPLY_LINE_CURRENCY,
                "warehouse_id": warehouse_id,
                "type": "supply_line"
            }
        else:
            error_msg = f"Failed: {response.status_code}"
            return {"status": "failed", "message": error_msg, "batch_id": batch_id}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": "Request error", "batch_id": batch_id}
    except Exception as e:
        return {"status": "error", "message": "Error", "batch_id": batch_id}

def get_item_price_from_erpnext(trade_item_id):
    """
    Look up the per-stem rate from Floriday Items > Stem Length Price by trade_item_id.

    Note: the same `Stem Length Price` child table is also used by Webshop Item
    Prices for pricing; only Floriday Items rows carry a trade_item_id, so
    filtering on trade_item_id is sufficient to scope this query.
    """
    if not trade_item_id:
        return None
    try:
        row = frappe.db.sql(
            """
            select rate
            from `tabStem Length Price`
            where parenttype = 'Floriday Items'
              and trade_item_id = %s
              and ifnull(rate, 0) > 0
            limit 1
            """,
            (trade_item_id,),
            as_dict=True,
        )
        if row and row[0].rate:
            return float(row[0].rate)
        return None
    except Exception:
        return None

def get_your_floriday_batches(BASE_URL, API_KEY, ACCESS_TOKEN, SUPPLIER_ORG_ID, tail_only=False, tail_size=1500, return_error=False):
    """
    Get batches via Floriday's documented sync endpoint:
      GET /batches/sync/{sequenceNumber}?limitResult=1000
      -> { maximumSequenceNumber, results: [Batch, ...] }

    Walk by passing the highest seen sequenceNumber as the next path segment until
    we catch up to maximumSequenceNumber. Org is inferred from the JWT/X-Api-Key —
    do NOT add ?supplierOrganizationId= (it's silently ignored on /batches and
    not a parameter on /batches/sync/).

    tail_only=True: skip ahead by first reading /batches/current-max-sequence and
    jumping to (max - tail_size) so we only fetch recent batches, not 30k stale ones.
    """
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "X-Api-Key": API_KEY,
        "Accept": "application/json",
    }
    base_url_clean = BASE_URL.rstrip("/")
    page_limit = 1000  # max documented
    max_pages = 1000   # safety cap (≤ 1M batches)
    last_error = {"msg": None}

    def _result(batches, error=None):
        if return_error:
            return batches, error
        return batches

    def fetch_sync_page(seq_from):
        url = f"{base_url_clean}/batches/sync/{seq_from}?limitResult={page_limit}"
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            err = f"HTTP {response.status_code}: {response.text[:200]}"
            last_error["msg"] = err
            safe_log(f"Batches sync {err[:80]}", "Floriday Batches Error")
            return None
        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            return None
        return body  # { maximumSequenceNumber, results: [...] }

    def fetch_current_max():
        url = f"{base_url_clean}/batches/current-max-sequence"
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            err = f"HTTP {response.status_code}: {response.text[:200]}"
            last_error["msg"] = err
            return None
        try:
            return int(response.text.strip())
        except Exception:
            return None

    try:
        # Decide starting sequence number
        sequence_from = 0
        if tail_only:
            current_max = fetch_current_max()
            if current_max is None:
                return _result([], last_error["msg"] or "Could not read current-max-sequence")
            sequence_from = max(0, current_max - tail_size)
            safe_log(
                f"Tail mode: starting at seq {sequence_from} (max={current_max}, size={tail_size})",
                "Floriday Batches Tail",
            )

        all_batches = []
        for _ in range(max_pages):
            body = fetch_sync_page(sequence_from)
            if body is None:
                break
            results = body.get("results") or []
            if not results:
                break  # no more records ahead
            all_batches.extend(results)
            page_max = max(b.get("sequenceNumber") or 0 for b in results)
            if page_max <= sequence_from:
                break  # no progress, avoid infinite loop
            sequence_from = page_max  # advance cursor to highest seen

        safe_log(
            f"Sync fetched {len(all_batches)} batches (last seq={sequence_from})",
            "Floriday Batches Pages",
        )
        return _result(all_batches, last_error["msg"] if not all_batches else None)

    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        safe_log(f"Batches sync error: {err[:80]}", "Floriday Batches Error")
        return _result([], err)

def get_default_packing_config():
    return {
        "piecesPerPackage": 200,
        "vbnPackageCode": 884,
        "packagesPerLayer": 10,
        "layersPerLoadCarrier": 2,
        "loadCarrier": "AUCTION_TROLLEY",
        "transportHeightInCm": 100
    }

@frappe.whitelist()
def get_available_batches():
    """Return today's (EAT) batches that still have available pieces, for the UI picker."""
    try:
        current_date = (datetime.now(timezone.utc) + EAT_OFFSET).strftime('%Y-%m-%d')

        settings = frappe.get_single("Floriday Settings")

        API_KEY = settings.api_key
        BASE_URL = settings.base_url
        ACCESS_TOKEN = settings.access_token
        SUPPLIER_ORG_ID = settings.organization_supplier_id

        all_batches = get_your_floriday_batches(BASE_URL, API_KEY, ACCESS_TOKEN, SUPPLIER_ORG_ID)

        if not all_batches:
            return {
                "status": "success",
                "batches": [],
                "total_batches": 0,
                "date_applied": current_date,
                "message": "No batches found"
            }

        todays_batches = filter_batches_by_date_eat(all_batches, current_date)

        if not todays_batches:
            return {
                "status": "success",
                "batches": [],
                "total_batches": len(all_batches),
                "todays_batches": 0,
                "date_applied": current_date,
                "message": f"No batches found for EAT today ({current_date})"
            }

        todays_batches = sort_batches_newest_first(todays_batches)
        available_batches = filter_available_batches_fixed(todays_batches)

        batch_options = []
        for batch in available_batches:
            available_quantity = batch.get("available_pieces", 0)
            batch_options.append({
                "batch_id": batch.get("batchId"),
                "trade_item_id": batch.get("tradeItemId"),
                "trade_item_name": batch.get("tradeItemName", "Unknown Item"),
                "available_quantity": available_quantity,
                "batch_date": batch.get("batchDate"),
                "warehouse": batch.get("warehouseId"),
                "label": f"{batch.get('tradeItemName', 'Unknown Item')} - {available_quantity} pieces"
            })

        return {
            "status": "success",
            "batches": batch_options,
            "total_batches": len(all_batches),
            "todays_batches": len(todays_batches),
            "available_batches": len(batch_options),
            "date_applied": current_date,
            "note": f"Batches with available pieces for EAT today ({current_date})"
        }

    except Exception as e:
        return {"status": "error", "message": "Error fetching batches"}


@frappe.whitelist()
def diagnose_floriday_batches():
    """
    Diagnostic: dump raw shape of /batches responses + sample of newest batches
    so we can see why live stock isn't showing up.
    """
    try:
        settings = frappe.get_single("Floriday Settings")
        API_KEY = settings.api_key
        BASE_URL = settings.base_url
        ACCESS_TOKEN = settings.access_token
        SUPPLIER_ORG_ID = settings.organization_supplier_id

        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "X-Api-Key": API_KEY,
            "Accept": "application/json",
        }
        base_url_clean = BASE_URL.rstrip("/")

        # Walk via the documented sync endpoint: /batches/sync/{seq}?limitResult=1000
        all_batches = []
        page_meta = []
        sequence_from = 0
        for page_num in range(1, 2001):
            endpoint = f"{base_url_clean}/batches/sync/{sequence_from}?limitResult=1000"
            response = requests.get(endpoint, headers=headers, timeout=30)
            page_info = {
                "page": page_num,
                "sequence_from": sequence_from,
                "http_status": response.status_code,
                "items_in_page": 0,
                "max_seq": None,
                "min_seq": None,
                "server_max_seq": None,
            }
            if response.status_code != 200:
                page_info["body_preview"] = response.text[:300]
                page_meta.append(page_info)
                break

            body = response.json() if response.content else {}
            items = body.get("results") or [] if isinstance(body, dict) else []
            server_max = body.get("maximumSequenceNumber") if isinstance(body, dict) else None
            page_info["server_max_seq"] = server_max
            page_info["items_in_page"] = len(items)
            if items:
                seqs = [b.get("sequenceNumber") or 0 for b in items]
                page_info["max_seq"] = max(seqs)
                page_info["min_seq"] = min(seqs)
                all_batches.extend(items)

            page_meta.append(page_info)

            if not items:
                break
            new_max = max(b.get("sequenceNumber") or 0 for b in items)
            if new_max <= sequence_from:
                break  # no progress
            sequence_from = new_max

        # Pick the newest batches by batchDate to inspect their shape
        newest = sort_batches_newest_first(all_batches)[:5]
        sample = []
        for b in newest:
            sample.append({
                "all_keys": sorted(b.keys()),
                "batchId": b.get("batchId"),
                "batchDate": b.get("batchDate"),
                "harvestDate": b.get("harvestDate"),
                "deliveryDate": b.get("deliveryDate"),
                "availableFromDate": b.get("availableFromDate"),
                "expectedAvailableDate": b.get("expectedAvailableDate"),
                "sequenceNumber": b.get("sequenceNumber"),
                "isDeleted": b.get("isDeleted"),
                "tradeItemId": b.get("tradeItemId"),
                "tradeItemName": b.get("tradeItemName"),
                "numberOfPieces": b.get("numberOfPieces"),
                "initialNumberOfPieces": b.get("initialNumberOfPieces"),
                "availableNumberOfPieces": b.get("availableNumberOfPieces"),
                "numberOfPiecesAvailable": b.get("numberOfPiecesAvailable"),
            })

        # Field-presence histogram across ALL fetched batches (not just newest 200)
        field_counts = {}
        for b in all_batches:
            for k in b.keys():
                field_counts[k] = field_counts.get(k, 0) + 1

        # Distinct batchDate values across ALL fetched (top 20 most recent strings)
        date_strings = sorted(
            {b.get("batchDate") for b in all_batches if b.get("batchDate")},
            reverse=True,
        )[:20]

        # Today (EAT) — and how many batches match it under various date fields
        current_date = (datetime.now(timezone.utc) + EAT_OFFSET).strftime('%Y-%m-%d')
        date_field_match_today = {}
        for field in ("batchDate", "harvestDate", "deliveryDate", "availableFromDate", "expectedAvailableDate"):
            count = sum(
                1 for b in all_batches
                if b.get(field) and current_date in str(b.get(field))
            )
            date_field_match_today[field] = count

        return {
            "status": "ok",
            "total_fetched": len(all_batches),
            "pages": page_meta,
            "current_eat_date": current_date,
            "newest_5_sample": sample,
            "field_presence_in_all_batches": field_counts,
            "top_20_distinct_batchDates": date_strings,
            "today_match_count_by_field": date_field_match_today,
        }

    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"}
