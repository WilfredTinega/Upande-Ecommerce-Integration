// Shared inline "enable stock → webshop" picker, reused by the Webshop
// Settings, Biflorica Setting and Floriday Settings desk forms.
//
// Renders directly on the form (into an HTML field) — no dialog. Every item
// currently on a Shelf (or in the configured warehouses) is listed with a
// checkbox, its available qty, and an editable "Qty to Enable" column. Ticking
// rows and clicking "Enable Selected Stock" PUBLISHES that qty to the storefront
// by flipping the Stem Length Price `enabled` flag and writing its stock_qty —
// no stock is moved. Already-enabled rows stay listed with an "On Webshop"
// checkmark and their published qty pre-filled, so the qty can be edited or the
// row un-published (untick + "Disable Selected") later. See
// webshop_item_prices.set_webshop_enabled_stock / get_webshop_enabled_rows.

frappe.provide("upande_webshop");

upande_webshop.GET_SHELF_ROWS = "upande_webshop.upande_webshop.utils.shelf_transfer.get_shelf_rows";
upande_webshop.GET_WAREHOUSE_ROWS =
	"upande_webshop.upande_webshop.utils.shelf_transfer.get_warehouse_rows";

// Publish endpoints: set the `enabled` flag + published qty on Stem Length Price.
// No stock movement.
upande_webshop.SET_ENABLED_STOCK =
	"upande_webshop.upande_webshop.doctype.webshop_item_prices.webshop_item_prices.set_webshop_enabled_stock";
upande_webshop.GET_ENABLED_ROWS =
	"upande_webshop.upande_webshop.doctype.webshop_item_prices.webshop_item_prices.get_webshop_enabled_rows";

upande_webshop.GET_CUSTOMER_WAREHOUSE_ROWS =
	"upande_webshop.upande_webshop.doctype.webshop_settings.webshop_settings.get_customer_warehouse_rows";

// Render the inline stock picker into `fieldname` on `frm`.
// opts: { frm, channel, fieldname, visible, source, warehouse }
//   channel  — "Biflorica" | "Floriday" (the move method on Enable); "" shows a
//              channel selector in the panel (Webshop Settings).
//   source   — "shelf" (default) reads shelves; "warehouse" reads the configured
//              Webshop warehouses' Bin stock; "customer" reads one specific
//              `warehouse`'s Bin stock (Customer Settings tab). Same row shape +
//              panel + enable/disable flow either way.
//   warehouse — required when source === "customer": the warehouse to list.
//   visible  — false hides the panel.
upande_webshop.render_shelf_move_buttons = function (opts) {
	const { frm, channel, fieldname } = opts;
	const visible = opts.visible !== false;
	const source = opts.source || "shelf";
	const field = frm.get_field(fieldname);
	if (!field || !field.$wrapper) return;

	field.$wrapper.empty();
	if (!visible) return;

	const $root = $('<div class="shelf-stock-panel"></div>').appendTo(field.$wrapper);
	// Persist source + warehouse on the panel so the post-enable/disable re-render
	// (which only has access to $root) can reload the SAME warehouse. Without this
	// a customer-source reload loses its warehouse and the panel blanks out.
	$root.data("source", source);
	$root.data("warehouse", opts.warehouse || "");

	// "customer" source needs a chosen warehouse; prompt instead of loading nothing.
	if (source === "customer" && !opts.warehouse) {
		$root.html(`<p class="text-muted small">${__("Select a warehouse for a customer above to manage their stock.")}</p>`);
		return;
	}

	$root.html(`<p class="text-muted small">${__("Loading stock…")}</p>`);

	let rows_promise;
	if (source === "customer") {
		rows_promise = frappe.xcall(upande_webshop.GET_CUSTOMER_WAREHOUSE_ROWS, {
			warehouse: opts.warehouse,
		});
	} else {
		const method =
			source === "warehouse" ? upande_webshop.GET_WAREHOUSE_ROWS : upande_webshop.GET_SHELF_ROWS;
		rows_promise = frappe.xcall(method);
	}

	// Load the available stock and the already-published ("enabled") rows in
	// parallel, then render once both are in. Enabled rows are merged so a
	// published-but-out-of-stock length stays listed with its "On Webshop" check.
	// For the "customer" source the enabled set is filtered to THIS warehouse's
	// items in _render_shelf_rows, so unrelated published items don't leak in.
	Promise.all([
		rows_promise.catch(() => []),
		frappe.xcall(upande_webshop.GET_ENABLED_ROWS).catch(() => []),
	]).then(([rows, enabled_rows]) => {
		upande_webshop._render_shelf_rows(
			$root,
			frm,
			channel,
			fieldname,
			rows || [],
			source,
			enabled_rows || []
		);
	}).catch(() => {
		$root.html(`<p class="text-muted small">${__("Could not load stock.")}</p>`);
	});
};

