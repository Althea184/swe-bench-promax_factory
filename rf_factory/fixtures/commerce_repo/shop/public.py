"""Stable import surface for partner integrations."""

from shop.core.shipping import shipping_cost
from shop.core.tax import tax_due
from shop.core.discount import apply_discount
from shop.core.inventory import allocate_stock
from shop.core.notice import dispatch_notice
