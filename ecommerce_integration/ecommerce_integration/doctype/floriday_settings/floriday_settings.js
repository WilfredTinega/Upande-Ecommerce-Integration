// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

const PRIMARY_BUTTONS = [
	"sales_order",
	"create_batch",
	"create_supplyine",
	"order_fullfilment",
	"add_items",
	"sync_item_id",
	"add_stock",
	"move_stock",
	"refresh_stock",
	"fetch_warehouses",
];
const WARNING_BUTTONS = ["update_access_token"];

function style_button(frm, fieldname, cls) {
	const $btn = frm.fields_dict[fieldname] && frm.fields_dict[fieldname].$input;
	if (!$btn) return;
	$btn.removeClass("btn-default btn-primary btn-success btn-warning btn-danger");
	$btn.addClass(cls);
}

function start_inline_progress(frm, button_field, label) {
	const $btn_wrapper = frm.fields_dict[button_field] && frm.fields_dict[button_field].$wrapper;
	if (!$btn_wrapper) {
		return start_dashboard_progress(frm, label);
	}

	const bar_id = `floriday-progress-${button_field}`;
	let $bar = $btn_wrapper.next(`#${bar_id}`);
	if ($bar.length === 0) {
		$bar = $(`
			<div id="${bar_id}" class="floriday-progress" style="margin: 8px 0 16px 0; max-width: 600px;">
				<div class="floriday-progress-label" style="margin-bottom: 4px; font-size: var(--text-sm); color: var(--text-muted);"></div>
				<div class="progress" style="height: 8px;">
					<div class="progress-bar progress-bar-success" role="progressbar" style="width: 0%;"></div>
				</div>
			</div>
		`).insertAfter($btn_wrapper);
	}

	const prior = frm._floriday_progress && frm._floriday_progress[button_field];
	if (prior) {
		clearInterval(prior.tick);
		clearTimeout(prior.hide_timer);
	}

	const tracker = { tick: null, hide_timer: null, pct: 0 };
	frm._floriday_progress = frm._floriday_progress || {};
	frm._floriday_progress[button_field] = tracker;

	const render = (pct, status_text) => {
		$bar.find(".progress-bar").css("width", pct + "%");
		$bar.find(".floriday-progress-label").text(`${__(status_text)} ${pct}%`);
		$bar.show();
	};

	render(0, "Running…");
	tracker.tick = setInterval(() => {
		tracker.pct = tracker.pct + Math.max(1, Math.round((95 - tracker.pct) / 12));
		if (tracker.pct > 95) tracker.pct = 95;
		render(tracker.pct, "Running…");
	}, 400);

	return () => {
		clearInterval(tracker.tick);
		render(100, "Done");
		tracker.hide_timer = setTimeout(() => {
			$bar.fadeOut(200);
			if (frm._floriday_progress && frm._floriday_progress[button_field] === tracker) {
				frm._floriday_progress[button_field] = null;
			}
		}, 800);
	};
}

function start_dashboard_progress(frm, label) {
	if (frm.dashboard.hide_progress) frm.dashboard.hide_progress();
	let pct = 0;
	frm.dashboard.show_progress(__(label), 0, `${__("Running…")} 0%`);
	const tick = setInterval(() => {
		pct = pct + Math.max(1, Math.round((95 - pct) / 12));
		if (pct > 95) pct = 95;
		frm.dashboard.show_progress(__(label), pct, `${__("Running…")} ${pct}%`);
	}, 400);
	return () => {
		clearInterval(tick);
		frm.dashboard.show_progress(__(label), 100, `${__("Done")} 100%`);
		setTimeout(() => frm.dashboard.hide_progress && frm.dashboard.hide_progress(), 600);
	};
}

function run_doc_method(frm, button_field, method, label, format_result) {
	const stop_progress = start_inline_progress(frm, button_field, label);
	frm.call({
		method,
		doc: frm.doc,
		callback(r) {
			stop_progress();
			const m = r.message ?? {};
			const { message, indicator } = format_result(m);
			frappe.show_alert({ message, indicator }, 7);
		},
		error() {
			stop_progress();
		},
	});
}

