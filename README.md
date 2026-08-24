### Ecommerce Integration

Upande Ecommerce Integrations — connectors for Floriday, Biflorica and Shopify.

### Shopify (subscriptions)

A subscription connector that deliberately does **not** use frappe's
`ecommerce_integrations` app, and creates **no Sales Order and no Quotation**.
Orders are held in the app and fulfilled through allocations against whatever
stock is actually available.

**Orders are the source of truth, not subscription contracts.** The connected
storefront sells subscriptions *without* Shopify selling plans — every box product
returns `selling_plan_groups: []` and `requires_selling_plan: false`, so there are
no `SubscriptionContract` records to read. "3 boxes over 3 months" is a single
prepaid order carrying its duration, start date and gift details as cart
attributes or line-item properties. The contract sync is still present and will
light up if a subscriptions app is ever installed, but it finds nothing today.

```
Shopify orders ──poll──► Shopify Order  (stored verbatim, every attribute kept)
                              │
                              ├── duration > 1 ──► Shopify Subscription
                              │                    start/end dates, N deliveries
                              ▼
                    Shopify Allocation × N   (one per delivery, dated by frequency)
                    team fills from available stock
                              │
                    submit ──► Stock Entry (Material Transfer)
                               source ──► reserve warehouse
                              │
                    Mark Packed ──► Mark Shipped
```

Doctypes: `Shopify Settings` (Single), `Shopify Product Map`,
`Shopify Subscription` (+ `Line`), `Shopify Order` (+ `Item`, + `Attribute`),
`Shopify Allocation` (+ `Item`, submittable).

**New subscription options are configuration, not code.** `Shopify Product Map`
holds one row per Shopify variant: its class (Box / Fee / Packaging) and its stem
count. Only `Box` lines are allocated, so the store's fee products — Future
deliveries, Delivery box fee, Carrier box — ride along on the order and are
ignored by fulfilment. A new box tier is a new row. Matching is by **variant id,
not SKU**: every product on this store has a null SKU.

Seeding guesses classes and stem counts from the current catalogue
(Petite 24 / Signature 48 / Grand 72, Build Your Own priced per stem so its
ordered qty *is* the stem count). Anything unrecognised comes through as a `Box`
with zero stems rather than being silently dropped — review the rows after seeding.

**Authentication.** Shopify's `client_credentials` grant mints an Admin API token
straight from the app's Client ID and Secret — no merchant browser step:

```
POST https://{shop}/admin/oauth/access_token
Content-Type: application/x-www-form-urlencoded
grant_type=client_credentials&client_id=...&client_secret=...
```

That token **expires in about 24 hours**, so it is treated as a cache. It is
re-minted whenever it falls inside *Refresh Buffer (Minutes)* — checked both
before every API call and by the `tok` job — so an expiring token is replaced
rather than used, and a missed cron slot can't become a failed sync. A pasted
long-lived `shpat_` token still works; it simply has no expiry to honour.

Note that a client_credentials token carries exactly the scopes the *app* is
configured for. A scopeless app authenticates fine and then denies every query,
so Test Connection warns when the grant returns no scopes.

Five scheduled jobs, each configured on Shopify Settings and mirrored into
Scheduled Job Type rows the same way Floriday and Biflorica do it:

| Prefix | Job | Default |
| --- | --- | --- |
| `tok` | Refresh Access Token (only when inside the buffer) | Hourly |
| `sub` | Derive Subscriptions from Orders (no Shopify call) | Hourly |
| `ord` | Sync Orders | Hourly |
| `alloc` | Generate Allocations | Daily |
| `exp` | Expire Subscriptions | Daily |

Ticking a task's *Enabled* is sufficient — if its frequency is blank (a field
default only applies to a brand-new doc, so a newly shipped task is empty on an
existing Settings) it is backfilled on save. Without that, an enabled task
registers no job and says nothing about why.

**Everything is driven from the Shopify Settings form** — no console. Individual
buttons for Test Connection, Refresh Access Token, Seed Product Map, Sync Now,
Sync Orders Now, Generate Allocations and Run Expiry Now, plus **Run Full Sync**
which does the whole chain in dependency order and reports each step separately. A
step that aborts on a GraphQL error is reported as failed even though the sync
returns normally, so a denied scope can't read as success. The form headline shows
remaining token life and granted scopes.

