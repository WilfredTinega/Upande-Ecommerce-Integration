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

Four scheduled jobs, each configured on Shopify Settings and mirrored into
Scheduled Job Type rows the same way Floriday and Biflorica do it:

| Prefix | Job | Default |
| --- | --- | --- |
| `sub` | Sync Subscription Contracts (dormant until selling plans exist) | Hourly |
| `ord` | Sync Orders | Hourly |
| `alloc` | Generate Allocations | Daily |
| `exp` | Expire Subscriptions | Daily |

**Start and end dates keep the two systems in step.** A subscription's end date is
derived from its start date, frequency and box count. The `exp` job flips a
subscription to `Inactive` once that date has passed *or* once every purchased
delivery has shipped, and inactive subscriptions stop generating allocations.

**Setup.**

1. In Shopify create a custom app with `read_orders`, `read_products`,
   `read_customers` (and `read_own_subscription_contracts` if you later add
   selling plans). Install it and copy the `shpat_...` token.
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
