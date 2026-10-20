"""Daily summary values generated from public business operations."""
from shop.core.shipping import shipping_cost
from shop.core.tax import tax_due
from shop.core.discount import apply_discount
from shop.core.inventory import allocate_stock
from shop.core.notice import dispatch_notice


def daily_row(order_id, subtotal, weight, available):
    return {"delivery": shipping_cost(weight, "domestic"),
            "tax": tax_due(subtotal, "north"),
            "discounted": apply_discount(subtotal, "silver"),
            "remaining": allocate_stock(available, 1),
            "message": dispatch_notice(order_id, "queued")}
