// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Allocation", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}

		if (frm.doc.status === "Allocated") {
			frm.add_custom_button(__("Mark Packed"), () => {
				frm.call("mark_packed").then(() => frm.reload_doc());
			});
		}

		if (frm.doc.status === "Packed") {
			frm.add_custom_button(__("Mark Shipped"), () => {
				frm.call("mark_shipped").then(() => frm.reload_doc());
			});
		}

		if (frm.doc.stock_entry) {
			frm.add_custom_button(
				__("Reservation Entry"),
				() => frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry),
				__("View")
			);
		}
	},

	source_warehouse(frm) {
		// Availability is read per line against the source warehouse, so changing it
		// invalidates what's on screen until the next save.
		if (frm.doc.items && frm.doc.items.length) {
			frm.dirty();
		}
	},
});