**Start and end dates keep the two systems in step.** A subscription's end date is
derived from its start date, frequency and box count. The `exp` job flips a
subscription to `Inactive` once that date has passed *or* once every purchased
delivery has shipped, and inactive subscriptions stop generating allocations.

**Setup.**

1. In the Shopify **Dev Dashboard** open the app → **Versions → Create a version**
   → **API access → Scopes**, then **Release**. Scopes live on a version; the
   Settings page has no scopes field. A released version is *not* applied to
   installed stores automatically — the merchant must approve it in the admin.

   | Purpose | Scopes |
   | --- | --- |
   | What this connector reads today | `read_orders`, `read_products`, `read_customers` |
   | Fulfilment write-back (own location) | `write_orders`, `read_locations`, `read_fulfillments`, `write_fulfillments`, `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders` |
   | Only if stock is ever pushed to Shopify | `read_inventory`, `write_inventory` |
   | Fulfilled by an outside service instead | `read_assigned_fulfillment_orders`, `write_assigned_fulfillment_orders` or the `*_third_party_fulfillment_orders` pair |

   Two scopes are **protected** and need Shopify's approval first — do not put them
   in the required Scopes field until granted, or the release/approval can fail:
   `read_all_orders` (without it `read_orders` only exposes the last **60 days**,
   so keep *Lookback Days* under 60) and `read_own_subscription_contracts` (only
   needed if selling plans are ever added).

   The contract sync knows about that last one: if *Granted Scopes* is populated and
   `read_own_subscription_contracts` is not in it, `Sync Subscription Contracts`
   skips without calling Shopify and says so, rather than collecting an
   `ACCESS_DENIED` on `subscriptionContracts` every hour. It still tries while the
   granted list is unknown — Shopify is the authority on what a token holds, not a
   field nobody has refreshed.

2. In Shopify Settings set Shop Domain (the `*.myshopify.com` one) and the token,
   then **Test Connection**.
3. **Seed / Refresh Product Map**, then review every row's class and stem count.
4. Set the source and reserve warehouses, company, the **Billing Customer**, and a
   fallback customer. Tick **Create Missing Customers**.
5. Tick *Enabled* plus the per-job switches.

**One customer, many subscribers under it.** *Customer* on Shopify Settings names the
single ERPNext Customer every order, subscription and allocation books to. There is
deliberately no second customer link: a subscriber is not a customer in their own
right, they are a **Shipping Address** and a **Contact** attached to that one account,
carrying their own name, phone and email. The delivery details are copied onto both
the order and the allocation as well, so the packing team reads them off the document
in front of them.

```
Customer: West View Software Ltd.        <- the only customer link
  ├── Address  RICHARD HOBBS-Shipping        901 Pyramid Park … Nairobi, 0706205998
  ├── Contact  RICHARD HOBBS-West View …     richard.hobbs2@gmail.com
  └── Address  Muga Martin-Shipping          … Nairobi
```

`_ensure_subscriber_address` matches on address line one plus city before inserting,
and `_ensure_subscriber_contact` matches on email under that customer. Neither has a
natural key and Address quietly appends a suffix on a name collision, so without those
checks every repeat order would hang another near-identical row off the same account.
An order with no shipping address gets no Address row — nothing to key on — but still
books to the customer and keeps its raw address text.

Leave *Customer* blank and resolution falls back to the older per-subscriber model:
match a Customer by `Contact Email` -> `Dynamic Link` -> `Customer`, and create one
(with its Contact, via `_create_subscriber`) when *Create Missing Customers* is
ticked. That Contact is load-bearing there — a Customer created without one can never
be matched again, and ERPNext's default `cust_master_name` of "Customer Name" appends
a suffix rather than refusing a duplicate, so you would silently accumulate `NAME`,
`NAME - 1`, `NAME - 2`, one per order.

