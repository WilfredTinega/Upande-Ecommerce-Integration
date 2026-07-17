import frappe
import requests
from datetime import datetime, timedelta
import json
import math
from frappe.utils import flt

_logger = frappe.logger("biflorica", allow_site=True)


def _clean_farm_code(farm):
    if not farm:
        return farm
    return str(farm).split("(", 1)[0].strip()

@frappe.whitelist()
def post_all_items_to_biflorica(box_type=None, packrate=None, minimum=None):
    try:

        if not frappe.db.exists("Biflorica Setting", "Biflorica Setting"):
            frappe.throw("Biflorica Setting not found. Please create the document first.")

        settings = frappe.get_doc("Biflorica Setting", "Biflorica Setting")
        _logger.info(f"[Biflorica Sync] Starting Biflorica sync for warehouse: {settings.warehouse}")

        required_fields = {
            "warehouse": settings.warehouse,
            "access_token": settings.access_token,
            "base_url": settings.base_url,
            "platform": settings.platform,
            "farm": settings.farm
        }

        missing_fields = [field for field, value in required_fields.items() if not value]
        if missing_fields:
            frappe.throw(f"Missing required fields in Biflorica Setting: {', '.join(missing_fields)}")

        token_valid = validate_access_token(settings)
        if not token_valid:
            frappe.throw("Invalid or expired access token. Please check your Biflorica credentials.")

        items_data = get_enabled_offer_items(settings.warehouse)
        if not items_data:
            _logger.info(f"[Biflorica Sync] No enabled items to offer for warehouse: {settings.warehouse}")
            return {
                "success": True,
                "message": "No enabled items available to create offers.",
                "offers_payload": {"data": [], "countAll": "0"},
                "individual_offers": []
            }

        _logger.info(f"[Biflorica Enabled Items] FOUND {len(items_data)} ENABLED OFFER ROWS:")
        for i, item in enumerate(items_data, 1):
            _logger.info(f"[Biflorica Enabled Items] Item {i}: {item.get('item_code')} - {item.get('item_name')} - Qty: {item.get('actual_qty')} - Price: {item.get('price_per_stem')} - Stem Length: {item.get('stem_length')}")

        _logger.info(f"[Biflorica Sync] Processing {len(items_data)} enabled items")

        offers_payload, individual_offers = prepare_offers_payload_with_details(
            items_data, settings, box_type=box_type, packrate=packrate, minimum=minimum
        )

        _logger.info(f"[Biflorica Payload] FINAL PAYLOAD BEING SENT TO BIFLORICA:")
        _logger.info(f"[Biflorica Payload] {json.dumps(offers_payload, indent=2)}")

        _logger.info(f"[Biflorica Offers Details] INDIVIDUAL OFFERS PAYLOAD DETAILS:")
        for i, offer in enumerate(individual_offers, 1):
            _logger.info(f"[Biflorica Offers Details] Offer {i}: {json.dumps(offer, indent=2)}")

        api_response = post_to_biflorica_api(offers_payload, settings)

        return {
            "api_response": api_response,
            "offers_payload": offers_payload,
            "individual_offers": individual_offers,
            "summary": {
                "total_items_processed": len(items_data),
                "offers_created": len(offers_payload["data"]),
                "items_skipped": len(items_data) - len(offers_payload["data"]),
                "skipped_items": [offer for offer in individual_offers if offer["status"] == "skipped"]
            }
        }

    except Exception as e:
        frappe.log_error(f"Biflorica sync error: {str(e)}", "Biflorica Sync Error")
        frappe.throw(f"Error posting items to Biflorica: {str(e)}")

def validate_access_token(settings):
    try:
        test_endpoint = f"{settings.base_url.rstrip('/')}/auth/verify"
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
            "accept": "application/json"
        }

        response = requests.get(
            test_endpoint,
            headers=headers,
            timeout=15
        )

        if response.status_code == 200:
            _logger.info(f"[Biflorica Auth] Access token validation successful")
            return True
        else:
            frappe.log_error(f"Token validation failed: {response.status_code} - {response.text}", "Biflorica Auth")
            return False

    except Exception as e:
        frappe.log_error(f"Token validation error: {str(e)}", "Biflorica Auth")
        return False

