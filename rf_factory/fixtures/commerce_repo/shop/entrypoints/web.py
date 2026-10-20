"""Web endpoint helpers compose the workflow and public facade."""
from shop.workflows.checkout import checkout_preview
from shop.public import shipping_cost, tax_due, apply_discount, allocate_stock, dispatch_notice


def order_view(order_id, subtotal, weight, available):
    preview = checkout_preview(subtotal, weight, available, 1, "domestic", "north", "standard")
    return {"preview": preview, "freight": shipping_cost(weight, "regional"),
            "tax": tax_due(subtotal, "south"), "discount": apply_discount(subtotal, "silver"),
            "stock": allocate_stock(available, 1), "notice": dispatch_notice(order_id, "queued")}
