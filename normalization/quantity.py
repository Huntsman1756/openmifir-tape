"""Quantity / notional sanity checks for G0-C.

Guardrail: sanity is REPORTED, never used to coerce or fill values. All raw
values are preserved verbatim. Insufficient data -> status INSUFFICIENT, never a
guessed value.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .model import SanityResult


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def notional_vs_quantity_price(
    quantity: Any,
    notional_amount: Any,
    price: Any,
    price_notation: str | None,
) -> SanityResult:
    """Report whether notional is consistent with quantity x price.

    For PERC (percentage) notation, price is a percentage, so notional = quantity
    * price / 100. For non-percentage notation, notional = quantity * price.
    Only reported; never applied.
    """
    q = _dec(quantity)
    n = _dec(notional_amount)
    p = _dec(price)
    if q is None or n is None or p is None:
        return SanityResult(
            check="notional_vs_quantity_price",
            status="INSUFFICIENT",
            detail="need quantity, notional and price; raw values preserved",
        )
    divisor = Decimal(100) if (price_notation or "").upper() == "PERC" else Decimal(1)
    expected = q * p / divisor
    # Relative tolerance 1e-6 avoids float/rounding noise.
    ok = abs(n) == 0 if abs(expected) == 0 else abs(n - expected) / abs(expected) < Decimal("1e-6")
    return SanityResult(
        check="notional_vs_quantity_price",
        status="PASS" if ok else "FLAG",
        detail=f"expected={expected} actual={n}",
    )