def get_stem_length_from_stock_entry(item_code, warehouse):
    try:
        stock_entries = frappe.get_all(
            "Stock Entry",
            fields=["name", "posting_date", "custom_stem_length"],
            filters={
                "docstatus": 1,
                "purpose": "Material Receipt",
                "items": ["like", f'%{item_code}%']
            },
            order_by="posting_date desc",
            limit=1
        )

        if stock_entries:
            stem_length = stock_entries[0].get("custom_stem_length")
            if stem_length:
                cleaned_length = validate_and_clean_stem_length(stem_length)
                if cleaned_length:
                    _logger.info(f"[Biflorica Stem Length] Found stem length for {item_code} in Stock Entry {stock_entries[0].name}: {stem_length} -> {cleaned_length}")
                    return cleaned_length

        stock_entry_details = frappe.get_all(
            "Stock Entry Detail",
            fields=["parent", "item_code", "custom_stem_length"],
            filters={
                "item_code": item_code,
                "docstatus": 1,
                "t_warehouse": warehouse
            },
            order_by="creation desc",
            limit=1
        )

        if stock_entry_details:
            stem_length = stock_entry_details[0].get("custom_stem_length")
            if stem_length:
                cleaned_length = validate_and_clean_stem_length(stem_length)
                if cleaned_length:
                    _logger.info(f"[Biflorica Stem Length] Found stem length for {item_code} in Stock Entry Detail {stock_entry_details[0].parent}: {stem_length} -> {cleaned_length}")
                    return cleaned_length

        item_stem_length = get_stem_length_from_item_master(item_code)
        if item_stem_length and item_stem_length != "50":
            _logger.info(f"[Biflorica Stem Length] Using stem length from Item master for {item_code}: {item_stem_length}")
            return item_stem_length

        _logger.info(f"[Biflorica Stem Length Warning] No stem length found for {item_code} in Stock Entry or Item master, using default 50")
        return "50"

    except Exception as e:
        frappe.log_error(f"Error fetching stem length for {item_code}: {str(e)}", "Biflorica Stem Length Error")
        return "50"

def get_stem_length_from_item_master(item_code):
    try:
        item = frappe.get_doc("Item", item_code)
        stem_length_fields = [
            'stem_length', 'item_length', 'length',
            'flower_size', 'stem_size', 'size'
        ]

        for field in stem_length_fields:
            stem_length = item.get(field)
            if stem_length:
                cleaned_length = validate_and_clean_stem_length(stem_length)
                if cleaned_length:
                    return cleaned_length
        return "50"
    except:
        return "50"

def validate_and_clean_stem_length(stem_length):
    if not stem_length:
        return None

    stem_str = str(stem_length).strip()

    stem_str = stem_str.replace('cm', '').replace('CM', '').strip()

    try:
        stem_float = float(stem_str)

        if 20 <= stem_float <= 120:
            rounded_length = round_to_nearest_tens(stem_float)
            _logger.info(f"[Biflorica Stem Length Rounding] Rounded stem length {stem_float} to nearest tens: {rounded_length}")
            return str(rounded_length)
        else:
            _logger.info(f"[Biflorica Stem Length Validation] Stem length {stem_float} outside reasonable range (20-120cm)")
            return None
    except ValueError:
        if '-' in stem_str:
            parts = stem_str.split('-')
            try:
                num1 = float(parts[0].strip())
                num2 = float(parts[1].strip())
                if 20 <= num1 <= 120 and 20 <= num2 <= 120:
                    average = (num1 + num2) / 2
                    rounded_length = round_to_nearest_tens(average)
                    _logger.info(f"[Biflorica Stem Length Conversion] Converted stem length range {stem_str} to average: {average} and rounded to: {rounded_length}")
                    return str(rounded_length)
            except:
                pass

        import re
        numbers = re.findall(r'\d+', stem_str)
        if numbers:
            try:
                first_num = float(numbers[0])
                if 20 <= first_num <= 120:
                    rounded_length = round_to_nearest_tens(first_num)
                    _logger.info(f"[Biflorica Stem Length Extraction] Extracted stem length {first_num} from text: {stem_str} and rounded to: {rounded_length}")
                    return str(rounded_length)
            except:
                pass

    return None

