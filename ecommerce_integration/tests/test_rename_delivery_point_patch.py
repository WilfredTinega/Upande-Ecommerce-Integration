# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""The Delivery Point GLN rename has to survive a real migrate.

Two ways it failed on a deploy, both pinned here:

* It dropped the stale column with `frappe.db.sql`. MariaDB implicitly commits a
  DDL statement, so Frappe refuses one issued after writes in the same
  transaction — and this patch always writes first (it copies the mappings over
  and deletes the old Custom Field). The deploy died with `ImplicitCommitError`
  and took the whole migrate with it. `frappe.db.sql_ddl` commits those writes
  first, which is the right order anyway.
* It bailed out whenever the old Custom Field was gone. That is exactly the
  state a failed column drop leaves behind, so the retry did nothing and the
  orphan column the patch exists to remove would have stayed forever.
"""

from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integration.patches import rename_delivery_point_floriday_id as renamer
from ecommerce_integration.testing import IntegrationTestCase

ALTER = f"ALTER TABLE `tabDelivery Point` DROP COLUMN `{renamer.OLD}`"


class _Run:
	"""Runs the patch against a described site state, capturing what it issued."""

	def __init__(self, old_field=None, columns=(), rows_to_move=0):
		self.old_field = old_field
		self.columns = set(columns)
		self.rows_to_move = rows_to_move
		self.sql = MagicMock(side_effect=self._sql)
		self.sql_ddl = MagicMock()
		self.deleted = []

	def _sql(self, query, *args, **kwargs):
		return [[self.rows_to_move]] if query.strip().upper().startswith("SELECT") else None

	def _exists(self, doctype, filters=None, *args, **kwargs):
		if doctype == "DocType":
			return filters == "Delivery Point"
		if doctype == "Custom Field":
			return self.old_field
		return None

	def __enter__(self):
		self._patches = [
			patch.object(frappe.db, "exists", side_effect=self._exists),
			patch.object(frappe.db, "has_column", side_effect=lambda dt, col: col in self.columns),
			patch.object(frappe.db, "sql", self.sql),
			patch.object(frappe.db, "sql_ddl", self.sql_ddl),
			patch.object(frappe, "delete_doc", side_effect=lambda *a, **k: self.deleted.append(a)),
			patch.object(frappe, "clear_cache"),
			patch(
				"ecommerce_integration.ecommerce_integration.doctype.floriday_settings."
				"floriday_custom_fields.ensure_floriday_custom_fields"
			),
		]
		for p in self._patches:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self._patches:
			p.stop()

	@property
	def ddl_statements(self):
		return [call.args[0] for call in self.sql_ddl.call_args_list]

	@property
	def plain_sql_statements(self):
		return [call.args[0] for call in self.sql.call_args_list]


class TestRenameDeliveryPointFloridayId(IntegrationTestCase):
	def test_the_column_drop_goes_through_sql_ddl(self):
		"""Not `frappe.db.sql` — DDL after a write raises ImplicitCommitError."""
		with _Run(old_field="CF-1", columns=(renamer.OLD, renamer.NEW), rows_to_move=2) as run:
			renamer.execute()

		self.assertIn(ALTER, run.ddl_statements)
		self.assertFalse(
			[q for q in run.plain_sql_statements if "ALTER TABLE" in q.upper()],
			"the DDL must not be issued through frappe.db.sql",
		)

	def test_it_copies_the_mappings_before_dropping_the_column(self):
		with _Run(old_field="CF-1", columns=(renamer.OLD, renamer.NEW), rows_to_move=2) as run:
			renamer.execute()

		updates = [q for q in run.plain_sql_statements if q.strip().upper().startswith("UPDATE")]
		self.assertEqual(len(updates), 1)
		self.assertTrue(run.deleted, "the old Custom Field must be removed")

	def test_a_stranded_column_is_dropped_even_with_the_field_already_gone(self):
		"""The state a failed drop leaves behind — the retry has to finish it."""
		with _Run(old_field=None, columns=(renamer.OLD, renamer.NEW)) as run:
			renamer.execute()

		self.assertIn(ALTER, run.ddl_statements)
		self.assertFalse(run.deleted)

	def test_nothing_to_do_touches_nothing(self):
		with _Run(old_field=None, columns=(renamer.NEW,)) as run:
			renamer.execute()

		self.assertEqual(run.ddl_statements, [])
		self.assertEqual(run.plain_sql_statements, [])

	def test_a_site_without_delivery_point_is_left_alone(self):
		with _Run(old_field="CF-1", columns=(renamer.OLD,)) as run:
			with patch.object(frappe.db, "exists", return_value=False):
				renamer.execute()

		self.assertEqual(run.ddl_statements, [])
