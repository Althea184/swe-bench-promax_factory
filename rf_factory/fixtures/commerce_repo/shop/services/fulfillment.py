"""Warehouse-facing fulfillment operations."""
import shop.core.shipping as shipping
from shop.core.inventory import allocate_stock
from shop.core.notice import dispatch_notice


def plan_parcel(available, units, weight, zone):
    left = allocate_stock(available, units)
    return {"remaining": left, "freight": shipping.shipping_cost(weight, zone)}


def mark_shipped(order_id, available, units):
    left = allocate_stock(available, units)
    return left, dispatch_notice(order_id, "shipped")
