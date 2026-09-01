# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Which (item, stem length) combinations this app offers to its sales channels.

Owned by ecommerce_integration. It replaces the pair of upande_webshop tables
(`Webshop Item Prices` + `Stem Length Price.enabled/stock_qty`) the enable/disable
picker used to write to, so the feature works on any site running this app alone.

Only availability lives here — the RATE never does. Prices come from ERPNext
`Item Price` (per length via `custom_length`) and the post-harvest
`Stem Length.price` master, resolved by `utils.stem_length`.
"""

from frappe.model.document import Document


class EcommerceEnabledStock(Document):
	pass
