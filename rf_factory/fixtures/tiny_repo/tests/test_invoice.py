import unittest
from invoice import invoice_total


class InvoiceTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(invoice_total([(10, 2), (5, 3)]), 35)

    def test_discount(self):
        self.assertEqual(invoice_total([(10, 2)], 0.25), 15)

    def test_empty(self):
        self.assertEqual(invoice_total([]), 0)

    def test_invalid(self):
        for items, discount in [([], -1), ([], 2), ([(-1, 2)], 0), ([(1, -2)], 0)]:
            with self.subTest(items=items, discount=discount), self.assertRaises(ValueError):
                invoice_total(items, discount)