def round_to_nearest_tens(number):
    return int(round(number / 10) * 10)

def _biflorica_item_qty_source(warehouse):
    bins = frappe.get_all(
        "Bin",
        fields=["item_code", "actual_qty"],
        filters={"warehouse": warehouse, "actual_qty": [">", 0]},
    )
    return {b["item_code"]: b["actual_qty"] for b in bins}


def get_enabled_offer_items(warehouse=None):
    rows = frappe.db.sql(
        """
        SELECT wip.item_code, slp.stem_length, slp.stock_qty, slp.rate
        FROM `tabStem Length Price` slp
        JOIN `tabWebshop Item Prices` wip ON wip.name = slp.parent
        WHERE slp.parenttype = 'Webshop Item Prices'
          AND slp.enabled = 1
          AND slp.stock_qty > 0
        ORDER BY wip.item_code, slp.stem_length
        """,
        as_dict=True,
    )
    if not rows:
        return []

    item_codes = list({r.item_code for r in rows})
    item_fields = [
        "item_code", "item_name", "item_group", "variant_of",
        "packing", "box_type", "color", "image", "size",
        "characteristics", "stem_length", "item_length", "length",
        "flower_type", "flower_variety", "flower_size", "stem_size",
        "biflorica_type", "biflorica_variety"
    ]
    existing_fields = [f.fieldname for f in frappe.get_meta("Item").fields]
    fetch_fields = [field for field in item_fields if field in existing_fields]
    item_meta = {
        i["item_code"]: i
        for i in frappe.get_all("Item", fields=fetch_fields, filters={"item_code": ["in", item_codes]})
    }

    offer_items = []
    for r in rows:
        base = dict(item_meta.get(r.item_code) or {"item_code": r.item_code, "item_name": r.item_code})
        base["actual_qty"] = flt(r.stock_qty)
        base["stem_length"] = r.stem_length or base.get("stem_length")
        base["price_per_stem"] = flt(r.rate)
        offer_items.append(base)

    return offer_items


def get_warehouse_stock_items(warehouse):
    qty_by_code = _biflorica_item_qty_source(warehouse)
    if not qty_by_code:
        return []

    item_codes = list(qty_by_code.keys())

    item_fields = [
        "item_code", "item_name", "item_group", "variant_of",
        "packing", "box_type", "color", "image", "size",
        "characteristics", "stem_length", "item_length", "length",
        "flower_type", "flower_variety", "flower_size", "stem_size",
        "biflorica_type", "biflorica_variety"
    ]

    existing_fields = [f.fieldname for f in frappe.get_meta("Item").fields]
    fetch_fields = [field for field in item_fields if field in existing_fields]

    items = frappe.get_all("Item", fields=fetch_fields, filters={"item_code": ["in", item_codes]})

    items_with_stock = []
    for item in items:
        item["actual_qty"] = qty_by_code.get(item["item_code"], 0)
        items_with_stock.append(item)

    return items_with_stock

def get_item_price(item_code, price_list="Standard Selling"):
    try:
        price = frappe.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": price_list},
            "price_list_rate"
        )

        if price is None:
            all_prices = frappe.get_all(
                "Item Price",
                fields=["price_list", "price_list_rate"],
                filters={"item_code": item_code}
            )

            if all_prices:
                frappe.log_error(f"Item {item_code} has prices but not in {price_list}: {all_prices}", "Biflorica Price Debug")
                price = all_prices[0].get("price_list_rate")
            else:
                frappe.log_error(f"No prices found for item {item_code} in any price list", "Biflorica Price Debug")

        return float(price or 0)

    except Exception as e:
        frappe.log_error(f"Error getting price for {item_code}: {str(e)}", "Biflorica Price Error")
        return 0

def get_biflorica_flower_type(item):
    return "Rose"

