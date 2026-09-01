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

Both are solved here, by this app alone. Nothing in this module imports another
Upande app: `ecommerce_integration` declares no `required_apps` and reads no
upande_webshop DocType, so its CI must not be bootstrapped by upande_webshop's
helper either — a failure over there would surface as a red build here, and a
change to its stubs would silently change what this app is tested against.
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
# ERPNext seeds these only through the setup wizard
# (erpnext/setup/setup_wizard/operations/install_fixtures.py). Without the
# wizard, creating a Company fails because its default "Goods In Transit"
# warehouse links Warehouse Type "Transit".
STANDARD_WAREHOUSE_TYPES = ("Transit",)

_TITLE_FIELD = [{"label": "Title", "fieldname": "title", "fieldtype": "Data"}]

# Most stubs only need to exist so a Link resolves. Three carry real fields
# because the code under test reads them: the post-harvest `Stem Length` master
# is a price source, and `Shelf`/`Shelf Item` are the shelf stock source.
STUB_DOCTYPES = {
	"Business Unit": {"fields": _TITLE_FIELD},
	"Consignee": {"fields": _TITLE_FIELD},
	"Delivery Point": {"fields": _TITLE_FIELD},
	"Farm": {"fields": _TITLE_FIELD},
	"Packrate": {"fields": _TITLE_FIELD},
	"Stem Length": {
		"autoname": "field:length",
		"fields": [
			{"label": "Length", "fieldname": "length", "fieldtype": "Data", "unique": 1, "reqd": 1},
			{"label": "Price", "fieldname": "price", "fieldtype": "Float"},
			{"label": "Company", "fieldname": "company", "fieldtype": "Link", "options": "Company"},
		],
	},
	"Shelf": {
		"autoname": "field:shelf_id",
		"fields": [
			{"label": "Shelf ID", "fieldname": "shelf_id", "fieldtype": "Data", "unique": 1, "reqd": 1},
			{"label": "Farm", "fieldname": "farm", "fieldtype": "Link", "options": "Farm"},
			{"label": "Items", "fieldname": "items", "fieldtype": "Table", "options": "Shelf Item"},
		],
	},
	"Shelf Item": {
		"istable": 1,
		"fields": [
			{"label": "Variety", "fieldname": "variety", "fieldtype": "Link", "options": "Item"},
			{"label": "Stem Length", "fieldname": "stem_length", "fieldtype": "Data"},
			{"label": "Stem Qty", "fieldname": "stem_qty", "fieldtype": "Int"},
			{"label": "Warehouse", "fieldname": "warehouse", "fieldtype": "Link", "options": "Warehouse"},
			{"label": "Date Added", "fieldname": "date_added", "fieldtype": "Datetime"},
		],
	},
}

# `Shelf.items` links to `Shelf Item`, so the child has to exist first.
_STUB_ORDER = (
	"Business Unit",
	"Consignee",
	"Delivery Point",
	"Farm",
	"Packrate",
	"Stem Length",
	"Shelf Item",
	"Shelf",
)


def ensure_stub_doctypes():
	"""Create a minimal custom DocType for each missing external link target, so
	the test runner can resolve the dependency without cloning a private repo.
	Idempotent, and a no-op wherever the real DocType is installed."""
	created = []
	for name in _STUB_ORDER:
		if frappe.db.exists("DocType", name):
			continue
		spec = STUB_DOCTYPES[name]
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": name,
				"module": "Ecommerce Integration",
				"custom": 1,
				"istable": spec.get("istable", 0),
				"autoname": spec.get("autoname", "hash"),
				"fields": spec["fields"],
				"permissions": []
				if spec.get("istable")
				else [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
			}
		).insert(ignore_permissions=True)
		created.append(name)

	# Another app on the bench may have stubbed one of these first, with only a
	# title field. The exists-check above then skips ours, leaving (for example) a
	# "Stem Length" with no `length`/`price` — and every query filtering on those
	# dies with "Unknown column 'length' in 'WHERE'". Top up whatever is missing so
	# the stub matches a real farm site regardless of who created it.
	topped_up = _ensure_stub_fields()

	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	print(f"ensure_stub_doctypes: created={created} fields_added={topped_up}")
	return created


def _ensure_stub_fields():
	"""Add any missing fields to stub DocTypes another app created first.

	Only touches DocTypes flagged `custom` — a real installed app's schema is
	never modified here.
	"""
	added = []
	for name, spec in STUB_DOCTYPES.items():
		if not frappe.db.exists("DocType", name):
			continue
		if not frappe.db.get_value("DocType", name, "custom"):
			continue  # the real thing is installed; leave it alone

		meta = frappe.get_meta(name)
		missing = [f for f in spec["fields"] if not meta.has_field(f["fieldname"])]
		if not missing:
			continue

		doc = frappe.get_doc("DocType", name)
		for field in missing:
			doc.append("fields", field)
			added.append(f"{name}.{field['fieldname']}")
		doc.save(ignore_permissions=True)
		frappe.clear_cache(doctype=name)
	return added


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


def ensure_warehouse_types():
	"""Create the Warehouse Types ERPNext's Company fixture needs.

	The setup wizard normally creates these. On a bare `install-app` site it never
	ran, so creating a Company fails on its default "Goods In Transit" warehouse:
	"Could not find Warehouse Type: Transit". That surfaces later as every
	ERPNext-derived test record failing at once, which says nothing about the
	cause.
	"""
	created = []
	for name in STANDARD_WAREHOUSE_TYPES:
		if frappe.db.exists("Warehouse Type", name):
			continue
		frappe.get_doc({"doctype": "Warehouse Type", "name": name}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)
		created.append(name)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	print(f"ensure_warehouse_types: created={created}, all={frappe.get_all('Warehouse Type', pluck='name')}")
	return created


def setup_test_site():
	"""Prepare a freshly installed CI site for `bench run-tests`.

	Self-contained on purpose: this app depends on no other Upande app, so its
	CI site is built entirely from this module. Order matters.

	1. Warehouse Types first — ERPNext's own bootstrap creates a Company, and
	   that fails without them.
	2. The stub DocTypes, before the bootstrap, so any link the test-record walk
	   follows already resolves.
	3. ERPNext's `before_tests`, which completes the setup wizard (Company,
	   fiscal year, currency exchange, ...).
	4. Warehouse Types and stubs again — `before_tests` resets site state, and
	   re-running both is idempotent and cheap.
	5. This app's custom fields last, now that everything they Link to exists.
	"""
	try:
		from erpnext.setup.utils import before_tests
	except ImportError:
		# v15 ships this; later ERPNext dropped it for a different test bootstrap.
		# Say so instead of dying on the import — but say it loudly, because
		# without the setup wizard there is no Company and half the test-record
		# walk will fail for reasons that look nothing like this.
		before_tests = None

	ensure_warehouse_types()
	ensure_stub_doctypes()

	if before_tests:
		print("setup_test_site: running erpnext before_tests ...")
		before_tests()
		print("setup_test_site: before_tests done")
	else:
		print(
			"setup_test_site: WARNING - erpnext.setup.utils.before_tests is absent on this "
			"ERPNext; the setup wizard has NOT run, so there is no Company fixture"
		)

	ensure_warehouse_types()
	ensure_stub_doctypes()
	ensure_custom_fields()
