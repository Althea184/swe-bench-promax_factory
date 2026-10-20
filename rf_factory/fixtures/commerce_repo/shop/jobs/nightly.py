"""Nightly reconciliation samples all public operations."""
from shop.core.shipping import shipping_cost
from shop.core.tax import tax_due
from shop.core.discount import apply_discount
from shop.core.inventory import allocate_stock
from shop.core.notice import dispatch_notice


def reconcile(order_id, amount, weight, available):
    return (shipping_cost(weight, "domestic"), tax_due(amount, "north"),
            apply_discount(amount, "standard"), allocate_stock(available, 1),
            dispatch_notice(order_id, "delivered"))
