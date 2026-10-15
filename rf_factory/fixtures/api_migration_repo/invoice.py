from money import render_amount


def invoice_line(price, quantity):
    return f"Invoice total: {render_amount(price * quantity, 'USD')}"


def invoice_tax(tax):
    return f"Tax: {render_amount(tax, 'USD')}"
