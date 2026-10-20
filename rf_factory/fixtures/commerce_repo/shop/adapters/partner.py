"""Partner adapter intentionally imports through the public re-export."""
from shop.public import shipping_cost, tax_due, apply_discount, allocate_stock, dispatch_notice


def partner_order(order_id, amount, weight, available):
    return {"shipping": shipping_cost(weight, "regional"),
            "tax": tax_due(amount, "south"),
            "discount": apply_discount(amount, "silver"),
            "stock": allocate_stock(available, 1),
            "notice": dispatch_notice(order_id, "queued")}
