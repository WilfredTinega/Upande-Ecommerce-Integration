(function () {
	// Stock comes from the app's own `utils.shop_stock.aged_shop_stock`, which
	// carries the rule the live `csr_shop_age` Server Script established: Bin is
	// the source of truth for quantity (it is already net of what was sold, moved
	// or discarded), and age comes from the latest stock entry that put the item
	// into that shop warehouse. Reading the live script directly is what made this
	// page work on exactly one site.
	//
	// `Shopify Allocation Item.qty` is in STEMS - _create_reservation posts it to a
	// Stock Entry with no uom, so it lands in the item's stock UOM. The drawer
	// collects BUNCHES (what a shop actually picks) and writes bunches x factor
	// stems. A bunch is NEVER a stem: Bunch (12) = 12 stems.
	var STOCK = [];
	var ITEMS = {};
	var ALLOCS = [];
	var OPEN = null;
	var STAGE = "all";
	var PICKS = {}; // allocation name -> {name, docstatus} of its Order Pick List
	var TIMER = null;
	var TICK = null;
	var LAST = ""; // when the data on screen was fetched
	var NEXT = 0; // when the next background refresh is due
	var AUTO_MS = 30000; // background refresh cadence
	var REASON = ""; // why the stock list is empty, in the endpoint's own words
	var VIEW = "alloc"; // "alloc" = the work queue, "trace" = what happened after
	var TRACE = {}; // allocation name -> where it has got to down the pipeline
	var TSTAGE = "all"; // which pipeline stage the traceability rail is filtered to
	var LENGTHS = []; // every Stem Length on record, as a fallback for the drawer
	var PACKING = {}; // allocation name -> {percent, complete} off its pack list

	// At most this many bunches of any one variety, so a bouquet is mixed rather
	// than a dozen bunches of whatever happened to be biggest. Relaxed only when
	// every variety is already at the cap and the order is still short.
	var PER_VARIETY = 2;

	var STAGES = [
		{ key: "all", label: "All orders" },
		{ key: "none", label: "Not started" },
		{ key: "progress", label: "In progress" },
		{ key: "ready", label: "Ready to submit" },
	];

	// The pipeline a submitted allocation travels, in order. `dt` is the doctype a
	// step's chip links to, so a stalled order is one click from the document that
	// is holding it up.
	var STEPS = [
		{ key: "alloc", label: "Allocated", dt: "Stock Entry" },
		{ key: "opl", label: "Pick list", dt: "Order Pick List" },
		{ key: "oplsub", label: "Picked", dt: "Order Pick List" },
		{ key: "pack", label: "Packed", dt: "Farm Pack List" },
		{ key: "labels", label: "Box labels", dt: "Box Label" },
		{ key: "dispatch", label: "Dispatched", dt: "Box Label" },
		{ key: "deliver", label: "Delivered", dt: "Box Label" },
	];

	// Named by what the order is WAITING for, not by what it has done: the point of
	// this rail is to find the ones that are stuck.
	var TRACE_STAGES = [
		{ key: "all", label: "All submitted" },
		{ key: "opl", label: "Awaiting pick list" },
		{ key: "oplsub", label: "Awaiting picking" },
		{ key: "pack", label: "Awaiting packing" },
		{ key: "labels", label: "Awaiting box labels" },
		{ key: "dispatch", label: "Awaiting dispatch" },
		{ key: "deliver", label: "Out for delivery" },
		{ key: "done", label: "Delivered" },
	];

	function stageOf(a) {
		if (a.docstatus === 1) return "submitted";
		var req = a.required_stems || 0,
			got = a.total_qty || 0;
		if (req && got >= req) return "ready";
		if (got > 0) return "progress";
		return "none";
	}
	function stageLabel(k) {
		for (var i = 0; i < STAGES.length; i++) if (STAGES[i].key === k) return STAGES[i].label;
		return k;
	}

	function $(id) {
		return document.getElementById(id);
	}
	function esc(s) {
		return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
			return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
		});
	}
	function n(v) {
		return (Math.round((v || 0) * 100) / 100).toLocaleString();
	}
	function shortWh(w) {
		return String(w || "")
			.replace(" Available for Sale - TL", "")
			.replace(" - TL", "");
	}
	function msg(text, kind) {
		var el = $("sb-msg");
		if (!text) {
			el.className = "sb-msg";
			el.innerHTML = "";
			return;
		}
		el.className = "sb-msg " + (kind || "warn");
		el.innerHTML = text;
	}
	// A Frappe failure arrives with three things in it: a sentence meant for a
	// person (`_server_messages`, what frappe.throw was given), an exception type,
	// and a traceback. Only the first is worth showing. The board used to print
	// the traceback into the page, which told the operator nothing and buried the
	// one line that did.
	function briefError(r) {
		if (!r) return "The server did not respond.";

		var sm = r._server_messages;
		if (sm) {
			try {
				var list = JSON.parse(sm);
				var first = typeof list[0] === "string" ? JSON.parse(list[0]) : list[0];
				var text = String((first && (first.message || first)) || "")
					.replace(/<[^>]*>/g, " ")
					.replace(/\s+/g, " ")
					.trim();
				if (text) return text;
			} catch (e) {
				/* not JSON after all; fall through to the type */
			}
		}

		if (r.exc_type === "PermissionError") return "You are not allowed to do that.";
		if (r.exc_type === "DoesNotExistError") return "That record no longer exists.";
		if (r.exc_type === "TimestampMismatchError") {
			return "Someone else changed this since it was opened. Reload and try again.";
		}
		if (r.exc_type) return "The server refused it (" + r.exc_type + ").";
		if (typeof r.message === "string" && r.message.indexOf("Traceback") === -1) {
			return r.message;
		}
		return "Something went wrong on the server.";
	}

	// Toasts rather than a wall of text: a failure should be readable at a glance
	// and get out of the way, with the detail kept where it can be looked up.
	function toast(text, kind) {
		var box = $("sb-toasts");
		if (!box) return;
		var el = document.createElement("div");
		el.className = "tst" + (kind ? " " + kind : "");
		el.innerHTML = text;
		box.appendChild(el);
		setTimeout(
			function () {
				el.className += " out";
				setTimeout(function () {
					if (el.parentNode) el.parentNode.removeChild(el);
				}, 260);
			},
			kind === "err" ? 9000 : 5000,
		);
	}

	// One place every failure goes: a sentence on screen, the whole thing in the
	// Error Log. The log call is fire-and-forget - if even that fails there is
	// nothing useful left to say about it.
	function reportError(context, r) {
		var brief = briefError(r);
		toast("<b>Could not " + esc(context) + ".</b> " + esc(brief), "err");
		var detail = "";
		try {
			detail = JSON.stringify(r, null, 1);
		} catch (e) {
			detail = String(r);
		}
		frappe.call({
			method: "ecommerce_integration.ecommerce_integration.utils.client_log.log_client_error",
			args: { context: context, detail: detail },
			callback: function () {},
			error: function () {},
		});
		return brief;
	}

	function today() {
		var d = new Date();
		d.setHours(0, 0, 0, 0);
		return d;
	}
	function days(s) {
		if (!s) return null;
		return Math.round((new Date(s + "T00:00:00") - today()) / 86400000);
	}

	function factorOf(uom) {
		var m = String(uom || "").match(/[0-9]+/);
		return m ? parseInt(m[0], 10) : null;
	}

	function seq(a) {
		if (!a.delivery_index) return "";
		return a.deliveries_total
			? a.delivery_index + "/" + a.deliveries_total
			: "#" + a.delivery_index;
	}

	function pill(d) {
		if (d === null) return "";
		if (d < 0) return '<span class="sb-pill due">' + -d + "d late</span>";
		if (d <= 7) return '<span class="sb-pill soon">in ' + d + "d</span>";
		return '<span class="sb-pill">in ' + d + "d</span>";
	}

	// ------------------------------------------------------------------ skeleton
	function skeleton(el, rows, cols) {
		var out = '<div class="sk-wrap">';
		for (var r = 0; r < rows; r++) {
			out += '<div class="sk-row">';
			for (var c = 0; c < cols; c++) {
				out +=
					'<span class="sk-bar" style="width:' +
					(c === 0 ? 42 : 10 + ((r + c) % 3) * 4) +
					'%"></span>';
			}
			out += "</div>";
		}
		$(el).innerHTML = out + "</div>";
	}

	function skeletonRail() {
		var out = "";
		for (var i = 0; i < 5; i++) {
			out +=
				"<div class='rail-i sk'><span class='sk-bar' style='width:" +
				(58 + i * 6) +
				"%'></span><span class='sk-bar' style='width:14px'></span></div>";
		}
		$("sb-rail").innerHTML = out;
	}

	function skeletonCards(on) {
		["sb-c-bunch", "sb-c-stems", "sb-c-vars", "sb-c-alloc", "sb-c-short"].forEach(
			function (id) {
				if (on) $(id).innerHTML = '<span class="sk-bar sk-num"></span>';
			},
		);
	}

	// ------------------------------------------------------------------ fetching
	function getList(doctype, args, ok, fail) {
		frappe.call({
			method: "frappe.client.get_list",
			args: Object.assign({ doctype: doctype, limit_page_length: 0 }, args),
			callback: function (r) {
				ok((r && r.message) || []);
			},
			error: function (r) {
				(fail || function () {})(briefError(r), r);
			},
		});
	}

	// `quiet` is the background refresh: the same fetches, but the skeletons stay
	// away and the scroll position is kept, so a page nobody is touching simply
	// keeps its numbers current instead of flashing every 30 seconds.
	function loadAll(quiet) {
		if (!quiet) {
			if (VIEW === "trace") {
				skeleton("sb-trace", 6, 4);
			} else {
				skeleton("sb-stock", 9, 5);
				skeleton("sb-orders", 7, 4);
			}
			skeletonRail();
			skeletonCards(true);
		}
		frappe.call({
			method: "ecommerce_integration.ecommerce_integration.utils.shop_stock.aged_shop_stock",
			type: "GET",
			callback: function (r) {
				var m = (r && r.message) || {};
				STOCK = (m.result || []).filter(function (x) {
					return (x.total || 0) > 0;
				});
				REASON = m.reason || "";
				buildWarehouses();
				loadItems(function () {
					loadLengths(function () {
						loadAllocations(renderAll);
					});
				});
			},
			error: function (r) {
				REASON = reportError("read the shop stock", r);
				if (quiet) return;
				$("sb-stock").innerHTML = '<div class="sb-empty">No data.</div>';
				$("sb-orders").innerHTML = "";
				skeletonCards(false);
				setCards(0, 0, 0);
			},
		});
	}

	function loadItems(done) {
		var codes = [],
			seen = {};
		STOCK.forEach(function (b) {
			if (b.variety && !seen[b.variety]) {
				seen[b.variety] = 1;
				codes.push(b.variety);
			}
		});
		if (!codes.length) {
			done();
			return;
		}
		getList(
			"Item",
			{ filters: [["name", "in", codes]], fields: ["name", "sales_uom", "stock_uom"] },
			function (rows) {
				rows.forEach(function (it) {
					ITEMS[it.name] = {
						uom: it.sales_uom || it.stock_uom,
						factor: factorOf(it.sales_uom),
					};
				});
				done();
			},
			function (d, raw) {
				toast("Selling units unavailable &mdash; quantities are shown in stems.", "warn");
				reportError("read the selling units", raw || { message: d });
				done();
			},
		);
	}

	// The lengths a variety is actually graded to come back on each stock row. This
	// is the fallback for a variety the shop has no graded history for - better to
	// offer every length than to offer none and block the allocation.
	function loadLengths(done) {
		getList(
			"Stem Length",
			{ fields: ["name"], order_by: "name asc" },
			function (rows) {
				LENGTHS = rows.map(function (r) {
					return r.name;
				});
				done();
			},
			function () {
				LENGTHS = [];
				done();
			},
		);
	}

	function loadAllocations(done) {
		getList(
			"Shopify Allocation",
			{
				filters: [["docstatus", "<", 2]],
				fields: [
					"name",
					"status",
					"docstatus",
					"source_warehouse",
					"required_stems",
					"total_qty",
					"delivery_date",
					"shopify_order",
					"delivery_index",
					"deliveries_total",
					"recipient_name",
					"recipient_phone",
					"shipping_address",
					"shipping_city",
					"customer",
					"shopify_subscription",
					"reserve_warehouse",
				],
				order_by: "delivery_date asc",
			},
			function (rows) {
				ALLOCS = rows;
				loadTrace(done);
			},
			function (d, raw) {
				reportError("read the allocations", raw || { message: d });
				ALLOCS = [];
				done();
			},
		);
	}

	// Follows every submitted allocation down the packing chain in three bulk
	// reads: allocation -> Order Pick List -> Farm Pack List -> Box Label.
	//
	// A read that FAILS marks the steps beyond it unknown rather than not-done.
	// The packing doctypes carry their own Custom DocPerms, so somebody who is
	// allowed to see this board is not necessarily allowed to see the pack lists,
	// and printing "not packed" for something we were never allowed to look at
	// would be a lie of exactly the kind this page exists to remove.
	function loadTrace(done) {
		PICKS = {};
		TRACE = {};
		PACKING = {};
		var names = submittedNames();
		if (!names.length) {
			done();
			return;
		}
		names.forEach(function (nm) {
			TRACE[nm] = {
				opl: null,
				oplStatus: -1,
				fpl: null,
				fplStatus: -1,
				labels: 0,
				loaded: 0,
				delivered: 0,
				packPct: 0,
				packDone: false,
				blocked: "",
			};
		});

		getList(
			"Order Pick List",
			{
				filters: [["custom_shopify_allocation", "in", names]],
				fields: ["name", "docstatus", "custom_shopify_allocation"],
			},
			function (picks) {
				var byOpl = {};
				picks.forEach(function (p) {
					var key = p.custom_shopify_allocation;
					if (!PICKS[key] || p.docstatus > PICKS[key].docstatus) PICKS[key] = p;
					var t = TRACE[key];
					if (!t) return;
					if (!t.opl || p.docstatus > t.oplStatus) {
						t.opl = p.name;
						t.oplStatus = p.docstatus;
					}
					byOpl[p.name] = key;
				});
				loadPacks(byOpl, done);
			},
			function (d, raw) {
				blockTrace(names, "Order Pick List");
				reportError("read the pick lists", raw || { message: d });
				done();
			},
		);
	}

	function loadPacks(byOpl, done) {
		var opls = Object.keys(byOpl);
		if (!opls.length) {
			done();
			return;
		}
		getList(
			"Farm Pack List",
			{
				filters: [["custom_order_pick_list", "in", opls]],
				fields: [
					"name",
					"docstatus",
					"custom_order_pick_list",
					"custom_completion_percentage",
					"custom_complete",
				],
			},
			function (packs) {
				var byFpl = {};
				packs.forEach(function (f) {
					var key = byOpl[f.custom_order_pick_list];
					var t = TRACE[key];
					if (!t) return;
					if (!t.fpl || f.docstatus > t.fplStatus) {
						t.fpl = f.name;
						t.fplStatus = f.docstatus;
						t.packPct = Math.round(f.custom_completion_percentage || 0);
						t.packDone = !!(f.custom_complete || t.packPct >= 100);
						PACKING[key] = { percent: t.packPct, complete: t.packDone };
					}
					byFpl[f.name] = key;
				});
				loadLabels(byFpl, done);
			},
			function (d, raw) {
				blockTrace(Object.keys(TRACE), "Farm Pack List");
				reportError("read the pack lists", raw || { message: d });
				done();
			},
		);
	}

	function loadLabels(byFpl, done) {
		var fpls = Object.keys(byFpl);
		if (!fpls.length) {
			done();
			return;
		}
		getList(
			"Box Label",
			{
				filters: [["farm_pack_list_link", "in", fpls]],
				fields: [
					"name",
					"farm_pack_list_link",
					"loaded",
					"loaded_internal_transfer",
					"custom_loaded_wells_fargo",
					"delivered",
				],
			},
			function (labels) {
				labels.forEach(function (b) {
					var t = TRACE[byFpl[b.farm_pack_list_link]];
					if (!t) return;
					t.labels += 1;
					// Loaded on to anything counts as gone: the farm dispatches by
					// its own truck, on an internal transfer, or through Wells Fargo,
					// and each is stamped on a different flag.
					if (b.loaded || b.loaded_internal_transfer || b.custom_loaded_wells_fargo) {
						t.loaded += 1;
					}
					if (b.delivered) t.delivered += 1;
				});
				done();
			},
			function (d, raw) {
				blockTrace(Object.keys(TRACE), "Box Label");
				reportError("read the box labels", raw || { message: d });
				done();
			},
		);
	}

	function submittedNames() {
		return ALLOCS.filter(function (a) {
			return a.docstatus === 1;
		}).map(function (a) {
			return a.name;
		});
	}

	function blockTrace(names, doctype) {
		names.forEach(function (nm) {
			if (TRACE[nm] && !TRACE[nm].blocked) TRACE[nm].blocked = doctype;
		});
	}

	// Run one of the allocation's own whitelisted methods. `run_doc_method` checks
	// the target is whitelisted itself, so nothing here widens what a user may do.
	function docAction(a, method, working) {
		busy(true, working);
		frappe.call({
			method: "run_doc_method",
			args: { dt: "Shopify Allocation", dn: a.name, method: method },
			callback: function (r) {
				var out = r && (r.message || r.docs);
				closeDrawer();
				msg(
					"<b>" +
						esc(a.name) +
						"</b> &mdash; " +
						esc(method.replace(/_/g, " ")) +
						(typeof out === "string" ? ": " + esc(out) : " done") +
						".",
					"ok",
				);
				loadAll(true);
			},
			error: function (e) {
				reportError(method.replace(/_/g, " "), e);
				busy(false);
			},
		});
	}

	// ------------------------------------------------------------------- filters
	function buildWarehouses() {
		var seen = {},
			list = [];
		STOCK.forEach(function (b) {
			if (b.warehouse && !seen[b.warehouse]) {
				seen[b.warehouse] = 1;
				list.push(b.warehouse);
			}
		});
		list.sort();
		var sel = $("sb-wh"),
			prev = sel.value;
		sel.innerHTML = "";
		list.forEach(function (w) {
			var o = document.createElement("option");
			o.value = w;
			o.textContent = shortWh(w);
			sel.appendChild(o);
		});
		var all = document.createElement("option");
		all.value = "__all__";
		all.textContent = "All farm shops";
		sel.appendChild(all);
		sel.value = list.indexOf(prev) > -1 ? prev : list[0] || "__all__";
	}
	function wh() {
		return $("sb-wh").value;
	}
	function inWh(w) {
		return wh() === "__all__" || w === wh();
	}

	// --------------------------------------------------------------------- stock
	function stockRows() {
		var q = ($("sb-var").value || "").toLowerCase().trim();
		return STOCK.filter(function (b) {
			if (!inWh(b.warehouse)) return false;
			if (q && String(b.variety).toLowerCase().indexOf(q) === -1) return false;
			return true;
		})
			.map(function (b) {
				return {
					item: b.variety,
					farm: b.farm,
					warehouse: b.warehouse,
					stems: b.total || 0,
					d4: b.d4 || 0,
					d5: b.d5 || 0,
					d6: b.d6 || 0,
					d7: b.d7 || 0,
				};
			})
			.sort(function (x, y) {
				return (
					String(x.item).localeCompare(String(y.item)) ||
					String(x.warehouse).localeCompare(String(y.warehouse))
				);
			});
	}

	function renderStock() {
		var rows = stockRows();
		if (!rows.length) {
			$("sb-stock").innerHTML =
				'<div class="sb-empty">' +
				(STOCK.length
					? "Nothing aged in " + esc(shortWh(wh())) + "."
					: REASON
						? esc(REASON)
						: "No farm shop holds aged stock. Anything 3 days old or newer is " +
							"excluded here, the same as Available for Sale &gt; Shop.") +
				"</div>";
			setCards(0, 0, 0);
			return;
		}
		var all = wh() === "__all__";
		var body = "",
			tS = 0,
			tB = 0,
			t4 = 0,
			t5 = 0,
			t6 = 0,
			t7 = 0,
			vars = {};
		rows.forEach(function (r) {
			var info = ITEMS[r.item] || {};
			var f = info.factor;
			var bunches = f ? Math.floor(r.stems / f) : null;
			tS += r.stems;
			tB += bunches || 0;
			t4 += r.d4;
			t5 += r.d5;
			t6 += r.d6;
			t7 += r.d7;
			vars[r.item] = 1;
			body +=
				"<tr><td>" +
				esc(r.item) +
				"</td>" +
				(all ? "<td>" + esc(r.farm || shortWh(r.warehouse)) + "</td>" : "") +
				'<td class="q dim">' +
				(r.d4 || "") +
				"</td>" +
				'<td class="q dim">' +
				(r.d5 || "") +
				"</td>" +
				'<td class="q dim">' +
				(r.d6 || "") +
				"</td>" +
				'<td class="q dim">' +
				(r.d7 || "") +
				"</td>" +
				'<td class="q">' +
				n(r.stems) +
				"</td>" +
				'<td class="unit">' +
				(info.uom ? esc(info.uom) : "&mdash;") +
				"</td>" +
				'<td class="q b">' +
				(bunches === null ? "&mdash;" : n(bunches)) +
				"</td></tr>";
		});
		var head =
			"<tr><th>Variety</th>" +
			(all ? "<th>Farm</th>" : "") +
			"<th class='q'>Day 4</th><th class='q'>Day 5</th><th class='q'>Day 6</th>" +
			"<th class='q'>Day 7+</th><th class='q'>Stems</th><th>Unit</th><th class='q'>Bunches</th></tr>";
		var foot =
			"<tr><td colspan='" +
			(all ? 2 : 1) +
			"'>Total</td>" +
			"<td class='q'>" +
			n(t4) +
			"</td><td class='q'>" +
			n(t5) +
			"</td>" +
			"<td class='q'>" +
			n(t6) +
			"</td><td class='q'>" +
			n(t7) +
			"</td>" +
			"<td class='q'>" +
			n(tS) +
			"</td><td></td><td class='q'>" +
			n(tB) +
			"</td></tr>";
		$("sb-stock").innerHTML =
			"<table class='sb'><thead>" +
			head +
			"</thead><tbody>" +
			body +
			"</tbody><tfoot>" +
			foot +
			"</tfoot></table>";
		setCards(tB, tS, Object.keys(vars).length);
	}

	function setCards(bunches, stems, varieties) {
		skeletonCards(false);
		$("sb-c-bunch").textContent = n(bunches);
		$("sb-c-stems").textContent = n(stems);
		$("sb-c-vars").textContent = n(varieties);
		$("sb-c-stems-note").textContent = bunches ? "across " + n(varieties) + " varieties" : "";
	}

	// ---------------------------------------------------------------- allocation
	function inWindow() {
		var win = parseInt($("sb-win").value, 10);
		return ALLOCS.filter(function (a) {
			if (!inWh(a.source_warehouse)) return false;
			if (a.status === "Cancelled") return false;
			if (win) {
				var d = days(a.delivery_date);
				if (d === null || d > win) return false;
			}
			return true;
		});
	}

	// The allocation list is a work queue. Once an allocation is submitted its
	// stock is reserved and there is nothing left to allocate, so it drops out of
	// here and is followed in Order traceability instead.
	function windowAllocs() {
		return inWindow().filter(function (a) {
			return a.docstatus !== 1;
		});
	}

	function submittedAllocs() {
		return inWindow().filter(function (a) {
			return a.docstatus === 1;
		});
	}

	function openAllocs() {
		return windowAllocs().filter(function (a) {
			return STAGE === "all" || stageOf(a) === STAGE;
		});
	}

	function renderRail() {
		var trace = VIEW === "trace";
		var pool = trace ? submittedAllocs() : windowAllocs();
		var defs = trace ? TRACE_STAGES : STAGES;
		var picked = trace ? TSTAGE : STAGE;

		var counts = {};
		pool.forEach(function (a) {
			var k = trace ? traceStage(a) : stageOf(a);
			counts[k] = (counts[k] || 0) + 1;
		});
		counts.all = pool.length;

		$("sb-rail-h").textContent = trace ? "Waiting on" : "Stage";
		$("sb-rail").innerHTML = defs
			.map(function (st) {
				var c = counts[st.key] || 0;
				return (
					"<button class='rail-i" +
					(picked === st.key ? " on" : "") +
					"' data-k='" +
					st.key +
					"'>" +
					"<span class='rl'>" +
					st.label +
					"</span><span class='rc'>" +
					c +
					"</span></button>"
				);
			})
			.join("");
		Array.prototype.forEach.call($("sb-rail").querySelectorAll(".rail-i"), function (b) {
			b.addEventListener("click", function () {
				if (trace) TSTAGE = b.getAttribute("data-k");
				else STAGE = b.getAttribute("data-k");
				closeDrawer();
				renderRail();
				if (trace) renderTrace();
				else renderOrders();
			});
		});
	}

	// ------------------------------------------------------------ traceability
	// What one step of the pipeline can say about itself. `unknown` is deliberately
	// distinct from `todo`: we could not look, so we do not claim.
	function stepState(a, t, step) {
		var blocked = t.blocked;
		var unk = function (from) {
			return blocked === from;
		};

		if (step.key === "alloc") {
			return { on: "done", detail: a.stock_entry || "reserved", link: a.stock_entry };
		}
		if (step.key === "opl") {
			if (unk("Order Pick List")) return { on: "unk", detail: "no access" };
			return t.opl
				? { on: "done", detail: t.opl, link: t.opl }
				: { on: "todo", detail: "not raised" };
		}
		if (step.key === "oplsub") {
			if (unk("Order Pick List")) return { on: "unk", detail: "no access" };
			if (!t.opl) return { on: "todo", detail: "—" };
			return t.oplStatus === 1
				? { on: "done", detail: "submitted", link: t.opl }
				: { on: "part", detail: "still a draft", link: t.opl };
		}
		if (step.key === "pack") {
			if (unk("Order Pick List") || unk("Farm Pack List")) {
				return { on: "unk", detail: "no access" };
			}
			if (!t.fpl) return { on: "todo", detail: "not packed" };
			// Packed means the packhouse finished, not that a pack list exists.
			if (t.packDone) return { on: "done", detail: t.fpl, link: t.fpl };
			return { on: "part", detail: t.packPct + "% packed", link: t.fpl };
		}
		if (blocked) return { on: "unk", detail: "no access" };
		if (step.key === "labels") {
			if (!t.fpl) return { on: "todo", detail: "—" };
			return t.labels
				? { on: "done", detail: t.labels + (t.labels === 1 ? " box" : " boxes") }
				: { on: "todo", detail: "none printed" };
		}
		if (step.key === "dispatch") {
			if (!t.labels) return { on: "todo", detail: "—" };
			if (t.loaded >= t.labels) return { on: "done", detail: "all loaded" };
			return t.loaded
				? { on: "part", detail: t.loaded + " of " + t.labels + " loaded" }
				: { on: "todo", detail: "on the farm" };
		}
		// delivered
		if (!t.labels) return { on: "todo", detail: "—" };
		if (t.delivered >= t.labels) return { on: "done", detail: "all delivered" };
		return t.delivered
			? { on: "part", detail: t.delivered + " of " + t.labels + " delivered" }
			: { on: "todo", detail: "not yet" };
	}

	// The step it is WAITING for - the first one not finished - or "done".
	function traceStage(a) {
		var t = TRACE[a.name];
		if (!t) return "opl";
		for (var i = 0; i < STEPS.length; i++) {
			if (stepState(a, t, STEPS[i]).on !== "done") return STEPS[i].key;
		}
		return "done";
	}

	function docHref(dt, name) {
		return "/app/" + dt.toLowerCase().replace(/ /g, "-") + "/" + encodeURIComponent(name);
	}

	function renderTrace() {
		var rows = submittedAllocs().filter(function (a) {
			return TSTAGE === "all" || traceStage(a) === TSTAGE;
		});

		var all = submittedAllocs();
		var delivered = all.filter(function (a) {
			return traceStage(a) === "done";
		}).length;
		setCard(
			"alloc",
			"In the pipeline",
			n(all.length - delivered),
			"submitted, not yet delivered",
		);
		setCard(
			"short",
			"Delivered",
			n(delivered),
			all.length ? "of " + all.length + " submitted" : "",
		);

		if (!rows.length) {
			$("sb-trace").innerHTML =
				'<div class="sb-empty">' +
				(all.length
					? "Nothing is " +
						esc(traceLabel(TSTAGE).toLowerCase()) +
						" for <b>" +
						esc(shortWh(wh())) +
						"</b>. " +
						all.length +
						" submitted " +
						(all.length === 1 ? "allocation is" : "allocations are") +
						" being tracked."
					: "Nothing submitted yet for <b>" +
						esc(shortWh(wh())) +
						"</b>. Allocate an order and submit it, and it will appear here on its way to delivery.") +
				"</div>";
			return;
		}

		var body = rows
			.map(function (a) {
				var t = TRACE[a.name] || { blocked: "" };
				var done = 0;
				var chips = STEPS.map(function (step) {
					var s = stepState(a, t, step);
					if (s.on === "done") done++;
					var inner =
						"<span class='tk-l'>" +
						esc(step.label) +
						"</span><span class='tk-d'>" +
						esc(s.detail) +
						"</span>";
					if (s.link) {
						return (
							"<a class='tk " +
							s.on +
							"' target='_blank' title='" +
							esc(step.dt + " " + s.link) +
							"' href='" +
							docHref(step.dt, s.link) +
							"'>" +
							inner +
							"</a>"
						);
					}
					return "<span class='tk " + s.on + "'>" + inner + "</span>";
				}).join("");

				var stage = traceStage(a);
				var pct = Math.round((done / STEPS.length) * 100);
				return (
					'<tr class="pick" data-a="' +
					esc(a.name) +
					'">' +
					"<td><div class='ord'>" +
					esc(a.shopify_order || a.name) +
					(seq(a) ? " <span class='sb-pill'>" + esc(seq(a)) + "</span>" : "") +
					"</div><div class='sub'>" +
					esc(a.recipient_name || a.customer || "") +
					"</div></td>" +
					"<td><div>" +
					esc(a.delivery_date || "&mdash;") +
					"</div><div>" +
					pill(days(a.delivery_date)) +
					"</div></td>" +
					"<td class='tw'><div class='tk-rail'>" +
					chips +
					"</div></td>" +
					"<td><span class='sb-pill " +
					(stage === "done" ? "tst-done" : "tst-wait") +
					"'>" +
					esc(stage === "done" ? "Delivered" : traceLabel(stage)) +
					"</span>" +
					"<div class='sub'>" +
					done +
					" of " +
					STEPS.length +
					" &middot; " +
					pct +
					"%</div></td>" +
					"</tr>"
				);
			})
			.join("");

		$("sb-trace").innerHTML =
			(anyBlocked()
				? "<div class='sb-note'>Some steps read <b>no access</b>: the packing doctypes " +
					"have their own permissions, so parts of the chain cannot be shown to you. " +
					"They are left blank rather than reported as not done.</div>"
				: "") +
			"<table class='sb tr'><thead><tr><th>Order</th><th>Delivery</th>" +
			"<th>Pipeline</th><th>Waiting on</th></tr></thead><tbody>" +
			body +
			"</tbody></table>";

		Array.prototype.forEach.call($("sb-trace").querySelectorAll("tr.pick"), function (tr) {
			tr.addEventListener("click", function (e) {
				// A chip is a link to its document; only the rest of the row opens
				// the allocation.
				if (e.target.closest && e.target.closest("a.tk")) return;
				openDrawer(tr.getAttribute("data-a"));
			});
		});
	}

	function anyBlocked() {
		var keys = Object.keys(TRACE);
		for (var i = 0; i < keys.length; i++) if (TRACE[keys[i]].blocked) return true;
		return false;
	}

	function traceLabel(k) {
		for (var i = 0; i < TRACE_STAGES.length; i++) {
			if (TRACE_STAGES[i].key === k) return TRACE_STAGES[i].label;
		}
		return k === "done" ? "Delivered" : k;
	}

	function setCard(which, label, value, note) {
		$("sb-c-" + which + "-k").textContent = label;
		$("sb-c-" + which).textContent = value;
		$("sb-c-" + which + "-note").textContent = note || "";
	}

	function renderTabs() {
		var trace = VIEW === "trace";
		$("sb-tc-alloc").textContent = n(windowAllocs().length);
		$("sb-tc-trace").textContent = n(submittedAllocs().length);
		Array.prototype.forEach.call(document.querySelectorAll(".sb-tab"), function (b) {
			var on = b.getAttribute("data-v") === VIEW;
			b.className = "sb-tab" + (on ? " on" : "");
		});
		$("sb-pane-orders").hidden = trace;
		$("sb-pane-stock").hidden = trace;
		$("sb-pane-trace").hidden = !trace;
		$("sb-var-f").hidden = trace;
		$("sb-card-short").className = "sb-card " + (trace ? "ok" : "bad");
	}

	// Anything landing today or tomorrow, regardless of the filters. A filtered-out
	// order is precisely the one that gets missed, so this reads from every open
	// allocation rather than the visible list, and is rebuilt on each refresh.
	function dueBanner() {
		var due = ALLOCS.filter(function (a) {
			if (a.docstatus === 2 || a.status === "Cancelled") return false;
			var d = days(a.delivery_date);
			return d === 0 || d === 1;
		}).sort(function (x, y) {
			return String(x.delivery_date).localeCompare(String(y.delivery_date));
		});
		if (!due.length) return "";

		var open = due.filter(function (a) {
			return stageOf(a) !== "submitted";
		});
		var items = due
			.map(function (a) {
				var d = days(a.delivery_date);
				var gap = Math.max((a.required_stems || 0) - (a.total_qty || 0), 0);
				return (
					"<button class='due-i' data-a='" +
					esc(a.name) +
					"'>" +
					"<span class='due-w'>" +
					(d === 0 ? "today" : "tomorrow") +
					"</span>" +
					"<b>" +
					esc(a.shopify_order || a.name) +
					"</b>" +
					(seq(a) ? "<span class='sb-pill'>" + esc(seq(a)) + "</span>" : "") +
					"<span class='due-n'>" +
					esc(a.recipient_name || a.customer || "") +
					"</span>" +
					(gap
						? "<span class='sb-short'>" + n(gap) + " stems short</span>"
						: "<span class='sb-cov'>covered</span>") +
					"</button>"
				);
			})
			.join("");

		return (
			"<div class='sb-due'><div class='due-t'>" +
			(open.length
				? "Allocate " +
					(open.length === 1 ? "this order" : "these " + open.length + " orders") +
					" so they can be delivered today or tomorrow"
				: due.length +
					(due.length === 1 ? " order is" : " orders are") +
					" due today or tomorrow &mdash; all allocated") +
			"</div><div class='due-l'>" +
			items +
			"</div></div>"
		);
	}

	function wireDue() {
		Array.prototype.forEach.call(document.querySelectorAll(".due-i"), function (b) {
			b.addEventListener("click", function () {
				openDrawer(b.getAttribute("data-a"));
			});
		});
	}

	function renderOrders() {
		var rows = openAllocs();
		setCard("alloc", "Orders in window", n(rows.length), "awaiting allocation");
		if (!rows.length) {
			$("sb-orders").innerHTML =
				dueBanner() +
				'<div class="sb-empty">' +
				(!ALLOCS.length
					? "No Shopify allocations exist yet. The allocation job on Shopify Settings raises them."
					: "No " +
						esc(stageLabel(STAGE).toLowerCase()) +
						" for <b>" +
						esc(shortWh(wh())) +
						"</b> in this window. " +
						ALLOCS.length +
						" allocations exist &mdash; widen the window or pick another stage.") +
				"</div>";
			setCard("short", "Stems short", "0", "");
			wireDue();
			return;
		}
		var body = "",
			short = 0,
			late = 0;
		rows.forEach(function (a) {
			var req = a.required_stems || 0,
				got = a.total_qty || 0;
			var gap = Math.max(req - got, 0);
			var pct = req ? Math.min(Math.round((got / req) * 100), 100) : 0;
			short += gap;
			var d = days(a.delivery_date);
			if (d !== null && d < 0) late++;
			var st = stageOf(a);
			body +=
				'<tr class="pick" data-a="' +
				esc(a.name) +
				'">' +
				"<td><div class='ord'>" +
				esc(a.shopify_order || a.name) +
				(seq(a) ? " <span class='sb-pill'>" + esc(seq(a)) + "</span>" : "") +
				"</div>" +
				"<div class='sub'>" +
				esc(a.recipient_name || a.customer || "") +
				"</div></td>" +
				"<td><div>" +
				esc(a.delivery_date || "&mdash;") +
				"</div><div>" +
				pill(d) +
				"</div></td>" +
				"<td class='pw'>" +
				"<div class='pr'><div class='pr-f st-" +
				st +
				"' style='width:" +
				pct +
				"%'></div></div>" +
				"<div class='sub'>allocated " +
				n(got) +
				" of " +
				n(req) +
				" stems &middot; " +
				pct +
				"%</div>" +
				"</td>" +
				"<td><span class='sb-pill st-" +
				st +
				"'>" +
				esc(stageLabel(st)) +
				"</span></td>" +
				'<td class="go">&rsaquo;</td></tr>';
		});
		$("sb-orders").innerHTML =
			dueBanner() +
			"<table class='sb'><thead><tr><th>Order</th><th>Delivery</th>" +
			"<th>Allocated</th><th>Stage</th><th></th></tr></thead><tbody>" +
			body +
			"</tbody></table>";
		setCard(
			"short",
			"Stems short",
			n(short),
			late ? late + " already late" : "all within date",
		);

		Array.prototype.forEach.call($("sb-orders").querySelectorAll("tr.pick"), function (tr) {
			tr.addEventListener("click", function () {
				openDrawer(tr.getAttribute("data-a"));
			});
		});
		wireDue();
	}

	// -------------------------------------------------------------------- drawer
	function propose(pool, need) {
		pool.forEach(function (r) {
			r.take = 0;
		});
		var left = need,
			bonus = 0,
			guard = 0;
		while (left > 0 && guard++ < 500) {
			var moved = false;
			for (var i = 0; i < pool.length && left > 0; i++) {
				var r = pool[i];
				if (r.factor > left) continue;
				if (r.take >= Math.min(r.bunches, PER_VARIETY + bonus)) continue;
				r.take += 1;
				left -= r.factor;
				moved = true;
			}
			if (moved) continue;

			var best = null;
			for (var j = 0; j < pool.length; j++) {
				var ceiling = Math.min(pool[j].bunches, PER_VARIETY + bonus);
				if (pool[j].take < ceiling && (!best || pool[j].factor < best.factor))
					best = pool[j];
			}
			if (best) {
				best.take += 1;
				left -= best.factor;
				continue;
			}

			var room = false;
			for (var k = 0; k < pool.length; k++) if (pool[k].take < pool[k].bunches) room = true;
			if (!room) break;
			bonus += 1;
		}
		return pool;
	}

	// One length per line, never derived: a variety in one farm shop routinely
	// holds several lengths at once (170 of 231 holdings on this farm do), so
	// which one is being packed is a decision, not a lookup.
	function lenSelect(r) {
		var opts = r.offer
			.map(function (L) {
				return (
					"<option value='" +
					esc(L) +
					"'" +
					(L === r.len ? " selected" : "") +
					">" +
					esc(L) +
					"</option>"
				);
			})
			.join("");
		return (
			"<select class='len' data-i='" +
			esc(r.item) +
			"'>" +
			(r.len ? "" : "<option value='' selected>&mdash;</option>") +
			opts +
			"</select>"
		);
	}

	// Bouquets are normally cut to one length, so setting all of them at once is
	// the common case. A variety that is not graded to that length is left alone
	// rather than being silently given a length it has never been cut to.
	function lenSetAll(pool) {
		var all = {};
		pool.forEach(function (r) {
			r.offer.forEach(function (L) {
				all[L] = (all[L] || 0) + 1;
			});
		});
		var keys = Object.keys(all).sort();
		if (keys.length < 2) return "";
		return (
			"<div class='dr-setall'><span>Set every line to</span>" +
			keys
				.map(function (L) {
					return (
						"<button class='sb-btn sec tiny' data-l='" +
						esc(L) +
						"'>" +
						esc(L) +
						"<span class='cnt'>" +
						all[L] +
						"</span></button>"
					);
				})
				.join("") +
			"</div>"
		);
	}

	function openDrawer(name) {
		var a = ALLOCS.filter(function (x) {
			return x.name === name;
		})[0];
		if (!a) return;
		OPEN = a;
		stamp();
		if ((a.total_qty || 0) > 0) {
			frappe.call({
				method: "frappe.client.get",
				args: { doctype: "Shopify Allocation", name: a.name },
				callback: function (r) {
					renderDrawer(a, ((r && r.message) || {}).items || []);
				},
				error: function () {
					renderDrawer(a, []);
				},
			});
			return;
		}
		renderDrawer(a, []);
	}

	function renderDrawer(a, existing) {
		var need = a.required_stems || 0;
		// Submitted means the stock is already reserved against these exact lines,
		// so there is nothing left to choose: the varieties, lengths and quantities
		// are shown as a record. Changing one here would disagree with the Stock
		// Entry that moved the stems and with the pick list the packhouse holds.
		var locked = a.docstatus === 1;
		var held = {};
		var heldLen = {};
		(existing || []).forEach(function (row) {
			held[row.item_code] = (held[row.item_code] || 0) + (row.qty || 0);
			if (row.stem_length) heldLen[row.item_code] = row.stem_length;
		});

		var pool = STOCK.filter(function (b) {
			return b.warehouse === a.source_warehouse;
		})
			.map(function (b) {
				var f = (ITEMS[b.variety] || {}).factor || 1;
				return {
					item: b.variety,
					uom: (ITEMS[b.variety] || {}).uom || "",
					factor: f,
					stems: b.total || 0,
					bunches: Math.floor((b.total || 0) / f),
					lengths: b.lengths || [],
				};
			})
			.filter(function (r) {
				return r.bunches > 0 || held[r.item];
			})
			.sort(function (x, y) {
				return y.stems - x.stems;
			});

		Object.keys(held).forEach(function (code) {
			if (
				pool.filter(function (r) {
					return r.item === code;
				}).length
			)
				return;
			var f = (ITEMS[code] || {}).factor || 1;
			pool.push({
				item: code,
				uom: (ITEMS[code] || {}).uom || "",
				factor: f,
				stems: 0,
				bunches: 0,
				lengths: [],
				gone: true,
			});
		});

		// Offer what the shop has graded this variety to; fall back to every length
		// on record. The saved choice wins, then the most recently graded one.
		pool.forEach(function (r) {
			r.offer = (r.lengths && r.lengths.length ? r.lengths : LENGTHS).slice();
			var want = heldLen[r.item] || r.offer[0] || "";
			if (want && r.offer.indexOf(want) === -1) r.offer.unshift(want);
			r.len = want;
		});

		if (Object.keys(held).length) {
			pool.forEach(function (r) {
				r.take = Math.round((held[r.item] || 0) / r.factor);
			});
		} else {
			propose(pool, need);
		}

		if (locked) {
			// Everything else on the shelf is beside the point once it is reserved.
			pool = pool.filter(function (r) {
				return r.take > 0;
			});
		}

		var rows = pool
			.map(function (r) {
				return (
					"<tr><td><div class='ord'>" +
					esc(r.item) +
					"</div>" +
					"<div class='sub'>" +
					esc(r.uom) +
					" = " +
					r.factor +
					" stems" +
					(r.gone ? " &middot; <span class='sb-short'>off the shelf</span>" : "") +
					"</div></td>" +
					'<td class="q">' +
					n(r.bunches) +
					"<div class='sub'>" +
					n(r.stems) +
					" stems</div></td>" +
					"<td>" +
					(locked
						? "<span class='dr-ro'>" + esc(r.len || "—") + "</span>"
						: lenSelect(r)) +
					"</td>" +
					(locked
						? "<td class='q'><span class='dr-ro'>" + n(r.take) + "</span></td>"
						: '<td class="q"><input class="qty" type="number" min="0" step="1" max="' +
							Math.max(r.bunches, r.take || 0) +
							'" value="' +
							r.take +
							'" data-i="' +
							esc(r.item) +
							'" data-f="' +
							r.factor +
							'"></td>') +
					'<td class="q stems" data-s="' +
					esc(r.item) +
					'">' +
					n(r.take * r.factor) +
					"</td></tr>"
				);
			})
			.join("");

		var d = days(a.delivery_date);
		var cell = function (k, v, full) {
			if (!v) return "";
			return (
				"<div" +
				(full ? " style='grid-column:1/-1'" : "") +
				"><span class='k'>" +
				k +
				"</span><span class='v' style='white-space:normal'>" +
				v +
				"</span></div>"
			);
		};
		$("sb-drawer").innerHTML =
			"<div class='dr-head'>" +
			"<div><div class='dr-t'>" +
			esc(a.shopify_order || a.name) +
			(seq(a) ? " <span class='sb-pill'>" + esc(seq(a)) + "</span>" : "") +
			"</div>" +
			"<div class='sub'>" +
			esc(a.recipient_name || a.customer || "") +
			"</div></div>" +
			"<button class='dr-x' id='dr-close' aria-label='Close'>&times;</button>" +
			"</div>" +
			"<div class='dr-meta'>" +
			cell("Delivery", esc(a.delivery_date || "?") + " " + pill(d)) +
			cell("Delivery no.", seq(a) ? esc(seq(a)) : "") +
			cell(
				"Subscriber",
				esc(a.recipient_name || "") +
					(a.recipient_phone
						? "<div class='sub'>" + esc(a.recipient_phone) + "</div>"
						: ""),
			) +
			cell("Billing customer", esc(a.customer || "")) +
			cell("Subscription", esc(a.shopify_subscription || "")) +
			cell(
				"Stage",
				"<span class='sb-pill st-" +
					stageOf(a) +
					"'>" +
					esc(stageLabel(stageOf(a))) +
					"</span>",
			) +
			cell("Shop", esc(shortWh(a.source_warehouse))) +
			cell("Reserve to", esc(shortWh(a.reserve_warehouse || ""))) +
			cell(
				"Ship to",
				[a.shipping_address, a.shipping_city].filter(Boolean).map(esc).join(", "),
				true,
			) +
			cell("Needs", n(need) + " stems") +
			"</div>" +
			"<div class='dr-bar'><div class='dr-fill' id='dr-fill'></div></div>" +
			"<div class='dr-count' id='dr-count'></div>" +
			"<div class='dr-warn' id='dr-warn'></div>" +
			(pool.length
				? (locked ? "" : lenSetAll(pool)) +
					"<div class='dr-scroll'><table class='sb'><thead><tr><th>Variety</th>" +
					"<th class='q'>On hand</th><th>Length</th>" +
					"<th class='q'>" +
					(locked ? "Bunches" : "Allocate") +
					"</th><th class='q'>Stems</th>" +
					"</tr></thead><tbody>" +
					rows +
					"</tbody></table></div>"
				: "<div class='sb-empty'>Nothing aged in " +
					esc(shortWh(a.source_warehouse)) +
					" to allocate.</div>") +
			"<div class='dr-foot'>" +
			(a.docstatus === 1
				? pipelineButtons(a)
				: "<button class='sb-btn sec' id='dr-save'" +
					(pool.length ? "" : " disabled") +
					">Save draft</button>" +
					"<button class='sb-btn' id='dr-submit'" +
					(pool.length ? "" : " disabled") +
					">Submit &amp; reserve</button>") +
			"<a class='sb-btn sec' target='_blank' href='/app/shopify-allocation/" +
			encodeURIComponent(a.name) +
			"'>Open document</a>" +
			"</div>";

		document.body.classList.add("dr-open");
		$("dr-close").addEventListener("click", closeDrawer);
		Array.prototype.forEach.call($("sb-drawer").querySelectorAll("input.qty"), function (i) {
			i.addEventListener("input", function () {
				clampQty(i, need);
			});
		});
		Array.prototype.forEach.call(
			$("sb-drawer").querySelectorAll("select.len"),
			function (sel) {
				sel.addEventListener("change", function () {
					warn("");
				});
			},
		);
		Array.prototype.forEach.call(
			$("sb-drawer").querySelectorAll(".dr-setall button"),
			function (b) {
				b.addEventListener("click", function () {
					var want = b.getAttribute("data-l");
					var skipped = 0;
					Array.prototype.forEach.call(
						$("sb-drawer").querySelectorAll("select.len"),
						function (sel) {
							var has = false;
							Array.prototype.forEach.call(sel.options, function (o) {
								if (o.value === want) has = true;
							});
							if (has) sel.value = want;
							else if (sel.parentNode) skipped++;
						},
					);
					warn(
						skipped
							? "Set to <b>" +
									esc(want) +
									"</b>, except " +
									skipped +
									(skipped === 1 ? " variety" : " varieties") +
									" the shop has never graded to that length."
							: "",
					);
				});
			},
		);
		if (a.docstatus === 1) {
			wirePipeline(a);
		} else if (pool.length) {
			$("dr-save").addEventListener("click", function () {
				persist(a, false);
			});
			$("dr-submit").addEventListener("click", function () {
				persist(a, true);
			});
		}
		tally(need);
	}

	function pipelineButtons(a) {
		var pick = PICKS[a.name];
		var out = "<span class='dr-done'>" + esc(a.status || "Submitted") + "</span>";

		if (pick) {
			out +=
				"<a class='sb-btn sec' target='_blank' href='/app/order-pick-list/" +
				encodeURIComponent(pick.name) +
				"'>Pick list " +
				(pick.docstatus === 1 ? "(submitted)" : "(draft)") +
				"</a>";
		} else {
			out += "<button class='sb-btn sec' id='dr-pick'>Raise pick list</button>";
		}

		if (pick && pick.docstatus === 1 && a.status === "Allocated") {
			out += "<button class='sb-btn' id='dr-pack'>Pack</button>";
		}
		// Packed is READ ONLY. It is what the pack list says - its own
		// completion percentage, kept up to date as boxes are filled - so there is
		// nothing here to click. Anything else would let an order be declared
		// packed while the packhouse was still filling it.
		var pk = PACKING[a.name] || { percent: 0, complete: false };
		if (pick) {
			out +=
				"<span class='dr-pack" +
				(pk.complete ? " done" : "") +
				"'>" +
				(pk.complete ? "Packed" : "Packing " + Math.round(pk.percent || 0) + "%") +
				"</span>";
		}
		if (pk.complete && a.status !== "Shipped") {
			out += "<button class='sb-btn' id='dr-shipped'>Dispatch</button>";
		}
		if (a.status !== "Cancelled") {
			out +=
				"<button class='sb-btn danger' id='dr-cancel'>Cancel &amp; return stock</button>";
		}
		return out;
	}

	function wirePipeline(a) {
		[
			["dr-pick", "create_pick_list", "Raising…"],
			["dr-pack", "create_farm_pack_list", "Packing…"],
			["dr-shipped", "mark_shipped", "Dispatching…"],
		].forEach(function (t) {
			if ($(t[0]))
				$(t[0]).addEventListener("click", function () {
					docAction(a, t[1], t[2]);
				});
		});
		if ($("dr-cancel")) {
			$("dr-cancel").addEventListener("click", function () {
				cancelAllocation(a);
			});
		}
	}

	// Cancelling IS the reversal: it cancels the reservation Stock Entry, which
	// writes the opposite ledger entries and so puts the stems back in the shop
	// they were taken from, and takes the pick list down with it.
	function cancelAllocation(a) {
		confirmModal(
			"Cancel this allocation and return the stock?",
			"<p><b>" +
				esc(a.shopify_order || a.name) +
				"</b> &middot; " +
				n(a.total_qty) +
				" stems reserved.</p>" +
				"<ul><li>Returns them to <b>" +
				esc(shortWh(a.source_warehouse)) +
				"</b> by cancelling the reservation Stock Entry.</li>" +
				"<li>Takes down the pick list raised for this allocation.</li></ul>" +
				"<p class='md-note'>A pack list that has already been submitted will block " +
				"this &mdash; cancel that first.</p>",
			function () {
				busy(true, null);
				frappe.call({
					method: "frappe.client.cancel",
					args: { doctype: "Shopify Allocation", name: a.name },
					callback: function () {
						closeDrawer();
						msg(
							"Cancelled <b>" +
								esc(a.name) +
								"</b> &mdash; the stems are back in <b>" +
								esc(shortWh(a.source_warehouse)) +
								"</b>.",
							"ok",
						);
						loadAll(true);
					},
					error: function (e) {
						reportError("cancel the allocation", e);
						busy(false);
					},
				});
			},
			"Yes, cancel it",
		);
	}

	// Yes/No confirmation. window.confirm is browser chrome that cannot be styled
	// and reads like a security prompt; these actions move real stock, so the
	// question is asked in the page.
	function confirmModal(title, bodyHtml, onYes, yesLabel) {
		var box = $("sb-modal");
		box.innerHTML =
			"<div class='md-card'>" +
			"<div class='md-t'>" +
			title +
			"</div>" +
			"<div class='md-b'>" +
			bodyHtml +
			"</div>" +
			"<div class='md-f'>" +
			"<button class='sb-btn sec' id='md-no'>No</button>" +
			"<button class='sb-btn' id='md-yes'>" +
			(yesLabel || "Yes, submit") +
			"</button>" +
			"</div>" +
			"</div>";
		box.className = "sb-modal on";
		var close = function () {
			box.className = "sb-modal";
			box.innerHTML = "";
		};
		$("md-no").addEventListener("click", close);
		$("md-yes").addEventListener("click", function () {
			close();
			onYes();
		});
		box.addEventListener("click", function (e) {
			if (e.target === box) close();
		});
		$("md-yes").focus();
	}

	// An order cannot be over-allocated. Whatever is typed is held to what the
	// order still needs, so the way to give one variety more is to take it off
	// another - which is the real decision being made.
	function clampQty(input, need) {
		var f = parseFloat(input.getAttribute("data-f")) || 1;
		var item = input.getAttribute("data-i");

		var others = 0;
		Array.prototype.forEach.call($("sb-drawer").querySelectorAll("input.qty"), function (i) {
			if (i === input) return;
			others +=
				(parseFloat(i.value || 0) || 0) * (parseFloat(i.getAttribute("data-f")) || 1);
		});

		var room = Math.max(need - others, 0);
		var allowed = Math.floor(room / f);
		var want = Math.max(parseFloat(input.value || 0) || 0, 0);

		if (need && want > allowed) {
			input.value = allowed;
			warn(
				allowed === 0
					? "The order is full. Reduce another variety before adding <b>" +
							esc(item) +
							"</b>."
					: "<b>" +
							esc(item) +
							"</b> capped at " +
							allowed +
							(allowed === 1 ? " bunch" : " bunches") +
							" &mdash; that is all the order still needs.",
			);
		} else {
			warn("");
		}
		tally(need);
	}

	function warn(html) {
		var el = $("dr-warn");
		if (!el) return;
		el.innerHTML = html || "";
		el.className = html ? "dr-warn on" : "dr-warn";
	}

	function closeDrawer() {
		document.body.classList.remove("dr-open");
		OPEN = null;
		stamp();
	}

	function tally(need) {
		var total = 0,
			used = 0;
		Array.prototype.forEach.call($("sb-drawer").querySelectorAll("input.qty"), function (i) {
			var b = parseFloat(i.value || 0) || 0;
			var f = parseFloat(i.getAttribute("data-f")) || 1;
			var stems = b * f;
			total += stems;
			if (b > 0) used++;
			var cell = $("sb-drawer").querySelector('[data-s="' + i.getAttribute("data-i") + '"]');
			if (cell) cell.textContent = n(stems);
		});
		if (!$("dr-fill")) return;
		var pct = need ? Math.min((total / need) * 100, 100) : 100;
		$("dr-fill").style.width = pct + "%";
		$("dr-fill").className = "dr-fill" + (total >= need && need ? " full" : "");
		$("dr-count").innerHTML =
			"<b>" +
			n(total) +
			"</b> of " +
			n(need) +
			" stems &middot; " +
			used +
			(used === 1 ? " variety" : " varieties") +
			(used === 1 ? " <span class='sb-short'>(at least 2 required)</span>" : "") +
			(need && total < need
				? " &middot; <span class='sb-short'>" + n(need - total) + " short</span>"
				: need && total > need
					? " &middot; <span class='sb-short'>" + n(total - need) + " over</span>"
					: need
						? " &middot; <span class='sb-cov'>covered</span>"
						: "");
	}

	// Read the bunch inputs back as STEMS, with both floors enforced here as well
	// as at the input: the quantities are editable, so nothing else stops one
	// variety filling a whole order, or the order being over-filled.
	function collectItems(a) {
		var items = [];
		var noLength = [];
		Array.prototype.forEach.call($("sb-drawer").querySelectorAll("input.qty"), function (i) {
			var b = parseFloat(i.value || 0) || 0;
			var f = parseFloat(i.getAttribute("data-f")) || 1;
			if (b > 0) {
				var code = i.getAttribute("data-i");
				var len = lenOf(code);
				if (!len) noLength.push(code);
				items.push({
					doctype: "Shopify Allocation Item",
					item_code: code,
					warehouse: a.source_warehouse,
					stem_length: len,
					qty: b * f,
				});
			}
		});
		if (!items.length) {
			msg("Nothing to save &mdash; every quantity is zero.", "warn");
			return null;
		}
		// Asked for here as well as on the server: the packhouse packs to the
		// length on the pick list, so a line without one cannot be packed, and
		// hearing that now beats hearing it from a failed submit.
		if (noLength.length) {
			msg(
				"Pick a stem length for <b>" +
					noLength.map(esc).join("</b>, <b>") +
					"</b> &mdash; packing works off it.",
				"err",
			);
			return null;
		}
		var total = items.reduce(function (t, r) {
			return t + r.qty;
		}, 0);
		var need = a.required_stems || 0;
		if (need && total > need) {
			msg(
				"This allocates " +
					n(total - need) +
					" stems more than the order needs. " +
					"Reduce a variety before saving.",
				"err",
			);
			return null;
		}
		var choices = $("sb-drawer").querySelectorAll("input.qty").length;
		if (items.length < 2 && choices > 1) {
			msg(
				"An order must draw on at least <b>two varieties</b>. Add a second one before saving.",
				"err",
			);
			return null;
		}
		return items;
	}

	function lenOf(code) {
		var sel = $("sb-drawer").querySelector('select.len[data-i="' + code + '"]');
		return sel ? sel.value || "" : "";
	}

	function busy(on, label) {
		["dr-save", "dr-submit", "dr-pick", "dr-pack", "dr-shipped", "dr-cancel"].forEach(
			function (id) {
				if ($(id)) $(id).disabled = on;
			},
		);
		if (!label) return;
		var target = $("dr-submit") || $("dr-pack") || $("dr-shipped") || $("dr-pick");
		if (target) target.textContent = label;
	}

	function persist(a, thenSubmit) {
		var items = collectItems(a);
		if (!items) return;

		var stems = items.reduce(function (t, r) {
			return t + r.qty;
		}, 0);
		var need = a.required_stems || 0;

		if (thenSubmit) {
			confirmModal(
				"Are you sure you want to submit this allocation?",
				"<p><b>" +
					esc(a.shopify_order || a.name) +
					"</b> &middot; " +
					n(stems) +
					" stems across " +
					items.length +
					" varieties, cut to " +
					lenSummary(items) +
					".</p>" +
					"<ul><li>Transfers them from <b>" +
					esc(shortWh(a.source_warehouse)) +
					"</b> to <b>" +
					esc(shortWh(a.reserve_warehouse || "the reserve warehouse")) +
					"</b> and flags them sold.</li>" +
					"<li>Raises the pick list so packing can start.</li></ul>" +
					(stems < need
						? "<p class='sb-short'>This is " +
							n(need - stems) +
							" stems short of the " +
							n(need) +
							" the order needs.</p>"
						: "") +
					"<p class='md-note'>Reversing it means cancelling the allocation.</p>",
				function () {
					write(a, items, true);
				},
			);
			return;
		}
		write(a, items, false);
	}

	function lenSummary(items) {
		var seen = [];
		items.forEach(function (r) {
			if (r.stem_length && seen.indexOf(r.stem_length) === -1) seen.push(r.stem_length);
		});
		seen.sort();
		if (!seen.length) return "no stated length";
		return seen.length === 1
			? "<b>" + esc(seen[0]) + "</b>"
			: "<b>" + seen.map(esc).join(" + ") + "</b>";
	}

	// The pick list is raised server-side the moment the allocation is submitted.
	// Give it a QR code the same way the farm's own "Regenerate QR Code" button
	// does - the pick list's desk URL, through the same QR service - so one
	// scanner reads a Shopify pick list and a farm one alike.
	//
	// Skipped when the code is already there, which is what happens once the app
	// version that draws the QR server-side is deployed. Never blocking: a pick
	// list without a QR is still a pick list, and the button is still there.
	function attachOplQr(alloc) {
		getList(
			"Order Pick List",
			{
				filters: [
					["custom_shopify_allocation", "=", alloc],
					["docstatus", "<", 2],
				],
				fields: ["name", "custom_qr_code"],
			},
			function (rows) {
				var opl = rows && rows[0];
				if (!opl || opl.custom_qr_code) return;
				var deskUrl =
					window.location.origin +
					"/app/order-pick-list/" +
					encodeURIComponent(opl.name);
				fetch(
					"https://api.qrserver.com/v1/create-qr-code/?size=600x600&margin=0&format=png&data=" +
						encodeURIComponent(deskUrl),
				)
					.then(function (resp) {
						if (!resp.ok) throw new Error("QR service returned " + resp.status);
						return resp.blob();
					})
					.then(function (blob) {
						var fd = new FormData();
						fd.append("file", blob, opl.name + "_qr.png");
						fd.append("is_private", 0);
						fd.append("optimize", 0);
						fd.append("doctype", "Order Pick List");
						fd.append("docname", opl.name);
						fd.append("fieldname", "custom_qr_code");
						return fetch("/api/method/upload_file", {
							method: "POST",
							headers: { "X-Frappe-CSRF-Token": frappe.csrf_token },
							body: fd,
						});
					})
					.then(function (up) {
						return up.json();
					})
					.then(function (res) {
						var fileUrl = res && res.message && res.message.file_url;
						if (!fileUrl) throw new Error("Upload returned no file url");
						// The pick list is submitted by now; an Attach field can
						// still be written on a submitted document.
						return frappe.call({
							method: "frappe.client.set_value",
							args: {
								doctype: "Order Pick List",
								name: opl.name,
								fieldname: "custom_qr_code",
								value: fileUrl,
							},
						});
					})
					.then(function () {
						toast("Pick list <b>" + esc(opl.name) + "</b> has its QR code.", "ok");
						loadAll(true);
					})
					.catch(function (e) {
						toast(
							"<b>" +
								esc(opl.name) +
								"</b> was raised, but its QR code was not generated. " +
								"Use <b>Regenerate QR Code</b> on the pick list.",
							"warn",
						);
						reportError("generate the pick list QR code", {
							message: (e && e.message) || String(e),
						});
					});
			},
			function () {},
		);
	}

	function write(a, items, thenSubmit) {
		busy(true, thenSubmit ? "Submitting…" : null);
		frappe.call({
			method: "frappe.client.get",
			args: { doctype: "Shopify Allocation", name: a.name },
			callback: function (r) {
				var doc = r && r.message;
				if (!doc) {
					failSave("read " + a.name, { exc_type: "DoesNotExistError" });
					return;
				}
				doc.items = items;
				frappe.call({
					method: "frappe.client.save",
					args: { doc: doc },
					callback: function (sv) {
						var saved = (sv && sv.message) || doc;
						if (!thenSubmit) {
							closeDrawer();
							msg(
								"Saved " +
									items.length +
									" varieties to <b>" +
									esc(a.name) +
									"</b> (" +
									n(saved.total_qty) +
									" stems). Still a draft &mdash; submit it to reserve the stock.",
								"ok",
							);
							loadAll(true);
							return;
						}
						frappe.call({
							method: "frappe.client.submit",
							args: { doc: saved },
							callback: function (sb) {
								var done = (sb && sb.message) || {};
								closeDrawer();
								msg(
									"Submitted <b>" +
										esc(a.name) +
										"</b> &mdash; " +
										n(saved.total_qty) +
										" stems reserved" +
										(done.stock_entry
											? " on Stock Entry <b>" +
												esc(done.stock_entry) +
												"</b>"
											: "") +
										". It moves to Order traceability from here.",
									"ok",
								);
								attachOplQr(a.name);
								loadAll(true);
							},
							error: function (e) {
								// The lines are saved; only the reservation failed.
								failSave("submit it (the lines are saved as a draft)", e);
								loadAll(true);
							},
						});
					},
					error: function (e) {
						failSave("save the allocation", e);
					},
				});
			},
			error: function () {
				failSave("read " + a.name, { exc_type: "DoesNotExistError" });
			},
		});
	}

	// One exit for every write that fails: a sentence on screen, the detail in the
	// Error Log, and the buttons handed back so the operator can try again.
	function failSave(context, r) {
		reportError(context, r || {});
		busy(false);
		if ($("dr-submit")) $("dr-submit").textContent = "Submit & reserve";
	}

	// -------------------------------------------------------------------- render
	function renderAll() {
		// Rewriting a pane's innerHTML collapses its scroll container for an instant,
		// which resets scrollTop. On a background refresh that would jerk the list
		// back to the top under the reader.
		var panes = document.querySelectorAll(".sb-scroll");
		var tops = [];
		Array.prototype.forEach.call(panes, function (p) {
			tops.push(p.scrollTop);
		});

		LAST = new Date().toLocaleTimeString();
		NEXT = Date.now() + AUTO_MS;
		stamp();
		renderTabs();
		renderRail();
		// The stock pane is hidden in the traceability view, but its cards are not,
		// so it is always rendered - the figures stay live on both tabs.
		renderStock();
		if (VIEW === "trace") renderTrace();
		else renderOrders();

		Array.prototype.forEach.call(panes, function (p, i) {
			if (tops[i]) p.scrollTop = tops[i];
		});
	}

	// Counts down to the next background refresh, so it is obvious the page is
	// live and how fresh the figures are. Says "paused" rather than sitting at 0s
	// while the drawer is open, since the refresh is deliberately held then.
	function stamp() {
		var el = $("sb-stamp");
		if (!el) return;
		var head = STOCK.length + " stock rows · " + ALLOCS.length + " allocations";
		if (!LAST) {
			el.textContent = head;
			return;
		}
		var tail;
		if (OPEN) {
			tail = "paused while allocating";
		} else {
			var secs = Math.max(Math.ceil((NEXT - Date.now()) / 1000), 0);
			tail = secs ? "next update in " + secs + "s" : "updating…";
		}
		el.textContent = head + " · updated " + LAST + " · " + tail;
	}

	// Poll rather than reload. Paused while the drawer is open (the numbers behind
	// an edit must not move) and while the tab is hidden (no point), and caught up
	// as soon as it comes back into view.
	function startAuto() {
		if (TIMER) clearInterval(TIMER);
		if (TICK) clearInterval(TICK);
		TICK = setInterval(stamp, 1000);
		TIMER = setInterval(function () {
			if (OPEN) return;
			if (document.hidden) return;
			loadAll(true);
		}, AUTO_MS);
	}

	function start() {
		if (!$("sb-wh")) return;
		["sb-wh", "sb-win"].forEach(function (id) {
			$(id).addEventListener("change", function () {
				closeDrawer();
				renderAll();
			});
		});
		$("sb-var").addEventListener("input", renderStock);
		Array.prototype.forEach.call(document.querySelectorAll(".sb-tab"), function (b) {
			b.addEventListener("click", function () {
				var v = b.getAttribute("data-v");
				if (v === VIEW) return;
				VIEW = v;
				closeDrawer();
				msg("");
				renderAll();
			});
		});
		$("sb-reload").addEventListener("click", function (e) {
			e.preventDefault();
			msg("");
			closeDrawer();
			loadAll();
		});
		$("sb-scrim").addEventListener("click", closeDrawer);
		document.addEventListener("keydown", function (e) {
			if (e.key === "Escape" && OPEN) closeDrawer();
		});
		document.addEventListener("visibilitychange", function () {
			if (!document.hidden && !OPEN) loadAll(true);
		});
		loadAll();
		startAuto();
	}

	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
	else setTimeout(start, 0);
})();
