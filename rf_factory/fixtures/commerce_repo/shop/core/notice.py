"""Customer-facing dispatch status text."""


def dispatch_notice(order_id, status):
    if status not in {"queued", "shipped", "delivered"}:
        raise ValueError("unsupported dispatch status")
    if not str(order_id).strip():
        raise ValueError("order id is required")
    return f"Order {order_id}: {status}"
