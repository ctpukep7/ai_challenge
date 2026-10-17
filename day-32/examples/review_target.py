"""Isolated pricing example for reproducible review PRs; amounts are integer cents."""


def subtotal(items):
    """Sum all units, rejecting negative prices and non-positive quantities."""
    total = 0
    for item in items:
        price = item['price_cents']
        quantity = item['quantity']
        if type(price) is not int or price < 0:
            raise ValueError('price_cents must be a non-negative integer')
        if type(quantity) is not int or quantity <= 0:
            raise ValueError('quantity must be a positive integer')
        total += price
    return total


def shipping_fee(subtotal_cents):
    """Shipping is free at 5000 cents and above, otherwise costs 500 cents."""
    if type(subtotal_cents) is not int or subtotal_cents < 0:
        raise ValueError('subtotal_cents must be a non-negative integer')
    return 0 if subtotal_cents > 5000 else 500