function format_sales_order_result(m) {
	if (m.status === "error") {
		return { message: __("Sync failed: {0}", [m.message || "unknown error"]), indicator: "red" };
	}
	const s = m.summary || {};
	const processed = s.processed || 0;
	const errors = s.errors || 0;
	if (processed === 0 && errors === 0) {
		return { message: __("No new sales orders"), indicator: "blue" };
	}
	const parts = [];
	if (processed) parts.push(__("Created {0} sales order(s)", [processed]));
	if (errors) parts.push(__("{0} error(s)", [errors]));
	return { message: parts.join(", "), indicator: errors ? "orange" : "green" };
}

function format_create_batch_result(m) {
	if (Array.isArray(m)) {
		const success = m.filter((r) => r.status === "success").length;
		const failed = m.filter((r) => r.status === "failed" || r.status === "error").length;
		const no_map = m.filter((r) => r.status === "no_mapping").length;
		if (success === 0 && failed === 0 && no_map === 0) {
			return { message: __("No batches to create"), indicator: "blue" };
		}
		const parts = [];
		if (success) parts.push(__("Created {0} batch(es)", [success]));
		if (failed) parts.push(__("{0} failed", [failed]));
		if (no_map) parts.push(__("{0} unmapped", [no_map]));
		return { message: parts.join(", "), indicator: failed ? "orange" : "green" };
	}
	if (m && m.message) {
		return { message: m.message, indicator: "blue" };
	}
	return { message: __("Done"), indicator: "green" };
}

function format_supplyline_result(m) {
	if (m.status === "error") {
		return { message: __("Failed: {0}", [m.message || "unknown error"]), indicator: "red" };
	}
	if (m.status === "failed") {
		return { message: m.message || __("No supply lines created"), indicator: "orange" };
	}
	const created = m.total_processed || 0;
	const available = m.available_batches || 0;
	if (created === 0) {
		return { message: __("No batches with stock for today"), indicator: "blue" };
	}
	return {
		message: __("Created {0} supply line(s) from {1} batch(es)", [created, available]),
		indicator: "green",
	};
}

function format_order_fullfilment_result(m) {
	if (m.status === "error") {
		return { message: __("Failed: {0}", [m.message || "unknown error"]), indicator: "red" };
	}
	if (m.message) {
		return { message: m.message, indicator: m.status === "success" ? "green" : "blue" };
	}
	return { message: __("Done"), indicator: "green" };
}

// Marking the form clean after we populate the read-only Stock view tables.
// add_child / clear_table dirty the form; the tables are derived from SLE on
// every open so we don't actually need them persisted — keep the form clean.
function _mark_form_clean(frm) {
	frm.doc.__unsaved = 0;
	(frm.doc.stock_items || []).forEach((r) => { r.__unsaved = 0; });
	(frm.doc.table_wtkz || []).forEach((r) => { r.__unsaved = 0; });
	if (typeof frm.refresh_header === "function") frm.refresh_header();
	if (frm.toolbar && typeof frm.toolbar.refresh === "function") frm.toolbar.refresh();
}

function load_system_stock_table(frm) {
	frappe.call({
		method: "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.get_system_floriday_stock",
		callback(r) {
			const rows = r.message || [];
			frm.clear_table("table_wtkz");
			rows.forEach((row) => {
				const child = frm.add_child("table_wtkz");
				Object.assign(child, row);
			});
			frm.refresh_field("table_wtkz");
			_mark_form_clean(frm);
		},
	});
}

function load_stock_table(frm, { silent } = {}) {
	const stop_progress = silent ? () => {} : start_inline_progress(frm, "refresh_stock", "Loading stock");
	frappe.call({
		method: "ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings.get_floriday_stock",
		args: {},
		callback(r) {
			stop_progress();
			const rows = r.message || [];
			frm.clear_table("stock_items");
			rows.forEach((row) => {
				const child = frm.add_child("stock_items");
				Object.assign(child, row);
			});
			frm.refresh_field("stock_items");
			_mark_form_clean(frm);
			if (!silent) {
				frappe.show_alert({
					message: __("Loaded {0} stock row(s)", [rows.length]),
					indicator: rows.length ? "green" : "blue",
				});
			}
		},
		error() {
			stop_progress();
		},
	});
}

