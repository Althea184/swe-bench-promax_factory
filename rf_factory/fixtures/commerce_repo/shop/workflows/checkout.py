"""Customer checkout composes pricing and fulfillment services."""
from shop.services.pricing import checkout_breakdown
from shop.services.fulfillment import plan_parcel, mark_shipped


def checkout_preview(subtotal, weight, available, units, zone, region, tier):
    charges = checkout_breakdown(subtotal, weight, zone, region, tier)
    parcel = plan_parcel(available, units, weight, zone)
    return {"charges": charges, "parcel": parcel}


def ship_order(order_id, available, units):
    return mark_shipped(order_id, available, units)