def get_biflorica_flower_variety(item, flower_type):
    if item.get("biflorica_variety"):
        return item.get("biflorica_variety")

    potential_varieties = [
        item.get("flower_variety"),
        item.get("variant_of"),
        item.get("item_name")
    ]

    for potential_variety in potential_varieties:
        if potential_variety:
            clean_variety = str(potential_variety).strip()

            clean_variety = clean_variety.replace(flower_type, "").strip()
            clean_variety = clean_variety.replace("Rose", "").strip()

            for prefix in ["Variety", "Type", "Flower", "Stem"]:
                clean_variety = clean_variety.replace(prefix, "").strip()

            if clean_variety:
                return clean_variety[:50]

    default_varieties = {
        "Rose": "Standard"
    }

    return default_varieties.get(flower_type, "Standard")

def prepare_offers_payload_with_details(items_data, settings, box_type=None, packrate=None, minimum=None):
    offer_duration_days = getattr(settings, 'offer_duration_days', 1)

    box_type = (box_type or "HB").strip() if isinstance(box_type, str) else (box_type or "HB")
    try:
        packrate = int(flt(packrate))
    except (TypeError, ValueError):
        packrate = 0
    if packrate <= 0:
        packrate = 300
    try:
        minimum = int(flt(minimum))
    except (TypeError, ValueError):
        minimum = 0
    if minimum <= 0:
        minimum = 1

    offer_data = []
    individual_offers_details = []

    for item in items_data:
        quantity = item.get("actual_qty", 0)
        price_per_stem = item.get("price_per_stem")
        if price_per_stem is None:
            price_per_stem = get_item_price(item["item_code"])
        price_per_stem = flt(price_per_stem)

        if price_per_stem <= 0:
            frappe.log_error(f"Skipping item {item['item_code']} with zero price. Check Item Price records.", "Biflorica Sync")
            individual_offers_details.append({
                "item_code": item["item_code"],
                "item_name": item.get("item_name"),
                "status": "skipped",
                "reason": "Zero price - no valid price found in Item Price",
                "payload": None,
                "debug_info": {
                    "quantity": quantity,
                    "price_per_stem": price_per_stem,
                    "suggestion": "Check if Item Price exists for this item in Standard Selling price list"
                }
            })
            continue

        if quantity <= 0:
            frappe.log_error(f"Skipping item {item['item_code']} with zero quantity", "Biflorica Sync")
            individual_offers_details.append({
                "item_code": item["item_code"],
                "item_name": item.get("item_name"),
                "status": "skipped",
                "reason": "Zero quantity",
                "payload": None
            })
            continue

        sizes_stems = packrate

        stem_length = validate_and_clean_stem_length(item.get("stem_length"))
        if not stem_length:
            stem_length = get_stem_length_from_stock_entry(item["item_code"], settings.warehouse)

        flower_type = get_biflorica_flower_type(item)
        flower_variety = get_biflorica_flower_variety(item, flower_type)

        characteristics = get_flower_characteristics(item)

        picture_url = get_picture_url(item)

        _logger.info(f"[Biflorica Item Mapping] Processing item: {item['item_code']} - Biflorica Type: {flower_type} - Biflorica Variety: {flower_variety} - Rounded Stem Length: {stem_length} - Price: {price_per_stem} - Packing: {sizes_stems} - BoxType: {box_type} - Minimum: {minimum}")

        offer = {
            "dateStart": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dateEnd": (datetime.now() + timedelta(days=offer_duration_days)).strftime("%Y-%m-%d %H:%M:%S"),
            "platform": settings.platform,
            "farm": _clean_farm_code(settings.farm),
            "type": flower_type,
            "variety": flower_variety,
            "color": item.get("color", "") or "",
            "pictureURL": picture_url,
            "size": stem_length,
            "pricePerStem": str(round(float(price_per_stem), 2)),
            "sizesStems": str(sizes_stems),
            "price": str(round(float(price_per_stem * sizes_stems), 2)),
            "packing": str(sizes_stems),
            "quantity": str(float(quantity)),
            "boxType": box_type,
            "minimum": str(minimum),
            "characteristics": characteristics
        }

        offer = {k: v for k, v in offer.items() if v is not None}

        offer_data.append(offer)

        individual_offer_detail = {
            "item_code": item["item_code"],
            "item_name": item.get("item_name"),
            "status": "ready_to_post",
            "reason": "Successfully mapped",
            "payload": offer,
            "source_data": {
                "original_quantity": quantity,
                "original_price_per_stem": price_per_stem,
                "stem_length_source": "Stock Entry",
                "mapped_flower_type": flower_type,
                "mapped_variety": flower_variety,
                "mapped_stem_length": stem_length,
                "mapped_packing": sizes_stems,
                "mapped_box_type": box_type,
                "mapped_minimum": minimum
            }
        }
        individual_offers_details.append(individual_offer_detail)

    main_payload = {
        "data": offer_data,
        "countAll": str(len(offer_data))
    }

    return main_payload, individual_offers_details