upande_webshop._render_shelf_rows = function ($root, frm, channel, fieldname, rows, source, enabled_rows) {
	source = source || "shelf";
	enabled_rows = enabled_rows || [];
	$root.data("source", source);
	// Both "warehouse" and "customer" sources are Bin-backed (one location, no
	// shelf dimension), so they share the warehouse chrome/labels.
	const is_wh = source === "warehouse" || source === "customer";
	const loc_label = is_wh ? __("Warehouse") : __("Shelf");

	// Canonical "<n>cm" key so "52CM"/"52 cm"/"52cm" collapse to one row when
	// matching available stock against published (enabled) rows.
	const canon_len = (s) => {
		const m = String(s || "").match(/\d+/);
		return m ? `${parseInt(m[0], 10)}cm` : "";
	};
	const row_key = (code, len) => `${code} ${canon_len(len)}`;

	// For the customer source, only keep enabled rows whose item is in THIS
	// warehouse, so the global published set (mostly shelf items) doesn't leak in
	// — the checkmark still shows for this warehouse's enabled items.
	const wh_item_codes =
		source === "customer" ? new Set((rows || []).map((r) => r.item_code)) : null;

	// Index published rows by (item_code, length) so matching stock rows get a
	// checkmark and published-but-out-of-stock lengths still show up.
	const enabled_by_key = new Map();
	(enabled_rows || []).forEach((e) => {
		if (wh_item_codes && !wh_item_codes.has(e.item_code)) return;
		enabled_by_key.set(row_key(e.item_code, e.stem_length), {
			item_code: e.item_code,
			item_name: e.item_name || e.item_code,
			stem_length: e.stem_length || "",
			qty: Math.floor(Number(e.stock_qty) || 0),
			bunch_size: Math.max(1, Math.floor(Number(e.bunch_size) || 1)),
		});
	});

	if (!rows.length && !enabled_by_key.size) {
		let empty_msg;
		if (source === "customer") {
			empty_msg = __("No stock in this customer's warehouse.");
		} else if (is_wh) {
			empty_msg = __("No stock in the configured warehouses.");
		} else {
			empty_msg = __("No stock currently on any shelf.");
		}
		$root.html(`<p class="text-muted small">${empty_msg}</p>`);
		return;
	}

	// Combine across shelves: one row per (item_code, stem_length), qty summed over
	// every shelf holding it. The per-shelf breakdown is kept so Enable can draw
	// the chosen qty FIFO across those shelves (the server moves per shelf).
	const combined = new Map(); // key -> {item_code, item_name, stem_length, total, shelves:[{shelf,qty}], enabled, published_qty}
	rows.forEach((r) => {
		const key = row_key(r.item_code, r.stem_length);
		let agg = combined.get(key);
		if (!agg) {
			agg = {
				item_code: r.item_code,
				item_name: r.item_name || r.item_code,
				stem_length: r.stem_length || "",
				total: 0,
				shelves: [],
				enabled: false,
				published_qty: 0,
				// Stems-per-bunch step (from the item's sales UOM, e.g. Bunch(10)→10).
				bunch_size: Math.max(1, Math.floor(Number(r.bunch_size) || 1)),
			};
			combined.set(key, agg);
		}
		const q = Math.floor(Number(r.shelf_qty) || 0);
		agg.total += q;
		if (q > 0) agg.shelves.push({ shelf: r.shelf || "", qty: q });
	});

	// Fold published rows in: flag matches; add a standalone row for any published
	// length that no longer has physical stock so it stays editable/un-publishable.
	enabled_by_key.forEach((e, key) => {
		let agg = combined.get(key);
		if (!agg) {
			agg = {
				item_code: e.item_code,
				item_name: e.item_name,
				stem_length: e.stem_length,
				total: 0,
				shelves: [],
				enabled: false,
				published_qty: 0,
				bunch_size: Math.max(1, Math.floor(Number(e.bunch_size) || 1)),
			};
			combined.set(key, agg);
		}
		agg.enabled = true;
		agg.published_qty = e.qty;
		// Prefer the live source's bunch size, but keep the published row's if the
		// item has since left the shelf (no source row this round).
		if (!agg.bunch_size) {
			agg.bunch_size = Math.max(1, Math.floor(Number(e.bunch_size) || 1));
		}
	});

	// Enabled first, then highest stock; ties broken by name then stem length.
	const combined_rows = Array.from(combined.values()).sort(
		(a, b) =>
			Number(b.enabled) - Number(a.enabled) ||
			b.total - a.total ||
			String(a.item_name).localeCompare(String(b.item_name)) ||
			String(a.stem_length).localeCompare(String(b.stem_length))
	);

	// Hold the per-row shelf breakdown for the Enable allocator (indexed by data-idx).
	$root.data("combined-rows", combined_rows);

	// Snap a qty DOWN to the nearest multiple of `step` (the item's bunch size).
	const snap_down = (v, step) => {
		step = Math.max(1, Math.floor(Number(step) || 1));
		v = Math.floor(Number(v) || 0);
		return Math.floor(v / step) * step;
	};

	const body = combined_rows
		.map((r, i) => {
			const qty = Math.floor(Number(r.total) || 0);
			const step = Math.max(1, Math.floor(Number(r.bunch_size) || 1));
			// Never publish more than is physically available. The ceiling is the
			// available qty snapped down to a whole bunch. For an already-published
			// row whose shelf stock has since gone (qty 0), keep its current
			// published qty as the ceiling so disabling/re-saving it isn't forced to 0.
			const avail_cap = snap_down(qty, step);
			const max_qty = r.enabled
				? Math.max(avail_cap, snap_down(Number(r.published_qty) || 0, step))
				: avail_cap;
			// Pre-fill "Qty to Enable" with the published qty for enabled rows,
			// otherwise the available qty — both snapped to a whole bunch multiple
			// and capped at max_qty so the value is always sellable AND in stock.
			const raw_default = r.enabled ? Math.floor(Number(r.published_qty) || 0) : qty;
			const default_qty = Math.min(snap_down(raw_default, step), max_qty);
			const check_badge = r.enabled
				? `<span class="indicator-pill green shelf-enabled-badge" title="${__("Published to webshop")}">✓</span>`
				: "";
			return `<tr data-idx="${i}" class="shelf-item-row${r.enabled ? " is-enabled" : ""}"
					data-item-code="${frappe.utils.escape_html(r.item_code || "")}"
					data-item-name="${frappe.utils.escape_html(r.item_name || r.item_code || "")}"
					data-stem-length="${frappe.utils.escape_html(r.stem_length || "")}"
					data-enabled="${r.enabled ? 1 : 0}"
					data-bunch="${step}"
					data-max="${max_qty}">
				<td style="width:32px;text-align:center;">
					<input type="checkbox" class="shelf-row-check">
				</td>
				<td>${frappe.utils.escape_html(r.item_name || r.item_code || "")}</td>
				<td>${frappe.utils.escape_html(r.stem_length || "")}</td>
				<td style="text-align:right;">${qty.toLocaleString()}</td>
				<td style="width:130px;">
					<input type="number" class="form-control input-sm shelf-row-qty"
						min="0" step="${step}" max="${max_qty}" value="${default_qty}"
						title="${__("Steps of {0} (one bunch); max {1} available", [step, max_qty.toLocaleString()])}"
						style="text-align:right;height:28px;">
				</td>
				<td style="width:90px;text-align:center;" class="shelf-enabled-cell">${check_badge}</td>
			</tr>`;
		})
		.join("");

	// Distinct varieties (by item_code) + distinct shelves, for the two searchable
	// (typeahead) filter dropdowns built below. Varieties come from combined_rows
	// so published-but-out-of-stock items still appear in the filter; shelves come
	// from the source rows (published-only rows have no shelf).
	const item_choices = Array.from(
		new Map(combined_rows.map((r) => [r.item_code, r.item_name || r.item_code])).entries()
	)
		.map(([code, name]) => ({ value: code, label: name }))
		.sort((a, b) => String(a.label).localeCompare(String(b.label)));

	const shelf_choices = Array.from(new Set(rows.map((r) => r.shelf || "")))
		.filter(Boolean)
		.sort()
		.map((s) => ({ value: s, label: s }));

	$root.html(`
		<div class="d-flex flex-wrap align-items-center mb-2" style="gap:8px;">
			<div class="shelf-filter-item-mount" style="width:220px;"></div>
			<div class="shelf-filter-shelf-mount" style="width:220px;"></div>
		</div>
		<div class="d-flex align-items-center mb-2" style="gap:8px;">
			<label class="mb-0" style="font-weight:normal;cursor:pointer;">
				<input type="checkbox" class="shelf-check-all"> ${__("Select all")}
			</label>
			<span class="text-muted small shelf-row-counter">${__("{0} items", [combined_rows.length])}</span>
			<div class="ml-auto d-flex align-items-center" style="gap:8px;">
				<button class="btn btn-sm btn-default shelf-disable-btn" type="button">
					${__("Disable")}
				</button>
				<button class="btn btn-sm btn-primary shelf-enable-btn" type="button">
					${__("Enable")}
				</button>
			</div>
		</div>
		<div style="max-height:480px;overflow-y:auto;border:1px solid var(--border-color);border-radius:var(--border-radius);">
			<table class="table table-bordered mb-0" style="font-size:12px;">
				<thead style="position:sticky;top:0;background:var(--fg-color);z-index:1;"><tr>
					<th></th>
					<th>${__("Variety")}</th>
					<th>${__("Stem Length")}</th>
					<th style="text-align:right;">${is_wh ? __("Warehouse Qty") : __("Shelf Qty")}</th>
					<th>${__("Qty to Enable")}</th>
					<th style="text-align:center;">${__("On Webshop")}</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table>
		</div>
	`);

	// Clamp typed qty to [0, available]. You can never publish more than is on the
	// shelf (the per-row `data-max` ceiling, already snapped to a whole bunch).
	// Editing the qty auto-ticks the row so the change is captured on the next
	// Enable — otherwise an edit to an already-published row would be silently
	// dropped (the row stays enabled at its old qty until re-ticked + re-Enabled).
	// On commit (blur/change) the value snaps DOWN to a whole bunch multiple so
	// only sellable quantities (e.g. 0,10,20,30 for Bunch(10)) are ever published.
	$root.find(".shelf-row-qty").on("input change", function (ev) {
		const $tr = $(this).closest("tr.shelf-item-row");
		const step = Math.max(1, Math.floor(Number($tr.data("bunch")) || 1));
		const max = Math.max(0, Math.floor(Number($tr.data("max")) || 0));
		let v = Math.floor(Number($(this).val()) || 0);
		if (v < 0) v = 0;
		// Never exceed available stock. Apply live so the field can't show an
		// over-stock figure even mid-typing.
		if (max && v > max) v = max;
		// Snap to the bunch multiple only once editing is committed, so mid-typing
		// keystrokes aren't fought; clamp applies live.
		if (ev.type === "change") v = Math.floor(v / step) * step;
		if (ev.type === "change" || max || this !== document.activeElement) $(this).val(v);
		$tr.find(".shelf-row-check").prop("checked", true);
	});

	$root.find(".shelf-check-all").on("change", function () {
		const checked = $(this).prop("checked");
		// Only toggle rows currently visible under the active filter.
		$root.find(".shelf-item-row:visible .shelf-row-check").prop("checked", checked);
	});

	$root.find(".shelf-enable-btn").on("click", () => {
		upande_webshop._set_selected_enabled($root, frm, channel, fieldname, 1);
	});

	$root.find(".shelf-disable-btn").on("click", () => {
		upande_webshop._set_selected_enabled($root, frm, channel, fieldname, 0);
	});

	// Filters: two searchable (typeahead) dropdowns — Item and Shelf. Built with
	// frappe.ui.form.make_control Autocomplete so the user types to search right
	// inside the dropdown. Blank value = "All". Filtering is client-side over the
	// already-loaded rows; a shelf header hides when none of its items match.
	const apply_filter = () => upande_webshop._apply_shelf_filter($root);

	$root.data("filter-item", "");
	$root.data("filter-shelf", "");

	const item_ctl = frappe.ui.form.make_control({
		parent: $root.find(".shelf-filter-item-mount").get(0),
		df: {
			fieldtype: "Autocomplete",
			fieldname: "shelf_filter_item",
			label: __("Item"),
			placeholder: __("All items"),
			options: item_choices,
			change() {
				$root.data("filter-item", (item_ctl.get_value() || "").trim());
				apply_filter();
			},
		},
		render_input: true,
	});
	item_ctl.refresh();

	const shelf_ctl = frappe.ui.form.make_control({
		parent: $root.find(".shelf-filter-shelf-mount").get(0),
		df: {
			fieldtype: "Autocomplete",
			fieldname: "shelf_filter_shelf",
			label: loc_label,
			placeholder: is_wh ? __("All warehouses") : __("All shelves"),
			options: shelf_choices,
			change() {
				$root.data("filter-shelf", (shelf_ctl.get_value() || "").trim());
				apply_filter();
			},
		},
		render_input: true,
	});
	shelf_ctl.refresh();
};