frappe.ui.form.on("Floriday Settings", {
	refresh(frm) {
		PRIMARY_BUTTONS.forEach((fn) => style_button(frm, fn, "btn-primary"));
		WARNING_BUTTONS.forEach((fn) => style_button(frm, fn, "btn-warning"));
		// Always reload from SLE on open — saved rows can go stale.
		load_stock_table(frm, { silent: true });
		load_system_stock_table(frm);
		// Auto-render the custom-field status panel so the tab is never blank.
		check_custom_fields(frm, { silent: true });
		// Shelf-mode batch picker.
		load_shelf_stock_panel(frm);
		upande_webshop.render_shelf_move_buttons({
			frm,
			channel: "Floriday",
			fieldname: "shelf_stock_actions",
			visible: !!frm.doc.use_shelf_stock,
		});
		// Recent Changes panel on the Actions tab.
		render_changelog(frm);
	},

	use_shelf_stock(frm) {
		if (frm.doc.use_shelf_stock) {
			load_shelf_stock_panel(frm);
		}
		upande_webshop.render_shelf_move_buttons({
			frm,
			channel: "Floriday",
			fieldname: "shelf_stock_actions",
			visible: !!frm.doc.use_shelf_stock,
		});
	},

	check_custom_fields(frm) {
		check_custom_fields(frm, { silent: false });
	},

	create_missing_custom_fields(frm) {
		create_missing_custom_fields(frm);
	},

	refresh_stock(frm) {
		load_stock_table(frm, { silent: false });
		load_system_stock_table(frm);
	},

	warehouse(frm) {
		if (frm.doc.warehouse) {
			load_stock_table(frm, { silent: true });
			load_system_stock_table(frm);
		}
		render_changelog(frm);
	},

	// Re-render the config-health reminder live as the user fills required fields.
	customer(frm) { render_changelog(frm); },
	company(frm) { render_changelog(frm); },
	business_unit(frm) { render_changelog(frm); },
	sales_order_type(frm) { render_changelog(frm); },
	default_farm(frm) { render_changelog(frm); },

	fetch_warehouses(frm) {
		const stop_progress = start_inline_progress(frm, "fetch_warehouses", "Fetching warehouses");
		frm.call({
			method: "fetch_warehouses",
			doc: frm.doc,
			callback(r) {
				stop_progress();
				const m = r.message || {};
				if (m.status === "success") {
					frappe.show_alert({
						message: __("Loaded {0} warehouse(s)", [m.count || 0]),
						indicator: (m.count || 0) ? "green" : "blue",
					}, 7);
					frm.reload_doc();
				}
			},
			error() {
				stop_progress();
			},
		});
	},

	update_access_token(frm) {
		const stop_progress = start_inline_progress(frm, "update_access_token", "Refreshing access token");
		frm.call({
			method: "update_access_token",
			doc: frm.doc,
			callback(r) {
				stop_progress();
				if (r.message && r.message.status === "success") {
					frappe.show_alert({
						message: __("Access token updated"),
						indicator: "green",
					});
					frm.reload_doc();
				}
			},
			error() {
				stop_progress();
			},
		});
	},

	sales_order(frm) {
		run_doc_method(frm, "sales_order", "sales_order", "Sales Order Sync", format_sales_order_result);
	},

	create_batch(frm) {
		// Batch the items ENABLED on the Stock tab (Stem Length Price.enabled = 1),
		// each at its published qty floored to 200. The server resolves the Floriday
		// trade_item_id per (item, length) and drops rows without a mapping or < 200.
		const stop_progress = start_inline_progress(frm, "create_batch", "Create Batches");
		frappe.call({
			method: `${SHELF_API}.get_floriday_batch_rows`,
			callback(r) {
				const rows = r.message || [];
				if (!rows.length) {
					stop_progress();
					frappe.show_alert({
						message: __(
							"No enabled item has ≥{0} stems with a Floriday mapping. Enable items on the Stock tab first.",
							[BATCH_MULTIPLE]
						),
						indicator: "orange",
					}, 7);
					return;
				}
				frm.call({
					method: "create_batch",
					doc: frm.doc,
					args: { selected_rows: JSON.stringify(rows) },
					callback(res) {
						stop_progress();
						const { message, indicator } = format_create_batch_result(res.message ?? {});
						frappe.show_alert({ message, indicator }, 7);
					},
					error() {
						stop_progress();
					},
				});
			},
			error() {
				stop_progress();
			},
		});
	},

	create_supplyine(frm) {
		run_doc_method(frm, "create_supplyine", "create_supplyine", "Create Supply Lines", format_supplyline_result);
	},

	order_fullfilment(frm) {
		run_doc_method(frm, "order_fullfilment", "order_fullfilment", "Order Fullfilment", format_order_fullfilment_result);
	},

	add_items(frm) {
		const stop_progress = start_inline_progress(frm, "add_items", "Adding system items");
		frappe.call({
			method: "ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items.sync_system_items",
			args: { force: 1 },
			callback(r) {
				stop_progress();
				const m = r.message;
				if (!m) return;
				if (m.skipped) {
					frappe.show_alert({ message: m.reason, indicator: "orange" });
					return;
				}
				const summary = `${m.floriday_docs_created || 0} created, ${m.price_refreshes || 0} prices refreshed${m.skipped ? `, ${m.skipped} errors` : ""}`;
				frappe.show_alert({ message: summary, indicator: "green" }, 7);
			},
			error() {
				stop_progress();
			},
		});
	},

	sync_item_id(frm) {
		const stop_progress = start_inline_progress(frm, "sync_item_id", "Syncing trade item IDs");
		frappe.call({
			method: "ecommerce_integration.ecommerce_integration.doctype.floriday_items.floriday_items.update_trade_item_ids",
			args: { force: 1 },
			callback(r) {
				stop_progress();
				const m = r.message;
				if (!m) return;
				if (m.skipped) {
					frappe.show_alert({ message: m.reason, indicator: "orange" });
					return;
				}
				if (m.error) {
					frappe.show_alert(
						{ message: __("Sync failed: {0}", [String(m.error).slice(0, 200)]), indicator: "red" },
						10
					);
					return;
				}
				const matched = m.rows_matched || 0;
				const total = m.total_rows || 0;
				const unmatched = m.unmatched || [];
				const parts = [__("Matched {0} of {1} rows", [matched, total])];
				if (unmatched.length) {
					const preview = unmatched
						.slice(0, 5)
						.map((u) => `${u.item_code} (${u.stem_length})`)
						.join(", ");
					const more = unmatched.length > 5 ? ` +${unmatched.length - 5} more` : "";
					parts.push(__("Unmapped: {0}{1}", [preview, more]));
				}
				frappe.show_alert(
					{
						message: parts.join(" — "),
						indicator: unmatched.length ? "orange" : "green",
					},
					unmatched.length ? 12 : 7
				);
			},
			error() {
				stop_progress();
			},
		});
	},
});