**Subscriptions come from orders, not from contracts.** The `sub` job runs
`rebuild_subscriptions_from_orders`, which re-reads the `Shopify Order` docs already
stored and upserts one subscription per order that is a subscription. It costs no
Shopify call, and being idempotent it is the way to re-derive everything after the
classification rules change — a re-pull cannot do that, since an order unchanged on
Shopify is never re-applied. `sync_subscription_contracts` is still in the tree for a
store that does use selling plans, reachable through `sync_contracts_now`, but
nothing schedules it.

What counts as a subscription is decided by `_plan_kind` from the storefront's own
`Plan` label plus the delivery count:

| `Plan` | Count | Kind | Result |
| --- | --- | --- | --- |
| `Gift, 3 boxes over 3 months`, `Pay upfront – 4 deliveries` | 3, 4 | Fixed | Subscription, `deliveries_total` set, end date computed |
| `Subscription`, `Ongoing` | none | Open-ended | Subscription, `deliveries_total` 0, **no** end date |
| `One time purchase`, `One-time gift box` | none | One-off | No subscription |

The count is what decides it wherever there is one; `Plan` is what separates the
other two, because a Seal `Subscription` order carries no count at all and counting
attributes alone would read an indefinite subscriber as a single purchase. Open-ended
subscriptions must have no end date and no purchased quantity — that is precisely
what keeps the expiry job's hands off them, since it retires a subscription on a past
end date or on every purchased delivery having shipped, and an open-ended one has
neither. Seal raises a fresh order each cycle, so each cycle contributes one delivery.

**Attribute mapping takes a fallback list.** Every mapping field accepts several
comma-separated names, tried in order — `_frequency_days, Frequency,
_frequency_unit`. Seal states the same fact under different keys and not consistently
per order: on this store `_frequency_days` appears on 4 orders of 44 while `Frequency`
appears on 27, so a single name silently defaulted most of them and the cadence a
subscriber chose was lost with nothing looking wrong. Prefer the machine-readable key
first (`_start_date_iso` before `First delivery`, which is a year-less `"Thu 30 Jul"`).

**Picking and packing use the Upande Tambuzi doctypes.** No pick-list or pack-list
doctype is defined here. A submitted allocation raises that app's own `Order Pick List`
(`OPL-.YYYY.-`) and, from it, its `Farm Pack List` (`FPL-.YYYY.-`).

```
Shopify Allocation  ──submit──▶ Allocated      (reservation Stock Entry raised)
      │ Create Pick List
      ▼
Order Pick List  (Upande Tambuzi)   purpose = Delivery
      │   locations[] is ERPNext's `Pick List Item`:
      │     warehouse + custom_source_warehouse = where the stock was available
      │     qty = No of Bunches, stock_qty = Stems, uom = Bunched By
      │ submit, then Create Farm Pack List
      ▼
Farm Pack List  (Upande Tambuzi)    custom_order_pick_list is mandatory there
          pack_list_item[] is `Dispatch Form Item`: bunch_qty, stock_qty,
          no_of_boxes, source_warehouse, custom_opl_id
```

The two tables are different shapes and neither is ours, so the mapping is explicit
rather than a field-name copy: `custom_total_stems` is a Data field on the pick list and
an Int on the pack list, and `custom_customer` / `custom_customer_address` on the pack
list are Data rather than Links, so names are written into them directly.

The one footprint this app leaves on that app's doctype is a single Custom Field,
`Order Pick List.custom_shopify_allocation`, added idempotently by
`ensure_packing_link_fields`. Without it there is nothing to find a pick list by:
`Order Pick List` references a Sales Order and nothing else, and this connector raises
no Sales Order — so a second pick list for the same allocation could not be prevented
either. Both buttons refuse with a plain message on a site where the Upande Tambuzi app
is not installed.

Stems per line come from the Product Map's `stems_per_box`, read through `box_item`
rather than the variant, because several variants can map to the same box.

**Pull everything, allocate only what is paid.** Two independent switches, meant to
differ. *Paid Orders Only* (Order Sync tab) filters the pull; *Allocate Paid Orders
Only* (Allocation tab) filters what gets stock committed to it. Off and on
respectively is the configuration this store runs: every order is pulled so the
subscription list is complete — a subscriber whose payment is still pending is a real
subscriber — while an allocation, which reserves stock, is raised only for an order
Shopify reports as `PAID`. The paid check is re-read on every run rather than recorded,
so an order paid later starts allocating on the next pass with nothing to re-trigger.
The rolling pass for open-ended subscriptions applies the same rule through the
subscription's source order.

