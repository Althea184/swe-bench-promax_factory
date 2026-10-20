"""Cross-region operational dashboard."""
from shop.core import shipping, tax, discount, inventory, notice


def operations_row(order_id, amount, weight, available):
    return (shipping.shipping_cost(weight, "international"),
            tax.tax_due(amount, "export"),
            discount.apply_discount(amount, "gold"),
            inventory.allocate_stock(available, 2),
            notice.dispatch_notice(order_id, "delivered"))