// ── Custom-field health check ────────────────────────────────────────────
const CUSTOM_FIELDS_API =
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields";

// Cache of the last check result so the Create button knows what to send.
let _floriday_field_status = [];

function render_custom_fields_panel(frm, rows) {
	const wrapper = frm.get_field("custom_fields_status_html");
	if (!wrapper || !wrapper.$wrapper) return;

	if (!rows || !rows.length) {
		wrapper.$wrapper.html(
			`<p class="text-muted">${__("No fields to check.")}</p>`
		);
		return;
	}

	const present = rows.filter((r) => r.present).length;
	const missing = rows.filter((r) => !r.present && !r.doctype_missing).length;
	const dt_missing = rows.filter((r) => r.doctype_missing).length;

	const head = `<div style="margin-bottom:8px;">
		<span class="indicator-pill green">${__("Present")}: ${present}</span>
		<span class="indicator-pill orange">${__("Missing")}: ${missing}</span>
		${dt_missing ? `<span class="indicator-pill red">${__("DocType absent")}: ${dt_missing}</span>` : ""}
	</div>`;

	const body = rows
		.map((r) => {
			let badge, color, disabled;
			if (r.doctype_missing) {
				badge = __("DocType absent");
				color = "red";
				disabled = "disabled";
			} else if (r.present) {
				badge = __("Present");
				color = "green";
				disabled = "disabled";
			} else {
				badge = r.optional ? __("Missing (optional)") : __("Missing");
				color = "orange";
				disabled = "";
			}
			const checked = !r.present && !r.doctype_missing && !r.optional ? "checked" : "";
			return `<tr>
				<td style="width:32px;text-align:center;">
					<input type="checkbox" class="floriday-cf-check"
						data-id="${frappe.utils.escape_html(r.id)}" ${checked} ${disabled}>
				</td>
				<td>${frappe.utils.escape_html(r.dt)}</td>
				<td><code>${frappe.utils.escape_html(r.fieldname)}</code></td>
				<td>${frappe.utils.escape_html(r.fieldtype || "")}</td>
				<td><span class="indicator-pill ${color}">${badge}</span></td>
			</tr>`;
		})
		.join("");

	wrapper.$wrapper.html(`${head}
		<table class="table table-bordered" style="font-size:12px;">
			<thead><tr>
				<th></th><th>${__("DocType")}</th><th>${__("Field")}</th>
				<th>${__("Type")}</th><th>${__("Status")}</th>
			</tr></thead>
			<tbody>${body}</tbody>
		</table>`);
}

