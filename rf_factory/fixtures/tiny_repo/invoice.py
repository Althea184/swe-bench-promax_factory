def invoice_total(items, discount=0):
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between zero and one")
    subtotal = 0
    for price, quantity in items:
        if price < 0:
            raise ValueError("negative price")
        if quantity < 0:
            raise ValueError("negative quantity")
        subtotal += price * quantity
    reduction = subtotal * discount
    total = subtotal - reduction
    return round(total, 2)
