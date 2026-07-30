// Copyright (c) 2026, Upande LTD and contributors
// For license information, please see license.txt

frappe.provide("upande.shopify");

// Namespaced rather than a file-scoped const: form scripts can be evaluated more
// than once, and a top-level `const` would then throw a redeclaration error.
upande.shopify = {
	REQUIRED_SCOPES: ["read_orders", "read_products", "read_customers"],

	/** Mirrors REQUIRED_SCOPES in shopify_settings.py. A write_* scope implies its read_*. */
	missing_scopes(frm) {
		const granted = new Set(
			(frm.doc.granted_scopes || "")
				.split(",")
				.map((s) => s.trim())
				.filter(Boolean)
		);
		return this.REQUIRED_SCOPES.filter(
			(s) => !granted.has(s) && !granted.has(s.replace("read_", "write_"))
		);
	},

	/** Green when connected, red when not, grey before the first test.
	 *
	 * Connected is green on its own merit — it means the token is valid. Whether the
	 * token can actually read anything is a separate matter, carried by the scope
	 * warning in the headline rather than by muddying this colour.
	 */
	state(frm) {
		const status = (frm.doc.connection_status || "").trim();
		const connected = status.startsWith("Connected to");
		const scopes = (frm.doc.granted_scopes || "").trim();

		if (!status) {
			return { colour: "gray", label: __("Not tested"), scopes, connected: false };
		}
		if (!connected) {
			return { colour: "red", label: __("Not connected"), scopes, connected: false };
		}
		return { colour: "green", label: __("Connected"), scopes, connected: true };
	},

	show_status(frm) {
		const state = this.state(frm);

		// Tint the status field itself, which is what people actually look at.
		const field = frm.get_field("connection_status");
		if (field && field.$wrapper) {
			field.$wrapper
				.find(".control-value, .like-disabled-input, input")
				.css({ color: `var(--${state.colour}-600)`, "font-weight": 500 });
		}

		const bits = [
			`<span style="color: var(--${state.colour}-600); font-weight: 600;">● ${frappe.utils.escape_html(
				state.label
			)}</span>`,
		];

		if (frm.doc.token_expires_on) {
			// Minutes, not frappe.datetime.get_diff() — that returns whole *days*, so a
			// token 23.9h out came back as 0 and was reported as already expired.
			// Both values are naive system-timezone strings, so parsing both the same
			// way keeps the comparison honest (no UTC/local mixing).
			const minsLeft = moment(frm.doc.token_expires_on).diff(
				moment(frappe.datetime.now_datetime()),
				"minutes"
			);
			if (minsLeft <= 0) {
				bits.push(__("token expired — re-minted on the next call or refresh check"));
			} else if (minsLeft < 120) {
				bits.push(__("token valid ~{0}m", [minsLeft]));
			} else {
				bits.push(__("token valid ~{0}h", [Math.round(minsLeft / 60)]));
			}
		}

		const gaps = this.missing_scopes(frm);
		bits.push(
			gaps.length
				? `<span style="color: var(--orange-600); font-weight: 600;">${__(
						"missing scopes: {0}",
						[frappe.utils.escape_html(gaps.join(", "))]
					)}</span>`
				: __("all required scopes granted")
		);

		frm.dashboard.set_headline(bits.join(" &nbsp;·&nbsp; "));

		// Connected with no scopes is a valid token that can read nothing. The status
		// field stays green, so the caveat gets its own persistent banner.
		if (state.connected && frm.doc.client_id && !state.scopes) {
			frm.dashboard.clear_comment();
			frm.dashboard.add_comment(
				__(
					"Connected, but the token is missing scopes: {0}. Queries needing them return ACCESS_DENIED. Add them to the app's Admin API scopes in Shopify, release the app version, then press Refresh Access Token.",
					[upande.shopify.missing_scopes(frm).join(", ")]
				),
				"orange",
				true
			);
		}
	},
};

frappe.ui.form.on("Shopify Settings", {
	refresh(frm) {
		upande.shopify.show_status(frm);
	},

	connection_status(frm) {
		upande.shopify.show_status(frm);
	},

	view_api_log(frm) {
		frappe.set_route("List", "Shopify API Error Log", { status: "Failed" });
	},

	open_shopify_app(frm) {
		// Scopes are app configuration, not data — no Shopify API can grant them. On the
		// Dev Dashboard they live under Versions (creating a version), NOT under Settings.
		const client = frappe.utils.escape_html(frm.doc.client_id || "");
		frappe.msgprint({
			title: __("How to grant the missing scopes"),
			message: `
				<p>${__("No API can set these — it has to be done in Shopify's Dev Dashboard. It does not need the Shopify CLI.")}</p>
				<ol>
					<li>${__("Open")} <a href="https://dev.shopify.com/dashboard" target="_blank">dev.shopify.com/dashboard</a>
						&rarr; <b>Apps</b> &rarr; <b>Upande Ecommerce Integration</b>
						${client ? `<br><small>${__("Client ID")} <code>${client}</code></small>` : ""}</li>
					<li><b>${__("Versions")}</b> &rarr; <b>${__("Create a version")}</b>
						<br><small>${__("Scopes are set on a version. The Settings page has no scopes field.")}</small></li>
					<li>${__("In the app scopes field add")}
						<code>read_orders</code>, <code>read_products</code>, <code>read_customers</code></li>
					<li>${__("Select")} <b>${__("Release")}</b></li>
					<li>${__("Approve the new scopes in the store admin — a released version is NOT applied to installed stores automatically")}</li>
					<li>${__("Come back and press")} <b>${__("Test Connection")}</b></li>
				</ol>
				<p><small>${__("Watch the two fields here: Requested Scopes fills in once the version is released; Granted Scopes fills in once the store approves.")}</small></p>`,
		});
	},

	run_full_sync(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings before running."));
			return;
		}
		frappe.dom.freeze(__("Running the full Shopify sync..."));
		frm.call("run_full_sync")
			.then((r) => {
				const results = ((r && r.message) || {}).results || [];
				const rows = results
					.map(
						(s) =>
							`<tr><td>${frappe.utils.escape_html(s.step)}</td>` +
							`<td>${s.ok ? "✅" : "❌"}</td>` +
							`<td>${frappe.utils.escape_html(s.detail || "")}</td></tr>`
					)
					.join("");
				frappe.msgprint({
					title: __("Full Sync Complete"),
					message: `<table class="table table-bordered"><thead><tr>
						<th>${__("Step")}</th><th></th><th>${__("Result")}</th>
						</tr></thead><tbody>${rows}</tbody></table>`,
				});
				frm.reload_doc();
			})
			.always(() => frappe.dom.unfreeze());
	},

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

	refresh_token_now(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save Shopify Settings first."));
			return;
		}
		frappe.dom.freeze(__("Requesting a token from Shopify..."));
		frm.call("refresh_token_now")
			.then((r) => {
				const res = (r && r.message) || {};
				frappe.msgprint({
					title: __("Access Token Minted"),
					indicator: "green",
					message:
						frappe.utils.escape_html(res.message || "") +
						"<br><br>" +
						__("Granted scopes: {0}", [
							frappe.utils.escape_html(res.scopes || __("none reported")),
						]),
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
