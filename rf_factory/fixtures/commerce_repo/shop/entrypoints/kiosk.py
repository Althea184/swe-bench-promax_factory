"""In-store terminal uses the checkout workflow without importing core APIs."""
from shop.workflows.checkout import checkout_preview, ship_order


def terminal_summary(subtotal, weight, available):
    preview = checkout_preview(subtotal, weight, available, 1, "domestic", "north", "standard")
    return preview["charges"]["total"], preview["parcel"]["remaining"]


def terminal_dispatch(order_id, available):
    return ship_order(order_id, available, 1)