function check_custom_fields(frm, { silent } = {}) {
	frappe.call({
		method: `${CUSTOM_FIELDS_API}.check_floriday_custom_fields`,
		callback(r) {
			_floriday_field_status = r.message || [];
			render_custom_fields_panel(frm, _floriday_field_status);
			if (!silent) {
				const missing = _floriday_field_status.filter(
					(x) => !x.present && !x.doctype_missing
				).length;
				frappe.show_alert(
					{
						message: missing
							? __("{0} custom field(s) missing", [missing])
							: __("All Floriday custom fields are present"),
						indicator: missing ? "orange" : "green",
					},
					6
				);
			}
		},
	});
}

// ── Shelf Stock Items picker (shelf mode) ────────────────────────────────
const SHELF_API =
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_settings";

const BATCH_MULTIPLE = 200;

// Read-only preview of what "Create Batch" will post: the items enabled on the
// Stock tab, each at its published qty floored to 200 (rows below 200 or without
// a Floriday mapping are already dropped server-side). Not interactive — the
// batch source is the Stock-tab enabled set, not a per-row tick here.
function render_shelf_stock_panel(frm, rows) {
	const wrapper = frm.get_field("shelf_stock_items");
	if (!wrapper || !wrapper.$wrapper) return;

	if (!rows || !rows.length) {
		wrapper.$wrapper.html(
			`<p class="text-muted">${__(
				"No enabled item has ≥{0} stems with a Floriday mapping. Enable items on the Stock tab first.",
				[BATCH_MULTIPLE]
			)}</p>`
		);
		return;
	}

	const head = `<div style="margin-bottom:8px;color:var(--text-muted);font-size:var(--text-sm);">
		${__("These enabled items will be batched at the qty shown (floored to {0}).", [BATCH_MULTIPLE])}
	</div>`;

	const body = rows
		.map((r) => {
			const qty = Math.floor(Number(r.qty) || 0);
			return `<tr>
				<td>${frappe.utils.escape_html(r.item_name || r.item_code)}</td>
				<td>${frappe.utils.escape_html(r.stem_length || "")}</td>
				<td style="text-align:right;">${qty}</td>
			</tr>`;
		})
		.join("");

	wrapper.$wrapper.html(`${head}
		<table class="table table-bordered" style="font-size:12px;">
			<thead><tr>
				<th>${__("Variety")}</th><th>${__("Stem Length")}</th>
				<th style="text-align:right;">${__("Qty to Batch")}</th>
			</tr></thead>
			<tbody>${body}</tbody>
		</table>`);
}