def get_flower_characteristics(item):
    characteristics = []

    item_characteristics = item.get("characteristics")
    if item_characteristics:
        if isinstance(item_characteristics, str):
            try:
                char_list = json.loads(item_characteristics)
                if isinstance(char_list, list):
                    characteristics.extend(char_list)
            except:
                if ',' in item_characteristics:
                    characteristics.extend([c.strip() for c in item_characteristics.split(',')])
                else:
                    characteristics.append(item_characteristics.strip())
        elif isinstance(item_characteristics, list):
            characteristics.extend(item_characteristics)

    if item.get("color"):
        characteristics.append(f"{item['color']} color")

    characteristics = [str(c) for c in characteristics if c]

    return characteristics

def get_picture_url(item):
    image_field = item.get("image")
    if image_field:
        if image_field.startswith(('http://', 'https://')):
            return image_field
        else:
            try:
                site_url = frappe.utils.get_url()
                return f"{site_url}{image_field}"
            except:
                return ""
    return ""

def post_to_biflorica_api(offers_payload, settings):
    endpoint_url = f"{settings.base_url.rstrip('/')}/offers"

    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }

    frappe.log_error(f"Posting {len(offers_payload['data'])} offers to: {endpoint_url}", "Biflorica Sync")

    try:
        response = requests.post(
            endpoint_url,
            json=offers_payload,
            headers=headers,
            timeout=30
        )

        _logger.info(f"[Biflorica API Response] API RESPONSE STATUS: {response.status_code}")
        _logger.info(f"[Biflorica API Response] API RESPONSE BODY: {response.text}")

        if response.status_code in [200, 201]:
            if "not_validate" in response.text or "Not parsed" in response.text:
                error_msg = f"Biflorica validation failed: {response.text}"
                frappe.log_error(error_msg, "Biflorica Validation Error")

                validation_errors = []
                try:
                    errors = json.loads(response.text)
                    for i, error_item in enumerate(errors):
                        if "errors" in error_item:
                            validation_errors.append({
                                "offer_index": i,
                                "errors": error_item['errors']
                            })
                            frappe.log_error(f"Item {i+1} errors: {error_item['errors']}", "Biflorica Validation Details")
                except:
                    pass

                return {
                    "success": False,
                    "message": "Biflorica validation failed. Check error logs for details.",
                    "validation_errors": validation_errors,
                    "api_response": response.text,
                    "status_code": response.status_code
                }
            else:
                _logger.info(f"[Biflorica Sync] Successfully posted {len(offers_payload['data'])} offers to Biflorica")
                return {
                    "success": True,
                    "message": f"Successfully posted {len(offers_payload['data'])} offers to Biflorica",
                    "offers_count": len(offers_payload['data']),
                    "api_response": response.text,
                    "status_code": response.status_code
                }
        else:
            error_msg = f"API Error {response.status_code}: {response.text}"
            frappe.log_error(error_msg, "Biflorica Sync")
            return {
                "success": False,
                "message": error_msg,
                "status_code": response.status_code,
                "api_response": response.text
            }

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {str(e)}"
        frappe.log_error(error_msg, "Biflorica Sync")
        return {
            "success": False,
            "message": error_msg,
            "status_code": None,
            "api_response": None
        }
