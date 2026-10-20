"""Tax estimate for order and refund flows."""


def tax_due(amount, region):
    if amount < 0:
        raise ValueError("amount must be nonnegative")
    if region not in {"north", "south", "export"}:
        raise ValueError("unsupported region")
    rate = {"north": 0.08, "south": 0.06, "export": 0.0}[region]
    return round(amount * rate, 2)
