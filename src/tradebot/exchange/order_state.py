"""Roostoo orders execute in full or not at all. Keep raw responses for audit.

FilledQuantity is a placeholder on some PENDING and CANCELED responses. Only
FILLED plus a valid execution price can establish an execution.
"""
from dataclasses import dataclass
import math


class OrderEvidenceError(ValueError):
    pass


def canonical_status(value):
    status = str(value or '').upper()
    return 'CANCELED' if status == 'CANCELLED' else status


def number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise OrderEvidenceError(f'invalid {name}') from None
    if not math.isfinite(result) or result < 0:
        raise OrderEvidenceError(f'invalid {name}')
    return result


@dataclass(frozen=True)
class OrderExecution:
    status: str
    quantity: float
    filled: float
    price: float
    fee: float | None
    fee_currency: str


def normalize_order(row, expected=None):
    status = canonical_status(row.get('Status'))
    if status not in {'PENDING', 'CANCELED', 'REJECTED', 'FILLED'}:
        raise OrderEvidenceError(f'unsupported Roostoo order status {status}')
    quantity = number(row.get('Quantity'), 'Quantity')
    if quantity <= 0:
        raise OrderEvidenceError('Quantity must be positive')
    if expected is not None:
        identities = {'Pair': expected['coin'] + '/USD', 'Side': expected['side']}
        if expected.get('order_id') is not None:
            identities['OrderID'] = str(expected['order_id'])
        for key, value in identities.items():
            if str(row.get(key)) != value:
                raise OrderEvidenceError(f'order identity mismatch: {key}')
        if not math.isclose(quantity, expected['quantity'], rel_tol=1e-9, abs_tol=1e-12):
            raise OrderEvidenceError('order quantity differs from accepted quantity')
    filled = number(row.get('FilledQuantity', 0), 'FilledQuantity')
    price = number(row.get('FilledAverPrice', 0), 'FilledAverPrice')
    fee = None if row.get('CommissionChargeValue') in (None, '') else number(row['CommissionChargeValue'], 'commission')
    currency = str(row.get('CommissionCoin') or 'USD').upper()
    movements = [number(row[k], k) for k in ('CoinChange', 'UnitChange') if k in row]
    if status != 'FILLED':
        if price or any(movements) or (fee and row.get('Side') != 'SHORT_OPEN'):
            raise OrderEvidenceError(f'{status} has contradictory execution evidence')
        if filled and not math.isclose(filled, quantity, rel_tol=1e-9, abs_tol=1e-12):
            raise OrderEvidenceError(f'{status} has unsupported fractional execution quantity')
        return OrderExecution(status, quantity, 0., 0., 0., currency)
    if not math.isclose(filled, quantity, rel_tol=1e-9, abs_tol=1e-12) or price <= 0:
        raise OrderEvidenceError('FILLED requires the full accepted quantity and a positive execution price')
    if movements and any(v <= 0 for v in movements):
        raise OrderEvidenceError('FILLED contradicts zero asset movement')
    for field, expected_value in [('CoinChange',quantity),('UnitChange',quantity*price)]:
        if field in row and not math.isclose(float(row[field]), expected_value, rel_tol=1e-8, abs_tol=1e-6 if field=='UnitChange' else 1e-12):
            raise OrderEvidenceError(f'FILLED contradicts {field}')
    if fee and currency != 'USD' and row.get('Side') in {'BUY', 'SELL'}:
        raise OrderEvidenceError(f'unsupported non-USD commission: {currency}')
    return OrderExecution(status, quantity, quantity, price, fee, currency)


def pending_quantity(row):
    """Never deduct a supposed partial execution from a resting reservation."""
    if canonical_status(row.get('Status', 'PENDING')) != 'PENDING':
        return 0.
    # Some short adapters expose collateral without a quantity; exposure uses
    # that explicit collateral directly, not a fabricated execution quantity.
    return number(row.get('Quantity', 0 if row.get('Side')=='SHORT_OPEN' else None), 'Quantity')
