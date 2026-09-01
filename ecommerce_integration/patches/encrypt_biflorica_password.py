# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Move the Biflorica Setting password out of `tabSingles` and into `__Auth`.

The field was a plain `Data` field, so the API password sat in cleartext in
`tabSingles` and rendered unmasked on the form. It is now a `Password` field;
this patch encrypts whatever value is already stored and replaces the Singles
row with the usual dummy mask, so the credential moves without anyone having to
re-type it.

The mask is only written once the encrypted value has been read back
successfully. On a site whose `encryption_key` does not match what is already in
`__Auth` — restored without its original site_config.json, or a key that was
regenerated — the round-trip fails, and destroying the cleartext would lose the
password outright. In that case the value is left where it is and the operator
is told to re-enter it.
"""

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

DOCTYPE = "Biflorica Setting"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	value = frappe.db.get_single_value(DOCTYPE, "password")
	if not value:
		return

	# Already migrated: the Singles row holds the '*****' mask, not the password.
	if "".join(set(value)) == "*":
		return

	set_encrypted_password(DOCTYPE, DOCTYPE, value, "password")

	if _read_back() != value:
		# frappe.throw() inside the failed decrypt has queued a message for the
		# client; drop it so it cannot surface on an unrelated request.
		frappe.clear_last_message()
		frappe.log_error(
			f"Could not read back the encrypted {DOCTYPE} password — the site encryption_key "
			"does not match __Auth. Left the value in tabSingles; re-enter it on the form.",
			"Biflorica password migration",
		)
		return

	frappe.db.set_single_value(DOCTYPE, "password", "*" * len(value))


def _read_back():
	try:
		return get_decrypted_password(DOCTYPE, DOCTYPE, "password", raise_exception=False)
	except Exception:
		return None
