"""Loyalty tier pricing."""


def apply_discount(amount, tier="standard"):
    if amount < 0:
        raise ValueError("amount must be nonnegative")
    if tier not in {"standard", "silver", "gold"}:
        raise ValueError("unsupported tier")
    rate = {"standard": 0.0, "silver": 0.05, "gold": 0.12}[tier]
    return round(amount * (1 - rate), 2)
