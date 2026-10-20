"""Pricing combines delivery, tax, and loyalty rules."""
from shop.core.shipping import shipping_cost
from shop.core.tax import tax_due
from shop.core.discount import apply_discount


def checkout_breakdown(subtotal, weight, zone, region, tier):
    discounted = apply_discount(subtotal, tier)
    delivery = shipping_cost(weight, zone)
    tax = tax_due(discounted, region)
    return {"items": discounted, "delivery": delivery, "tax": tax,
            "total": round(discounted + delivery + tax, 2)}


def express_total(subtotal, weight, region):
    items = apply_discount(subtotal, "standard")
    return round(items + shipping_cost(weight, "regional") + tax_due(items, region), 2)
