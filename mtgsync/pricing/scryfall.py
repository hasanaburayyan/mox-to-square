"""Default price source: the `prices` block already present on the enriched record."""

from __future__ import annotations

from datetime import date

from ..records import Record
from .base import PriceQuote

FINISH_TO_FIELD = {"nonfoil": "usd", "foil": "usd_foil", "etched": "usd_etched"}


class ScryfallPriceSource:
    name = "scryfall"

    def __init__(self, as_of: str | None = None):
        self.as_of = as_of or date.today().isoformat()

    def get(self, scryfall_id: str, set_code: str, collector_number: str, finish: str,
            record: Record | None = None) -> PriceQuote | None:
        if record is None or not record.prices:
            return None
        raw = record.prices.get(FINISH_TO_FIELD[finish])
        if raw in (None, ""):
            return None
        try:
            price = float(raw)
        except (TypeError, ValueError):
            return None
        return PriceQuote(price=price, currency="USD", source=self.name,
                          as_of=self.as_of, condition="NM")
