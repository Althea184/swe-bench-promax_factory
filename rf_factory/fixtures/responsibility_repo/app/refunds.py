def final_price(amount, rate):
    if amount < 0:
        raise ValueError("amount must be nonnegative")
    if not 0 <= rate <= 1:
        raise ValueError("rate out of range")
    return round(amount * (1 - rate), 2)


def refund_amount(amount, rate):
    return final_price(amount, rate)