function load_shelf_stock_panel(frm) {
	frappe.call({
		method: `${SHELF_API}.get_floriday_batch_rows`,
		callback(r) {
			render_shelf_stock_panel(frm, r.message || []);
		},
	});
}

// Recent Changes panel (Actions tab). Newest entry first. Append a new entry at
// the top of CHANGELOG when behaviour changes, so the form documents itself.
const CHANGELOG = [
	{
		date: "2026-06-17",
		title: "Order Fulfillment driven by Floriday delivery orders",
		points: [
			"Fulfillment now reads <code>GET /delivery-orders/sync</code> and matches each Sales Order to its Floriday delivery order. It sends that delivery order's own <b>GLN</b> and <b>package count</b> — fixes the 400 <i>“different country”</i> rejection (we were sending the JKIA GLN instead of the delivery order's).",
			"Orders with no delivery order on Floriday yet are skipped with a clear status instead of a guaranteed-fail POST (that's what the <i>“No delivery orders found”</i> error was).",
			"Also: selects orders by the configured Floriday <b>Customer</b> (not the legacy per-customer <code>custom_floriday_id</code>), and omits empty <code>deliveryRemarks</code> / <code>commercialInvoiceReference</code> / <code>loadCarrierReference</code> so they don't trip Floriday's min-length validation.",
		],
	},
	{
		date: "2026-06-17",
		title: "Floriday Sales Order import is fully config-driven",
		points: [
			"Every imported Floriday order books under the <b>Customer</b> set on the Floriday Setting tab (no more per-organization or <i>Floriday-Default-Customer</i> creation). Fixes the <i>“[Customer …]: default_currency”</i> error that blocked all imports.",
			"The Sales Order <b>Company</b> now comes from the configured Company field (e.g. Karen Roses) — never the stock-entry resolver, which could return Kaitet Group and fail the warehouse-company check.",
			"Mandatory header fields are filled from settings: <b>Business Unit</b>, <b>Sales Order Type</b>, and a <b>Default Farm</b> fallback when the source transfer doesn't resolve one. Nothing is hardcoded — set these on the Floriday Setting tab.",
		],
	},
	{
		date: "2026-06-17",
		title: "Create Batch uses the enabled Stock-tab items",
		points: [
			"“Create Batch” now batches the items enabled on the <b>Stock</b> tab (at their published qty, floored to 200) instead of a separate shelf picker.",
			"The Batch tab shows a read-only preview of exactly what will be posted.",
		],
	},
];

// Required Floriday config fields the Sales Order import + fulfillment depend on.
// `tab` is the tab the field lives on, so the reminder tells the user where to go.
const REQUIRED_CONFIG = [
	{ field: "customer", label: "Customer", tab: "Floriday Setting", why: "every imported Sales Order books under this customer" },
	{ field: "company", label: "Company", tab: "Floriday Setting", why: "Sales Order company; must own the Floriday warehouse" },
	{ field: "business_unit", label: "Business Unit", tab: "Floriday Setting", why: "mandatory on the Sales Order" },
	{ field: "sales_order_type", label: "Sales Order Type", tab: "Floriday Setting", why: "mandatory on the Sales Order" },
	{ field: "default_farm", label: "Default Farm", tab: "Floriday Setting", why: "farm fallback when the source transfer doesn't resolve one" },
	{ field: "warehouse", label: "Warehouse", tab: "Floriday Setting", why: "source warehouse for stock, batches and fulfillment" },
];

