"""Validate the correct baseline example, not the untrusted PR head."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('review_target', Path(__file__).parents[1] / 'examples/review_target.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


def test_subtotal_counts_every_unit_without_mutating_items():
    items = [{'price_cents': 1250, 'quantity': 3}, {'price_cents': 0, 'quantity': 2}]
    assert target.subtotal(items) == 3750
    assert items == [{'price_cents': 1250, 'quantity': 3}, {'price_cents': 0, 'quantity': 2}]
    assert target.subtotal([]) == 0


@pytest.mark.parametrize('price,quantity', [(-1, 1), (10, 0), (10, -1), (1.5, 1), (10, 1.5), (True, 1), (10, True)])
def test_subtotal_rejects_invalid_inputs(price, quantity):
    with pytest.raises(ValueError):
        target.subtotal([{'price_cents': price, 'quantity': quantity}])


@pytest.mark.parametrize('amount,fee', [(0, 500), (4999, 500), (5000, 0), (5001, 0)])
def test_shipping_inclusive_free_boundary(amount, fee):
    assert target.shipping_fee(amount) == fee


@pytest.mark.parametrize('amount', [-1, 1.5, True])
def test_shipping_rejects_invalid_inputs(amount):
    with pytest.raises(ValueError):
        target.shipping_fee(amount)
