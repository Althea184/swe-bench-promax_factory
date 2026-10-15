import unittest

from invoice import invoice_line, invoice_tax
from quote import quote_line


class MoneyTests(unittest.TestCase):
    def test_invoice(self):
        self.assertEqual(invoice_line(12.5, 2), "Invoice total: $25.00")
        self.assertEqual(invoice_tax(3), "Tax: $3.00")

    def test_quote(self):
        self.assertEqual(quote_line(4), "Quote: €4.00")

    def test_negative_amount(self):
        with self.assertRaisesRegex(ValueError, "negative amount"):
            invoice_line(-1, 1)