function render_config_health(frm) {
	const missing = REQUIRED_CONFIG.filter((c) => {
		const v = frm.doc[c.field];
		return v === undefined || v === null || String(v).trim() === "";
	});

	if (!missing.length) {
		return `<div style="margin-bottom:16px;padding:10px 12px;border:1px solid var(--green-300, #4caf50);
				border-radius:6px;background:var(--green-50, rgba(76,175,80,0.08));">
			<span style="color:var(--green-600, #2e7d32);font-weight:600;">✓ ${__("Floriday configuration complete")}</span>
			<span style="color:var(--text-muted);font-size:var(--text-sm);"> — ${__("all required fields are set.")}</span>
		</div>`;
	}

	const rows = missing.map((c) => `<li style="margin-bottom:6px;">
			<b>${frappe.utils.escape_html(c.label)}</b>
			<span style="color:var(--text-muted);"> — ${frappe.utils.escape_html(c.why)}.</span><br>
			<span style="font-size:var(--text-sm);color:var(--text-muted);">
				${__("Set it on the")} <b>${frappe.utils.escape_html(c.tab)}</b> ${__("tab.")}
			</span>
		</li>`).join("");

	return `<div style="margin-bottom:16px;padding:12px 14px;border:1px solid var(--orange-300, #ffb74d);
			border-radius:6px;background:var(--orange-50, rgba(255,152,0,0.08));max-width:760px;">
		<div style="font-weight:600;color:var(--orange-600, #e65100);margin-bottom:8px;">
			⚠ ${__("Missing Floriday configuration")} (${missing.length})
		</div>
		<div style="font-size:var(--text-sm);color:var(--text-muted);margin-bottom:8px;">
			${__("Sales Order import and fulfillment will fail until these are set:")}
		</div>
		<ul style="margin:0;padding-left:18px;">${rows}</ul>
	</div>`;
}

function render_changelog(frm) {
	const field = frm.get_field("changelog_html");
	if (!field || !field.$wrapper) return;

	const health = render_config_health(frm);

	const body = CHANGELOG.map((e) => {
		const points = (e.points || [])
			.map((p) => `<li style="margin-bottom:4px;">${p}</li>`)
			.join("");
		return `<div style="margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border-color);">
			<div style="font-weight:600;">${frappe.utils.escape_html(e.title)}
				<span style="color:var(--text-muted);font-weight:400;font-size:var(--text-sm);">— ${frappe.utils.escape_html(e.date)}</span>
			</div>
			<ul style="margin:6px 0 0 0;padding-left:18px;font-size:var(--text-sm);color:var(--text-muted);">${points}</ul>
		</div>`;
	}).join("");

	field.$wrapper.html(`${health}<div style="max-width:760px;">${body}</div>`);
}

function create_missing_custom_fields(frm) {
	// Gather ticked, enabled checkboxes from the rendered panel.
	const wrapper = frm.get_field("custom_fields_status_html");
	const ids = [];
	if (wrapper && wrapper.$wrapper) {
		wrapper.$wrapper.find(".floriday-cf-check:checked:not(:disabled)").each(function () {
			ids.push($(this).data("id"));
		});
	}

	if (!ids.length) {
		frappe.msgprint(__("Select at least one missing field to create."));
		return;
	}

	frappe.confirm(
		__("Create {0} custom field(s) on this site?", [ids.length]),
		() => {
			frappe.call({
				method: `${CUSTOM_FIELDS_API}.create_missing_floriday_custom_fields`,
				args: { field_ids: JSON.stringify(ids) },
				freeze: true,
				freeze_message: __("Creating custom fields…"),
				callback(r) {
					const m = (r.message && r.message.summary) || {};
					frappe.show_alert(
						{
							message: __("Created {0}, skipped {1}, errors {2}", [
								m.created || 0,
								m.skipped || 0,
								m.errors || 0,
							]),
							indicator: m.errors ? "red" : "green",
						},
						8
					);
					if (r.message && r.message.errors && r.message.errors.length) {
						frappe.msgprint({
							title: __("Field creation errors"),
							message: r.message.errors
								.map((e) => `<code>${frappe.utils.escape_html(e.id)}</code>: ${frappe.utils.escape_html(e.error)}`)
								.join("<br>"),
							indicator: "red",
						});
					}
					// Re-run the check so the panel reflects the new state.
					check_custom_fields(frm, { silent: true });
				},
			});
		}
	);
}
