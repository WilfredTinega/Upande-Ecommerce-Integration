"""CI / test-environment setup helpers.

Invoked explicitly from `.github/helper/install.sh` via `bench execute`, so the
CI test site carries the fixtures `bench run-tests` needs. Deliberately NOT
wired into after_install / after_migrate — nothing here runs on a real site.

Frappe's v15 test runner builds a test record for every link-field dependency of
every DocType it walks (`frappe.test_runner.get_dependencies`), recursing through
the whole reachable graph. Two things break that on a bare CI site:

  1. ERPNext's own test records need the setup-wizard baseline (a Company, fiscal
     year, the "Transit" Warehouse Type, ...) which `install-app` never creates.
  2. This app's DocTypes Link to DocTypes owned by sibling Upande apps that are
     not installed here (and are not `required_apps`), so the walk reaches
     `make_test_records("Business Unit")` and dies with DoesNotExistError.

(1) is already solved by upande_webshop's CI helper, which this app depends on.
(2) is solved below with minimal custom stub DocTypes, the same way webshop
stubs "Stem Length".
"""

import frappe

# Link targets reachable from this app's tested DocTypes that no app installed in
# CI defines. Owners, for the record:
#   Business Unit, Farm  -> upande_core
#   Consignee            -> upande_packhouse / upande_kaitet
#   Delivery Point       -> site-side custom DocType on the Floriday sites
#   Stem Length          -> upande_harvest (webshop stubs this one too; the
#                           exists-check below makes the overlap a no-op)
#   Packrate             -> upande_packhouse
# "Business Unit" / "Consignee" / "Farm" are hard Link fields on Floriday
# Settings and Biflorica Setting; the rest are targets of the custom fields this
# app puts on Sales Order / Sales Order Item / Warehouse / Stock Entry Detail.
STUB_DOCTYPES = (
	"Business Unit",
	"Consignee",
	"Delivery Point",
	"Farm",
	"Packrate",
	"Stem Length",
)


def ensure_stub_doctypes():
	"""Create a minimal custom DocType for each missing external link target, so
	the test runner can resolve the dependency without cloning a private repo.
	Idempotent, and a no-op wherever the real DocType is installed."""
	created = []
	for name in STUB_DOCTYPES:
		if frappe.db.exists("DocType", name):
			continue
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": name,
				"module": "Ecommerce Integration",
				"custom": 1,
				"autoname": "hash",
				"fields": [{"label": "Title", "fieldname": "title", "fieldtype": "Data"}],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
			}
		).insert(ignore_permissions=True)
		created.append(name)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	print(f"ensure_stub_doctypes: created={created}")
	return created


def ensure_custom_fields():
	"""Re-run this app's custom-field step now that the stubs exist.

	`install-app` ran it before the stubs were created, and the creator skips any
	Link whose target DocType is absent, so the Sales Order / Warehouse fields
	were left out. Running it again gives the CI site the same schema a
	production site has — which is the point of the deploy simulation, and what
	the test-record walk then exercises.
	"""
	from ecommerce_integration.ecommerce_integration.doctype.biflorica_setting.biflorica_custom_fields import (
		ensure_biflorica_custom_fields,
	)
	from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
		ensure_floriday_custom_fields,
	)

	for label, fn in (
		("floriday", ensure_floriday_custom_fields),
		("biflorica", ensure_biflorica_custom_fields),
	):
		result = fn() or {}
		print(f"ensure_custom_fields[{label}]: {result.get('summary')}")
		for err in result.get("errors") or []:
			print(f"  error: {err}")

	frappe.db.commit()  # nosemgrep: frappe-manual-commit


def setup_test_site():
	"""Prepare a freshly installed CI site for `bench run-tests`.

	Order matters: the ERPNext/webshop baseline first (it completes the setup
	wizard, which is what the Company test record needs), then our stubs, then
	the custom fields that link to them.
	"""
	# upande_webshop is a required_app of this app, so it is always installed
	# here. Reusing its helper keeps the ERPNext test bootstrap in one place; it
	# raises on failure, which is what we want — a swallowed failure here just
	# resurfaces as a dozen confusing test-record errors.
	from upande_webshop.setup.ci import setup_test_site as webshop_setup_test_site

	print("setup_test_site: running upande_webshop setup_test_site ...")
	webshop_setup_test_site()
	print("setup_test_site: upande_webshop setup_test_site done")

	ensure_stub_doctypes()
	ensure_custom_fields()
