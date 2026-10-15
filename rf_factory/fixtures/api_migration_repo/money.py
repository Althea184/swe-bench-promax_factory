from decimal import Decimal


def render_amount(amount, currency):
    if currency not in {"USD", "EUR"}:
        raise ValueError("unsupported currency")
    value = Decimal(str(amount))
    if value < 0:
        raise ValueError("negative amount")
    sign = "$" if currency == "USD" else "€"
    return f"{sign}{value:.2f}"
