// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Allocation", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}

		if (frm.doc.status === "Allocated") {
			// The pick list is the normal next step once stock is reserved, so it leads.
			// Both doctypes belong to the Upande Tambuzi app; the server refuses with a
			// clear message on a site that does not have it.
			frm.add_custom_button(__("Create Pick List"), () => {
				frm.call("create_pick_list").then((r) => {
					if (r && r.message) {
						frappe.set_route("Form", "Order Pick List", r.message);
					}
				});
			}).addClass("btn-primary");

			frm.add_custom_button(__("Create Farm Pack List"), () => {
				frm.call("create_farm_pack_list").then((r) => {
					if (r && r.message) {
						frappe.set_route("Form", "Farm Pack List", r.message);
					}
				});
			});

			// Still available for a delivery that never goes through picking.
			frm.add_custom_button(__("Mark Packed"), () => {
				frm.call("mark_packed").then(() => frm.reload_doc());
			});
		}

		// Only look for a pick list where that app is actually installed.
		if (frappe.boot.user.can_read.includes("Order Pick List")) {
			frappe.db
				.get_list("Order Pick List", {
					filters: { custom_shopify_allocation: frm.doc.name, docstatus: ["<", 2] },
					fields: ["name"],
					limit: 1,
				})
				.then((rows) => {
					if (rows && rows.length) {
						frm.add_custom_button(
							__("Pick List"),
							() => frappe.set_route("Form", "Order Pick List", rows[0].name),
							__("View")
						);
					}
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