upande_webshop._apply_shelf_filter = function ($root) {
	// Values come from the two Autocomplete filters. A selection sets the exact
	// item_code / shelf; free-typed text is matched as a substring so partial
	// queries also work.
	const itemVal = String($root.data("filter-item") || "").trim();
	const shelfVal = String($root.data("filter-shelf") || "").trim();
	const itemLc = itemVal.toLowerCase();
	const shelfLc = shelfVal.toLowerCase();
	const combined_rows = $root.data("combined-rows") || [];

	let visible_items = 0;

	$root.find(".shelf-item-row").each(function () {
		const $tr = $(this);
		const i = Number($tr.data("idx"));
		const agg = combined_rows[i] || {};
		const code = String($tr.data("item-code") || "");
		const name = String($tr.data("item-name") || "").toLowerCase();

		const matchItem =
			!itemVal ||
			code === itemVal ||
			name.indexOf(itemLc) !== -1 ||
			code.toLowerCase().indexOf(itemLc) !== -1;
		// Shelf filter checks the row's underlying shelves (combined across them).
		const matchShelf =
			!shelfVal ||
			(agg.shelves || []).some(
				(s) => s.shelf === shelfVal || String(s.shelf).toLowerCase().indexOf(shelfLc) !== -1
			);
		const show = matchItem && matchShelf;

		$tr.toggle(show);
		if (show) visible_items += 1;
	});

	$root.find(".shelf-row-counter").text(__("{0} items", [visible_items]));
};

