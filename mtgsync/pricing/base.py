"""PriceSource interface and the PricingRule that turns a base price into a sell price."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Protocol

from ..records import Record


@dataclass(frozen=True)
class PriceQuote:
    price: float
    currency: str
    source: str
    as_of: str              # ISO date or datetime
    condition: str          # condition the quote applies to, usually NM
    in_stock: bool | None = None


class PriceSource(Protocol):
    name: str

    def get(self, scryfall_id: str, set_code: str, collector_number: str, finish: str,
            record: Record | None = None) -> PriceQuote | None:
        """Return a quote for this printing+finish, or None when the source has nothing."""
        ...


class NullPriceSource:
    """No prices at all: the import leaves Price blank and prices are set in Square."""
    name = "none"

    def get(self, scryfall_id: str, set_code: str, collector_number: str, finish: str,
            record: Record | None = None) -> PriceQuote | None:
        return None


@dataclass(frozen=True)
class PricingRule:
    markup: float = 1.0
    round_to: float = 0.25
    floor: float | None = None
    condition_multipliers: dict[str, float] | None = None

    def condition_multiplier(self, condition: str) -> float:
        if not self.condition_multipliers:
            return 1.0
        return float(self.condition_multipliers.get(condition, 1.0))

    def sell_price(self, base_price: float, condition: str = "NM") -> float:
        """base * condition multiplier * markup, rounded UP to round_to, then floored."""
        price = Decimal(str(base_price)) * Decimal(str(self.condition_multiplier(condition))) \
            * Decimal(str(self.markup))
        step = Decimal(str(self.round_to))
        if step > 0:
            price = (price / step).quantize(Decimal("1"), rounding=ROUND_CEILING) * step
        if self.floor is not None and price < Decimal(str(self.floor)):
            price = Decimal(str(self.floor))
        return float(price.quantize(Decimal("0.01")))
