# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Updating this app must never orphan a site's Shopify orders.

The connector moved to shopify_integration. `before_migrate` either hands the
DocTypes over to it or stops the migrate; it installs nothing on a site that
never used Shopify.
"""

from unittest.mock import patch

import frappe

from ecommerce_integration.setup import shopify_split
from ecommerce_integration.testing import IntegrationTestCase

MODULE = "ecommerce_integration.setup.shopify_split"


class TestShopifyHandover(IntegrationTestCase):
	def run_before_migrate(self, doctypes, has_data, installed=(), on_bench=("shopify_integration",)):
		with (
			patch(f"{MODULE}.shopify_doctypes_left_here", return_value=doctypes),
			patch(f"{MODULE}.has_shopify_data", return_value=has_data),
			patch.object(frappe, "get_installed_apps", return_value=list(installed)),
			patch.object(frappe, "get_all_apps", return_value=list(on_bench)),
			patch.object(frappe.db, "set_value") as set_value,
			patch.object(frappe, "clear_cache"),
			patch("frappe.installer.install_app") as install_app,
		):
			shopify_split.before_migrate()
		return set_value, install_app

	def test_a_site_with_shopify_data_gets_the_app_installed(self):
		set_value, install_app = self.run_before_migrate(["Shopify Order"], has_data=True)
		set_value.assert_called_once()
		self.assertEqual(set_value.call_args.args[2:4], ("module", "Shopify Integration"))
		install_app.assert_called_once_with("shopify_integration", set_as_patched=True)

	def test_a_site_with_data_and_no_app_on_the_bench_stops_the_migrate(self):
		with self.assertRaises(frappe.ValidationError):
			self.run_before_migrate(["Shopify Order"], has_data=True, on_bench=())

	def test_a_site_without_shopify_data_installs_nothing(self):
		set_value, install_app = self.run_before_migrate(["Shopify Order"], has_data=False)
		set_value.assert_not_called()
		install_app.assert_not_called()

	def test_an_installed_app_only_gets_its_doctypes_back(self):
		set_value, install_app = self.run_before_migrate(
			["Shopify Order"], has_data=False, installed=("shopify_integration",)
		)
		set_value.assert_called_once()
		install_app.assert_not_called()

	def test_nothing_left_here_is_a_no_op(self):
		set_value, install_app = self.run_before_migrate([], has_data=True)
		set_value.assert_not_called()
		install_app.assert_not_called()
