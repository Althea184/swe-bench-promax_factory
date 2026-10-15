from money import render_amount


def quote_line(price):
    return f"Quote: {render_amount(price, 'EUR')}"
