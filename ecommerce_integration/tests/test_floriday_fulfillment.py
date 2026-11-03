# Copyright (c) 2026, Upande LTD and contributors
# See license.txt

"""Submitting a Floriday Sales Order fulfills it on Floriday.

Fulfillment used to run only as a bulk sweep over "orders submitted in the last
N hours", triggered by a button. Submitting is the moment the order is actually
committed on our side, so it is the moment to fulfill it — these tests pin the
three things that keeps honest:

* the hook fires for Floriday's own orders and NOTHING else, since firing on the
  wrong Sales Order would POST a fulfillment for an order Floriday never sent;
* it never fulfills twice, on a resubmit or on a later bulk run;
* it is queued rather than run inline, so a slow Floriday cannot roll back a
  submit.

The network is never touched: `enqueue` is patched, and the single-order path is
asserted through its guards.
"""

import uuid
from unittest.mock import patch

import frappe
from frappe import _dict

from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_order_fullfillment import (
	FULFIL_ON_BOX_LABEL,
	FULFIL_ON_FARM_PACK_LIST,
	FULFIL_ON_ORDER_PICK_LIST,
	FULFIL_ON_SALES_ORDER,
	fulfill_floriday_sales_order_on_submit,
	fulfill_on_box_label_insert,
	fulfill_on_farm_pack_list_submit,
	fulfill_on_order_pick_list_submit,
	fulfill_sales_order,
	is_floriday_sales_order,
)
from ecommerce_integration.testing import IntegrationTestCase

FULFILLMENT_MODULE = (
	"ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_order_fullfillment"
)
FLORIDAY_CUSTOMER = "_Test Floriday Buyer"


class _FakeSalesOrder(dict):
	"""Just enough of a Sales Order for the recogniser, which only reads fields."""

	doctype = "Sales Order"
	name = "SAL-ORD-TEST-0001"

	def get(self, key, default=None):
		return dict.get(self, key, default)


def _order(po_no, customer=FLORIDAY_CUSTOMER, **extra):
	doc = _FakeSalesOrder(po_no=po_no, customer=customer)
	doc.update(extra)
	return doc


class TestFloridayOrderRecognition(IntegrationTestCase):
	"""Which Sales Orders count as Floriday's."""

	def setUp(self):
		# Patch the Single read rather than writing to Floriday Settings: the
		# recogniser is the thing under test, not the settings form.
		patcher = patch.object(
			frappe.db, "get_single_value", side_effect=self._single_value(FLORIDAY_CUSTOMER)
		)
		self.get_single_value = patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def _single_value(customer):
		def _get(doctype, fieldname, *args, **kwargs):
			if doctype == "Floriday Settings" and fieldname == "customer":
				return customer
			return None

		return _get

	def test_uuid_po_no_and_matching_customer_is_a_floriday_order(self):
		self.assertTrue(is_floriday_sales_order(_order(str(uuid.uuid4()))))

	def test_floriday_order_id_with_a_blank_po_no_is_a_floriday_order(self):
		self.assertTrue(is_floriday_sales_order(_order("", custom_floriday_order_id=str(uuid.uuid4()))))

	def test_a_human_purchase_order_number_is_not_ours(self):
		"""The same customer can place ordinary orders; those must never be posted.

		Floriday salesOrderIds are UUIDs, so a real PO number is the tell.
		"""
		for po_no in ("PO-2026-0042", "b-B12", "8719604906480", ""):
			with self.subTest(po_no=po_no):
				self.assertFalse(is_floriday_sales_order(_order(po_no)))

	def test_another_customer_is_not_ours_even_with_a_uuid(self):
		self.assertFalse(is_floriday_sales_order(_order(str(uuid.uuid4()), customer="Someone Else")))

	def test_unconfigured_floriday_customer_matches_nothing(self):
		with patch.object(frappe.db, "get_single_value", side_effect=self._single_value(None)):
			self.assertFalse(is_floriday_sales_order(_order(str(uuid.uuid4()))))

	def test_a_non_sales_order_is_ignored(self):
		quotation = _order(str(uuid.uuid4()))
		quotation.doctype = "Quotation"
		self.assertFalse(is_floriday_sales_order(quotation))


