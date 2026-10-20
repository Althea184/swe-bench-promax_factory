"""Commands used by warehouse and support staff."""
import shop.core.shipping
from shop.public import tax_due, apply_discount, allocate_stock, dispatch_notice
from shop.workflows.checkout import checkout_preview


def quick_quote(weight, amount):
    return shop.core.shipping.shipping_cost(weight, "domestic") + tax_due(amount, "north")


def support_snapshot(order_id, amount, available):
    return (apply_discount(amount, "gold"), allocate_stock(available, 1),
            dispatch_notice(order_id, "shipped"))


def preview_for_staff(subtotal, weight, available):
    return checkout_preview(subtotal, weight, available, 1, "regional", "south", "silver")
