# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""This app stands alone, and its CI must prove that rather than assume it.

`ecommerce_integration` declares no `required_apps` and reads no other Upande
app's DocTypes — every such read was removed so the Floriday and Biflorica
screens work on a site running this app by itself. The CI used to undercut that:
it pulled upande_webshop into the bench, installed it on the test site, and
bootstrapped the test fixtures by calling upande_webshop's own CI helper. So the
suite was green on a site that had upande_webshop, which is the one arrangement
the app no longer supports — and a break in that repo turned this build red.

These tests fail if the coupling comes back, by any of the three routes it took:
a Python import, the bench/site the workflow builds, or `required_apps`.
"""

import ast
import pathlib

import frappe

from ecommerce_integration.testing import IntegrationTestCase

# Sibling Upande apps this one must never import. Not an exhaustive list of what
# exists — a list of what this app has previously leaned on.
SIBLING_APPS = ("upande_webshop", "upande_packhouse", "upande_harvest", "upande_core", "upande_kaitet")

APP_ROOT = pathlib.Path(frappe.get_app_path("ecommerce_integration"))
REPO_ROOT = APP_ROOT.parent


def _imported_modules(path):
	"""Top-level module names imported by a Python file, via AST.

	Parsed rather than grepped: the app's comments and docstrings mention
	upande_webshop all over the place, explaining what was removed and why. Those
	are the record of the decoupling, not a breach of it.
	"""
	try:
		tree = ast.parse(path.read_text())
	except (OSError, SyntaxError):
		return set()

	modules = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			modules.update(alias.name.split(".")[0] for alias in node.names)
		elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
			modules.add(node.module.split(".")[0])
	return modules


class TestNoSiblingAppImports(IntegrationTestCase):
	def test_no_python_file_imports_a_sibling_upande_app(self):
		offenders = []
		for path in sorted(APP_ROOT.rglob("*.py")):
			for module in _imported_modules(path) & set(SIBLING_APPS):
				offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module}")

		self.assertEqual(offenders, [], f"this app must not import a sibling Upande app: {offenders}")

	def test_the_ci_helper_bootstraps_the_test_site_itself(self):
		"""The one that actually bit: setup_test_site called webshop's helper."""
		from ecommerce_integration.setup import ci

		self.assertNotIn("upande_webshop", _imported_modules(pathlib.Path(ci.__file__)))
		for name in ("ensure_warehouse_types", "ensure_stub_doctypes", "ensure_custom_fields"):
			self.assertTrue(callable(getattr(ci, name, None)), f"{name} must live in this app")


class TestNoSiblingAppInCi(IntegrationTestCase):
	"""The workflow must not build a bench or a site that carries one."""

	CI_FILES = (
		".github/workflows/ci.yml",
		".github/helper/install.sh",
		".github/helper/site_config.json",
	)

	def test_the_ci_pipeline_never_installs_a_sibling_upande_app(self):
		offenders = []
		for relative in self.CI_FILES:
			path = REPO_ROOT / relative
			if not path.is_file():
				continue  # the app dir alone, without the repo around it
			for line in path.read_text().splitlines():
				stripped = line.strip()
				if stripped.startswith("#"):
					continue  # a comment saying why it is absent is the point
				for app in SIBLING_APPS:
					if app in stripped:
						offenders.append(f"{relative}: {stripped}")

		self.assertEqual(offenders, [], f"CI must not pull in a sibling Upande app: {offenders}")


class TestNoRequiredApps(IntegrationTestCase):
	def test_the_app_declares_no_required_apps(self):
		self.assertEqual(frappe.get_hooks("required_apps", app_name="ecommerce_integration"), [])
