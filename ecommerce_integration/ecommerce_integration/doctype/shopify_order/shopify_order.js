// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Order", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const expected = Math.max(frm.doc.duration_boxes || 1, 1);
		const raised = frm.doc.allocations_raised || 0;

		if (frm.doc.needs_allocation && raised < expected) {
			const label = raised
				? __("Raise Remaining Allocations ({0} of {1})", [raised, expected])
				: __("Create Allocations");
			frm.add_custom_button(label, () => {
				frappe.dom.freeze(__("Raising allocations..."));
				frm.call("create_allocations")
					.then((r) => {
						const names = (r && r.message) || [];
						if (names.length === 1) {
							frappe.set_route("Form", "Shopify Allocation", names[0]);
						} else {
							frm.reload_doc();
							frappe.msgprint({
								title: __("Allocations Raised"),
								message: __("{0} allocation(s) created.", [names.length]),
							});
						}
					})
					.always(() => frappe.dom.unfreeze());
			}).addClass("btn-primary");
		}

		if (raised) {
			frm.add_custom_button(
				__("Allocations"),
				() =>
					frappe.set_route("List", "Shopify Allocation", {
						shopify_order: frm.doc.name,
					}),
				__("View")
			);
		}

		if (!frm.doc.needs_allocation) {
			frm.dashboard.add_comment(
				__("No box lines on this order — fees and packaging only, so nothing to allocate."),
				"blue",
				true
			);
		}

		if (frm.doc.shopify_subscription) {
			frm.add_custom_button(
				__("Subscription"),
				() =>
					frappe.set_route("Form", "Shopify Subscription", frm.doc.shopify_subscription),
				__("View")
			);
		}
	},
});
