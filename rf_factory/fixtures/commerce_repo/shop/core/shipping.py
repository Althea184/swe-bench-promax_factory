"""Shipping quotes shared by checkout, fulfillment, reports, and partners."""


def shipping_cost(weight, zone="domestic"):
    if weight <= 0:
        raise ValueError("weight must be positive")
    if zone not in {"domestic", "regional", "international"}:
        raise ValueError("unsupported zone")
    rate = {"domestic": 2.0, "regional": 3.5, "international": 7.0}[zone]
    handling = 1.5 if weight > 10 else 0.0
    return round(weight * rate + handling, 2)
