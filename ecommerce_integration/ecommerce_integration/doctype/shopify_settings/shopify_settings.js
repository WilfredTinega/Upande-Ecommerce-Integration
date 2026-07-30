// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Settings", {
	test_connection(frm) {
		frappe.dom.freeze(__("Talking to Shopify..."));
		frm.call("test_connection")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: res.ok ? __("Connected") : __("Connection Failed"),
					indicator: res.ok ? "green" : "red",
					message: frappe.utils.escape_html(res.message || __("No response")),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

	sync_now(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings before syncing."));
			return;
		}
		frappe.dom.freeze(__("Pulling subscription contracts..."));
		frm.call("sync_now")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Sync Complete"),
					message: frappe.utils.escape_html(res.summary || __("No summary returned")),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

	sync_orders_now(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings before syncing."));
			return;
		}
		frappe.dom.freeze(__("Pulling subscription orders..."));
		frm.call("sync_orders_now")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Order Sync Complete"),
					message: frappe.utils.escape_html(res.summary || __("No summary returned")),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

	seed_product_map(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings first."));
			return;
		}
		frappe.dom.freeze(__("Reading products from Shopify..."));
		frm.call("seed_product_map")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Product Map Updated"),
					message:
						frappe.utils.escape_html(res.summary || __("No summary returned")) +
						"<br><br>" +
						__("Review each row: only Box lines are allocated, and each box needs its stem count."),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

	expire_now(frm) {
		frappe.dom.freeze(__("Checking subscription end dates..."));
		frm.call("expire_now")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Expiry Run Complete"),
					message: frappe.utils.escape_html(res.summary || __("No summary returned")),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

	generate_allocations(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings before generating allocations."));
			return;
		}
		frappe.dom.freeze(__("Raising allocations..."));
		frm.call("generate_allocations")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Allocations Generated"),
					message: frappe.utils.escape_html(res.summary || __("No summary returned")),
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},
});