**Re-reading stored orders.** `reapply_stored_orders` re-parses every stored order's
kept `shopify_payload` under the current attribute mapping and re-derives its
subscription — no Shopify call. This is the counterpart to editing a mapping field,
because a re-sync cannot do it: an order unchanged on Shopify is never re-applied,
whatever the watermark says. Change a mapping, run this, then *Generate Allocations*.

**Deleting leaves wreckage unless the controller cleans up.** `Shopify Order.allocation`
and `.shopify_subscription` are Links, and a `force=True` delete skips the outgoing
link check that would otherwise refuse it — so a bulk clear-out used to leave orders
pointing at documents that no longer existed, and every later save of those orders
died on link validation. `Shopify Allocation.on_trash` recounts and repoints its
order; `Shopify Subscription.on_trash` clears the field on both orders and allocations.

One caution when changing settings from a script: use `doc.save()`, not `db_set`.
`db_set` leaves `modified` alone, so a Shopify Settings form somebody already had open
writes its stale values straight back over yours on their next Save, with no conflict
warning — which is exactly how the plan mapping silently reverted to blank once.

**Paid orders only (the pull switch).** *Paid Orders Only* (on by default) appends
`AND financial_status:paid` to the orders query, so unpaid carts never reach ERPNext
and never spend a page of the 25 each request pulls. It matters more than it sounds:
on the Tambuzi store the first unfiltered page of a 60-day window was 6 PAID, 18
PENDING and 1 REFUNDED, and the whole window holds 16 paid orders against 42 of all
kinds. An order paid later has its `updated_at` bumped by Shopify, so the next
incremental run collects it with no special handling. Two edges worth knowing: the
term matches `PAID` exactly, so a refund moves an order to `REFUNDED` and it stops
being returned (a copy already stored is kept, not deleted); and because the setting
is a Check, its default of 1 applies only to a newly created Shopify Settings — an
existing one has to be ticked by hand or it keeps syncing everything. The node loop
re-checks `displayFinancialStatus` as well, since Shopify's search syntax is not
versioned the way the schema is.

**Order Attribute Mapping.** The storefront's property names for duration, start
date, note and special requests are not discoverable from outside Shopify, so
every attribute Shopify sends is stored verbatim on each Shopify Order. Sync one
real order, read the names off its Attributes table, then enter them in Shopify
Settings. Until they're mapped, orders still sync — they just default to one box
with the order date as the delivery date.

**API log.** Every call to Shopify — successful or failed — lands in
`Shopify API Error Log` with operation, GraphQL operation name, error code
(`ACCESS_DENIED`, `THROTTLED`, …), duration, attempt count, the query, its
variables and a truncated response. `shopify_graphql()` is the single choke point
for Shopify traffic, so instrumenting it there covers every caller.

Entries are queued into **redis** and drained after the operation commits, rather
than inserted inline. That is deliberate: callers roll the database back when a
step fails, and an inline insert would be rolled back with it — losing precisely
the failures the log exists to record. Retention is set in Shopify Settings and
written into Frappe's Log Settings, which is what actually clears old rows.

**Reservation.** Submitting an allocation moves stock into the reserve warehouse
so it cannot be promised twice; cancelling reverses it. ERPNext's native Stock
Reservation Entry is unusable here because it is bound to a Sales Order.

**Polling, not webhooks.** Shopify signs webhooks with base64 HMAC-SHA256. As an
app this is now technically possible (unlike the Server Script sandbox, which
exposes no `hmac`/`base64`), but contracts change slowly and polling needs no
public endpoint. A webhook receiver can be added alongside without disturbing it.

**First run.** The GraphQL documents in `shopify_order_pull.py`,
`shopify_product_map.py` and `shopify_subscription_sync.py` have not been diffed
against Shopify's schema reference
for a specific API version. A wrong field name fails the whole query; the error
text is written verbatim to *Last Sync Summary* and the Error Log, so treat the
first run as a validation pass.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app ecommerce_integration
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ecommerce_integration
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
