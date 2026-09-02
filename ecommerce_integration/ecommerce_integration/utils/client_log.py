# Copyright (c) 2026, Upande LTD and contributors
# For license information, please see license.txt

"""Somewhere for the allocation board to put an error a person should not read.

A Frappe traceback is the right thing to keep and the wrong thing to show. The
board used to print the raw `exc` into the page, so a missing Server Script
appeared as three lines of `handler.py` frames and a KeyError — which tells the
operator nothing they can act on and buries the one sentence that does.

So the page now shows a short sentence and sends the detail here, where it lands
in the Error Log with the rest of the site's failures.
"""

import frappe

MAX_DETAIL = 8000
TITLE_PREFIX = "Shopify allocation board"


@frappe.whitelist()
def log_client_error(context: str | None = None, detail: str | None = None):
	"""Record a browser-side failure against the Error Log.

	Whitelisted because the page that hits it runs in the browser, and capped
	because it is reachable by anyone who can open the board: a truncated entry
	is still diagnosable, an unbounded one is a way to fill the table.
	"""
	where = (str(context or "unknown step")).strip()[:100]
	body = (str(detail or "")).strip()[:MAX_DETAIL] or "No detail was sent."

	frappe.log_error(
		title=f"{TITLE_PREFIX}: {where}"[:140],
		message=f"User: {frappe.session.user}\nStep: {where}\n\n{body}",
	)
	return True
