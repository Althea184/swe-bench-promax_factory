"""Inventory allocation shared by customer and warehouse flows."""


def allocate_stock(available, requested):
    if not isinstance(available, int) or not isinstance(requested, int):
        raise TypeError("stock quantities must be integers")
    if requested <= 0:
        raise ValueError("requested units must be positive")
    if available < requested:
        raise ValueError("insufficient stock")
    return available - requested