// Publish (enable=1) or un-publish (enable=0) the ticked rows to the webshop.
// No stock movement: this only sets the Stem Length Price `enabled` flag and,
// when enabling, writes its stock_qty to the "Qty to Enable" value. The row
// stays in the panel afterwards (with / without the "On Webshop" checkmark) so
// the published qty can be edited or the row toggled again.
upande_webshop._set_selected_enabled = function ($root, frm, channel, fieldname, enable) {
	const combined_rows = $root.data("combined-rows") || [];
	const items = [];
	$root.find("tr[data-idx]:visible").each(function () {
		const $tr = $(this);
		if (!$tr.find(".shelf-row-check:checked").length) return;
		const agg = combined_rows[Number($tr.data("idx"))] || {};
		const step = Math.max(1, Math.floor(Number($tr.data("bunch")) || 1));
		const max = Math.max(0, Math.floor(Number($tr.data("max")) || 0));
		// Final guard: cap at available stock, then snap to a whole bunch multiple,
		// so only sellable AND in-stock quantities are published regardless of how
		// the value got into the input.
		let raw = Math.floor(Number($tr.find(".shelf-row-qty").val()) || 0);
		if (max && raw > max) raw = max;
		const qty = Math.floor(raw / step) * step;
		// When enabling, require a qty above zero (that's the number the webshop
		// shows). When disabling, qty is irrelevant.
		if (enable && qty <= 0) return;
		items.push({
			item_code: agg.item_code,
			stem_length: String(agg.stem_length || ""),
			qty,
		});
	});

	if (!items.length) {
		frappe.show_alert({
			message: enable
				? __("Tick at least one row with a qty above zero.")
				: __("Tick at least one row to disable."),
			indicator: "orange",
		});
		return;
	}

	// For the customer source, tell the server which warehouse the stock is from so
	// its cap includes that warehouse (the items aren't in the default shelf/
	// configured-warehouse sets, so otherwise every row caps to 0).
	const enable_args = { items: JSON.stringify(items), enabled: enable ? 1 : 0 };
	if ($root.data("source") === "customer" && $root.data("warehouse")) {
		enable_args.source_warehouse = $root.data("warehouse");
	}

	const $btns = $root.find(".shelf-enable-btn, .shelf-disable-btn").prop("disabled", true);
	frappe.call({
		method: upande_webshop.SET_ENABLED_STOCK,
		args: enable_args,
		freeze: true,
		freeze_message: enable
			? __("Publishing stock to the webshop…")
			: __("Removing stock from the webshop…"),
		callback(r) {
			$btns.prop("disabled", false);
			const updated = (r.message && r.message.updated) || 0;
			const capped = (r.message && r.message.capped) || 0;
			let message;
			let indicator = "green";
			if (enable) {
				message = __("{0} length(s) published to the webshop.", [updated]);
				if (capped) {
					// Server reduced one or more qty to the available stock.
					message += " " + __("{0} capped to available stock.", [capped]);
					indicator = "orange";
				}
			} else {
				message = __("{0} length(s) removed from the webshop.", [updated]);
			}
			frappe.show_alert({ message, indicator }, capped ? 10 : 7);
			// Reload so the checkmark + published qty reflect the new state. Carry the
			// warehouse through so a customer-source panel reloads the same warehouse
			// instead of blanking out.
			upande_webshop.render_shelf_move_buttons({
				frm,
				channel,
				fieldname,
				visible: true,
				source: $root.data("source") || "shelf",
				warehouse: $root.data("warehouse") || null,
			});
		},
		error() {
			$btns.prop("disabled", false);
		},
	});
};
