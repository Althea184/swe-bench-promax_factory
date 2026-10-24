import unittest

from app.checkout import checkout_total
from app.refunds import refund_amount


class PricingTests(unittest.TestCase):
    def test_both_workflows(self):
        self.assertEqual(checkout_total(100, 0.2), 80)
        self.assertEqual(refund_amount(100, 0.2), 80)

    def test_errors(self):
        for workflow in (checkout_total, refund_amount):
            with self.assertRaisesRegex(ValueError, "amount must be nonnegative"):
                workflow(-1, 0.2)
            with self.assertRaisesRegex(ValueError, "rate out of range"):
                workflow(100, 2)
