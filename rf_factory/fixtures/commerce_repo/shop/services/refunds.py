"""Refund estimates preserve the original tax and discount policies."""
from shop.core import tax
from shop.core.discount import apply_discount
from shop.core.inventory import allocate_stock


def refund_amount(subtotal, region, tier):
    discounted = apply_discount(subtotal, tier)
    return round(discounted + tax.tax_due(discounted, region), 2)


def exchange_balance(available, units, amount, region):
    remaining = allocate_stock(available, units)
    return remaining, tax.tax_due(amount, region)