class TestFulfillOnSubmit(IntegrationTestCase):
	"""What submitting actually does."""

	def setUp(self):
		for target, value in (("is_floriday_sales_order", True), ("_fulfil_on", FULFIL_ON_SALES_ORDER)):
			patcher = patch(f"{FULFILLMENT_MODULE}.{target}", return_value=value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_submitting_queues_the_fulfillment_after_commit(self):
		"""Queued, not inline — and only after the submit itself has committed.

		Inline would hang the submit behind Floriday's delivery-order paging, and
		an exception escaping the hook would roll the submit back.
		"""
		doc = _order(str(uuid.uuid4()))
		with (
			patch(f"{FULFILLMENT_MODULE}.frappe.enqueue") as enqueue,
			patch(f"{FULFILLMENT_MODULE}.frappe.msgprint"),
		):
			fulfill_floriday_sales_order_on_submit(doc)

		enqueue.assert_called_once()
		_args, kwargs = enqueue.call_args
		self.assertTrue(kwargs.get("enqueue_after_commit"))
		self.assertEqual(kwargs.get("sales_order"), doc.name)

	def test_an_already_fulfilled_order_is_not_fulfilled_again(self):
		"""Resubmitting, or a later bulk sweep, must not create a second one."""
		doc = _order(str(uuid.uuid4()), custom_floriday_fulfillment_order_id=str(uuid.uuid4()))
		with patch(f"{FULFILLMENT_MODULE}.frappe.enqueue") as enqueue:
			fulfill_floriday_sales_order_on_submit(doc)
		enqueue.assert_not_called()

	def test_a_non_floriday_order_queues_nothing(self):
		with (
			patch(f"{FULFILLMENT_MODULE}.is_floriday_sales_order", return_value=False),
			patch(f"{FULFILLMENT_MODULE}.frappe.enqueue") as enqueue,
		):
			fulfill_floriday_sales_order_on_submit(_order("PO-1"))
		enqueue.assert_not_called()


class TestFulfilOnSetting(IntegrationTestCase):
	"""Floriday Settings > Fulfil On picks the one step that sends the fulfilment."""

	def fire(self, step, hook, doc):
		with (
			patch(f"{FULFILLMENT_MODULE}._fulfil_on", return_value=step),
			patch(f"{FULFILLMENT_MODULE}._queue_fulfilment") as queue,
			patch.object(frappe.db, "get_value", return_value="SAL-ORD-TEST-0001"),
		):
			hook(doc)
		return queue

	def test_each_step_fulfils_only_when_it_is_the_chosen_one(self):
		so = _order(str(uuid.uuid4()))
		opl = _dict(sales_order="SAL-ORD-TEST-0001")
		fpl = _dict(sales_order="SAL-ORD-TEST-0001")
		label = _dict(order_pick_list="OPL-0001")
		hooks = {
			FULFIL_ON_SALES_ORDER: (fulfill_floriday_sales_order_on_submit, so),
			FULFIL_ON_ORDER_PICK_LIST: (fulfill_on_order_pick_list_submit, opl),
			FULFIL_ON_FARM_PACK_LIST: (fulfill_on_farm_pack_list_submit, fpl),
			FULFIL_ON_BOX_LABEL: (fulfill_on_box_label_insert, label),
		}
		for chosen in hooks:
			for step, (hook, doc) in hooks.items():
				with self.subTest(chosen=chosen, step=step):
					queue = self.fire(chosen, hook, doc)
					self.assertEqual(queue.called, step == chosen)

	def test_a_box_label_resolves_its_sales_order_through_the_pick_list(self):
		queue = self.fire(FULFIL_ON_BOX_LABEL, fulfill_on_box_label_insert, _dict(order_pick_list="OPL-0001"))
		queue.assert_called_once_with("SAL-ORD-TEST-0001")

	def test_one_job_per_order_however_many_labels(self):
		"""A Farm Pack List writes several Box Labels; only one fulfilment may go out."""
		doc = _order(str(uuid.uuid4()))
		with (
			patch(f"{FULFILLMENT_MODULE}.is_floriday_sales_order", return_value=True),
			patch(f"{FULFILLMENT_MODULE}.frappe.enqueue") as enqueue,
			patch(f"{FULFILLMENT_MODULE}.frappe.msgprint"),
			patch(f"{FULFILLMENT_MODULE}._fulfil_on", return_value=FULFIL_ON_SALES_ORDER),
		):
			fulfill_floriday_sales_order_on_submit(doc)
		_args, kwargs = enqueue.call_args
		self.assertTrue(kwargs.get("deduplicate"))
		self.assertEqual(kwargs.get("job_id"), f"floriday-fulfil-{doc.name}")


class TestFulfillSingleSalesOrder(IntegrationTestCase):
	"""The single-order entry point refuses clearly instead of failing silently."""

	def test_missing_sales_order_is_reported_not_raised(self):
		result = fulfill_sales_order("SAL-ORD-DOES-NOT-EXIST")
		self.assertEqual(result["status"], "error")
		self.assertIn("not found", result["message"])

	def test_it_never_reaches_floriday_for_an_unknown_order(self):
		"""The guard comes before any HTTP, so a bad name costs nothing."""
		with patch(f"{FULFILLMENT_MODULE}.order_fullment") as bulk:
			fulfill_sales_order("SAL-ORD-DOES-NOT-EXIST")
		bulk.assert_not_called()


class TestFulfillmentFieldIsDeclared(IntegrationTestCase):
	"""The idempotency key has to survive install/migrate and leave on uninstall."""

	def test_the_fulfillment_id_field_is_declared_and_owned_here(self):
		from ecommerce_integration.uninstall import (
			_declared_custom_fields,
			_fields_owned_by_other_apps,
		)

		pair = ("Sales Order", "custom_floriday_fulfillment_order_id")
		self.assertIn(pair, _declared_custom_fields())
		self.assertNotIn(pair, _fields_owned_by_other_apps())

	def test_the_field_is_writable_after_submit(self):
		"""It is stamped on a submitted order, so allow_on_submit is not optional."""
		from ecommerce_integration.ecommerce_integration.doctype.floriday_settings.floriday_custom_fields import (
			FLORIDAY_CUSTOM_FIELDS,
		)

		spec = next(
			s
			for s in FLORIDAY_CUSTOM_FIELDS
			if s["df"]["fieldname"] == "custom_floriday_fulfillment_order_id"
		)
		self.assertTrue(spec["df"].get("allow_on_submit"))
		self.assertTrue(spec["df"].get("read_only"))


class TestOnSubmitHookIsRegistered(IntegrationTestCase):
	def test_sales_order_on_submit_runs_the_floriday_fulfillment(self):
		handlers = frappe.get_hooks("doc_events").get("Sales Order", {}).get("on_submit", [])
		self.assertTrue(
			any("fulfill_floriday_sales_order_on_submit" in h for h in handlers),
			f"on_submit handlers: {handlers}",
		)

	def test_packhouse_steps_are_hooked(self):
		doc_events = frappe.get_hooks("doc_events")
		for doctype, event, handler in (
			("Order Pick List", "on_submit", "fulfill_on_order_pick_list_submit"),
			("Farm Pack List", "on_submit", "fulfill_on_farm_pack_list_submit"),
			("Box Label", "after_insert", "fulfill_on_box_label_insert"),
		):
			with self.subTest(doctype=doctype):
				handlers = doc_events.get(doctype, {}).get(event, [])
				self.assertTrue(any(handler in h for h in handlers), f"{doctype}.{event}: {handlers}")
