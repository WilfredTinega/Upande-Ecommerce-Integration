# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Frappe version-tolerant test base classes.

This app supports frappe >=15,<20 (see pyproject [tool.bench.frappe-dependencies]),
and the scaffolded test base class moved between those versions:

  * v15  - only ``frappe.tests.utils.FrappeTestCase`` exists.
  * v16+ - ``frappe.tests.IntegrationTestCase`` is the replacement;
           ``FrappeTestCase`` still imports but is deprecated.

Test modules import ``IntegrationTestCase`` from here so the same file runs on
both, without tripping the v16 deprecation warning.
"""

try:  # frappe v16+
	from frappe.tests import IntegrationTestCase
except ImportError:  # frappe v15
	from frappe.tests.utils import FrappeTestCase as IntegrationTestCase

__all__ = ["IntegrationTestCase"]
