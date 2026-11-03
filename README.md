### Ecommerce Integration

Upande Ecommerce Integrations — connectors for Floriday and Biflorica.

The Shopify connector lives in its own app, `shopify_integration`.

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

GitHub Actions workflows:

- **CI** (`ci.yml`) — on every push to `main` and every pull request. Runs
  `.github/helper/install.sh`, a deploy simulation: Frappe v15 + ERPNext +
  Payments + Upande Webshop, then `install-app ecommerce_integration` (which
  runs the `after_install` orchestrator), then `bench run-tests --app`.
- **Linters** (`linter.yml`) — pre-commit (ruff, eslint, prettier),
  [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and
  [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.
- **Release** (`release.yml`, `release_notes.yml`, `semantic-commits.yml`) —
  semantic-release versioning and release notes.

Before the test step, CI runs `ecommerce_integration.setup.ci.setup_test_site`.
Frappe's test runner builds a test record for every link-field dependency it can
reach, so the site needs two things a plain `install-app` does not provide:
ERPNext's setup-wizard fixtures (a Company, fiscal year, the "Transit" Warehouse
Type — via `upande_webshop.setup.ci`), and stub DocTypes for the link targets
owned by sibling Upande apps that are not installed in CI (`Business Unit` and
`Farm` from upande_core, `Consignee`, `Delivery Point`, `Packrate`,
`Stem Length`). That module is CI-only — it is never wired into
`after_install` / `after_migrate`.


### License

mit
