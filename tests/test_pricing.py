from pathlib import Path

from mtgsync.pricing import PricingRule, ScryfallPriceSource, StarCityPriceSource
from mtgsync.records import Record


def _rec(**kw) -> Record:
    base = dict(moxfield_line=1, count=1, tradelist_count=0, condition="NM", finish="nonfoil",
                name="Lightning Bolt", set="sta", set_name="STA", collector_number="42",
                rarity="rare", lang="en", scryfall_id="x",
                prices={"usd": "2.40", "usd_foil": "5.10", "usd_etched": None})
    base.update(kw)
    return Record(**base)


def test_round_up_to_quarter():
    rule = PricingRule(markup=1.0, round_to=0.25)
    assert rule.sell_price(2.40) == 2.50
    assert rule.sell_price(2.50) == 2.50
    assert rule.sell_price(0.01) == 0.25
    assert rule.sell_price(17.33) == 17.50


def test_markup_then_round_then_floor():
    rule = PricingRule(markup=1.1, round_to=0.25, floor=1.00)
    assert rule.sell_price(2.40) == 2.75      # 2.64 -> 2.75
    assert rule.sell_price(0.08) == 1.00      # 0.088 -> 0.25 -> floor 1.00


def test_condition_multiplier():
    rule = PricingRule(markup=1.0, round_to=0.25, condition_multipliers={"NM": 1.0, "LP": 0.9})
    assert rule.sell_price(10.00, "LP") == 9.00
    assert rule.sell_price(10.00, "NM") == 10.00
    assert rule.sell_price(10.00, "HP") == 10.00   # unknown condition -> 1.0


def test_scryfall_source_picks_field_by_finish():
    src = ScryfallPriceSource(as_of="2026-10-18")
    rec = _rec()
    assert src.get("x", "sta", "42", "nonfoil", record=rec).price == 2.40
    assert src.get("x", "sta", "42", "foil", record=rec).price == 5.10
    assert src.get("x", "sta", "42", "etched", record=rec) is None


def test_scg_source_with_fake_fetcher(tmp_path: Path):
    calls: list[str] = []

    def fetcher(name: str):
        calls.append(name)
        return [
            {"set": "sta", "collector_number": "42", "finish": "nonfoil", "condition": "LP",
             "price": 2.00, "in_stock": True},
            {"set": "sta", "collector_number": "42", "finish": "nonfoil", "condition": "NM",
             "price": 2.99, "in_stock": True},
            {"set": "sta", "collector_number": "42", "finish": "nonfoil", "condition": "NM",
             "price": 1.50, "in_stock": False},
        ]

    slept: list[float] = []
    src = StarCityPriceSource(tmp_path / "scg", fetcher=fetcher, rate_limit_seconds=2,
                              max_cards=1, sleep=slept.append)
    q = src.get("x", "sta", "42", "nonfoil", record=_rec())
    assert q is not None and q.price == 2.99 and q.condition == "NM"   # NM in stock preferred
    # Second lookup for the same card comes from the disk cache: no new request.
    q2 = src.get("x", "sta", "42", "foil", record=_rec(finish="foil"))
    assert q2 is None and calls == ["Lightning Bolt"] and src.stats.cache_hits == 1
    # A different card is over the per-run cap.
    assert src.get("y", "neo", "141", "nonfoil", record=_rec(name="Fable")) is None
    assert src.stats.cap_skipped == 1


def test_scg_without_fetcher_returns_none(tmp_path: Path):
    src = StarCityPriceSource(tmp_path / "scg")
    assert src.get("x", "sta", "42", "nonfoil", record=_rec()) is None
    assert src.stats.not_implemented == 1


def test_scg_fetcher_error_falls_through(tmp_path: Path):
    def boom(name: str):
        raise RuntimeError("503")

    src = StarCityPriceSource(tmp_path / "scg", fetcher=boom, sleep=lambda s: None)
    assert src.get("x", "sta", "42", "nonfoil", record=_rec()) is None
    assert src.stats.errors == 1
