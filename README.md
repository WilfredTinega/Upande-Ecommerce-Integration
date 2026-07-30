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
| `sub` | Sync Subscription Contracts (dormant until selling plans exist) | Hourly |
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
2. In Shopify Settings set Shop Domain (the `*.myshopify.com` one) and the token,
   then **Test Connection**.
3. **Seed / Refresh Product Map**, then review every row's class and stem count.
4. Set the source and reserve warehouses, company, and a fallback customer.
5. Tick *Enabled* plus the per-job switches.

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
