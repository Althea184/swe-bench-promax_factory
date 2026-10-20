import unittest

from shop.adapters.partner import partner_order
from shop.entrypoints.cli import quick_quote, preview_for_staff, support_snapshot
from shop.entrypoints.web import order_view
from shop.entrypoints.kiosk import terminal_summary, terminal_dispatch
from shop.jobs.nightly import reconcile
from shop.reports.daily import daily_row
from shop.reports.operations import operations_row
from shop.services.refunds import exchange_balance, refund_amount
from shop.workflows.checkout import checkout_preview, ship_order


class CommerceTests(unittest.TestCase):
    def test_checkout_path(self):
        preview = checkout_preview(100, 2, 10, 2, "regional", "south", "silver")
        self.assertEqual(preview["charges"], {"items": 95.0, "delivery": 7.0, "tax": 5.7, "total": 107.7})
        self.assertEqual(preview["parcel"], {"remaining": 8, "freight": 7.0})
        self.assertEqual(ship_order("A1", 10, 2), (8, "Order A1: shipped"))

    def test_reports_and_partner_facade(self):
        self.assertEqual(daily_row("A1", 100, 2, 10),
                         {"delivery": 4.0, "tax": 8.0, "discounted": 95.0,
                          "remaining": 9, "message": "Order A1: queued"})
        self.assertEqual(operations_row("A1", 100, 2, 10), (14.0, 0.0, 88.0, 8, "Order A1: delivered"))
        self.assertEqual(partner_order("A1", 100, 2, 10),
                         {"shipping": 7.0, "tax": 6.0, "discount": 95.0,
                          "stock": 9, "notice": "Order A1: queued"})

    def test_entrypoints_and_jobs(self):
        self.assertEqual(terminal_summary(100, 2, 10), (112.0, 9))
        self.assertEqual(terminal_dispatch("A1", 10), (9, "Order A1: shipped"))
        self.assertEqual(quick_quote(2, 100), 12.0)
        self.assertEqual(support_snapshot("A1", 100, 10), (88.0, 9, "Order A1: shipped"))
        self.assertEqual(preview_for_staff(100, 2, 10)["charges"]["total"], 107.7)
        view = order_view("A1", 100, 2, 10)
        self.assertEqual((view["freight"], view["tax"], view["discount"], view["stock"]), (7.0, 6.0, 95.0, 9))
        self.assertEqual(reconcile("A1", 100, 2, 10), (4.0, 8.0, 100.0, 9, "Order A1: delivered"))

    def test_refunds_and_errors(self):
        self.assertEqual(refund_amount(100, "north", "gold"), 95.04)
        self.assertEqual(exchange_balance(10, 2, 100, "south"), (8, 6.0))
        with self.assertRaisesRegex(ValueError, "insufficient stock"):
            exchange_balance(1, 2, 100, "south")
        with self.assertRaisesRegex(ValueError, "unsupported zone"):
            checkout_preview(100, 2, 10, 1, "moon", "north", "standard")
