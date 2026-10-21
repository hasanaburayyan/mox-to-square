"""Stage 2: attach base_price, sell_price and tier to every record."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .pricing import (NullPriceSource, PriceSource, PricingRule, ScryfallPriceSource,
                      StarCityPriceSource)
from .records import Record
from .tiering import (BulkBucket, assign_tiers, bucket_for, load_buckets, load_tracked_state,
                      save_tracked_state, TRACKED_STATE_FILENAME)


@dataclass
class PriceStats:
    priced: int = 0
    fallback_to_scryfall: int = 0
    null_price: int = 0
    tracked: int = 0
    bulk: int = 0
    bulk_unbucketed: int = 0
    warnings: list[str] = field(default_factory=list)


def build_rule(settings: Settings, floor: float | None = None) -> PricingRule:
    return PricingRule(
        markup=float(settings.get("pricing.markup", 1.0)),
        round_to=float(settings.get("pricing.round_to", 0.25)),
        floor=floor,
        condition_multipliers={str(k).upper(): float(v)
                               for k, v in (settings.get("pricing.condition_multipliers") or {}).items()},
    )


def build_source(settings: Settings, name: str | None = None) -> PriceSource:
    name = (name or settings.get("pricing.source", "scryfall")).lower()
    if name == "none":
        return NullPriceSource()
    if name == "scryfall":
        return ScryfallPriceSource()
    if name == "scg":
        return StarCityPriceSource(
            cache_dir=settings.path("paths.cache") / "scg",
            rate_limit_seconds=float(settings.get("pricing.scg.rate_limit_seconds", 2)),
            max_cards=int(settings.get("pricing.scg.max_cards", 200)),
            cache_ttl_hours=float(settings.get("pricing.scg.cache_ttl_hours", 168)),
        )
    raise ValueError(f"unknown price source {name!r} (expected none, scryfall or scg)")


def quote_record(rec: Record, source: PriceSource, fallback: PriceSource | None, stats: PriceStats):
    q = source.get(rec.scryfall_id, rec.set, rec.collector_number, rec.finish, record=rec)
    if q is None and fallback is not None:
        q = fallback.get(rec.scryfall_id, rec.set, rec.collector_number, rec.finish, record=rec)
        if q is not None:
            stats.fallback_to_scryfall += 1
    return q


def price_records(records: list[Record], settings: Settings, *, source_name: str | None = None,
                  source: PriceSource | None = None, state_dir: Path | None = None
                  ) -> PriceStats:
    """Mutates records in place. Returns stats."""
    stats = PriceStats()
    source = source or build_source(settings, source_name)
    no_prices = source.name == "none"
    fallback = ScryfallPriceSource() if source.name not in ("scryfall", "none") else None
    buckets: list[BulkBucket] = load_buckets(settings.get("tier.bulk_buckets") or [])
    threshold = float(settings.get("tier.threshold_usd", 0) or 0)
    state_dir = state_dir or settings.path("paths.state")
    tracked_path = state_dir / TRACKED_STATE_FILENAME

    # 1. base price
    for rec in records:
        q = quote_record(rec, source, fallback, stats)
        if q is None:
            rec.base_price = None
            rec.price_source = None
            rec.price_as_of = None
            rec.price_condition = None
            stats.null_price += 1
        else:
            rec.base_price = q.price
            rec.price_source = q.source
            rec.price_as_of = q.as_of
            rec.price_condition = q.condition
            stats.priced += 1

    # 2. tier
    tracked = assign_tiers(records, threshold, buckets, load_tracked_state(tracked_path))
    save_tracked_state(tracked_path, tracked)

    # 3. sell price
    base_rule = build_rule(settings)
    for rec in records:
        bucket = bucket_for(buckets, rec.rarity, rec.finish)
        bucket_price = bucket.price if bucket else None
        if rec.tier == "bulk":
            stats.bulk += 1
            if bucket is None:
                stats.bulk_unbucketed += 1
                stats.warnings.append(
                    f"{rec.name} ({rec.set} #{rec.collector_number}, {rec.rarity}, {rec.finish}) "
                    "is bulk but matches no bulk bucket; it will be skipped")
            rec.sell_price = bucket_price
            continue
        stats.tracked += 1
        if no_prices:
            rec.sell_price = None           # Price column left blank; set in Square
            continue
        if rec.base_price is None:
            # Null base price: bulk bucket price, and flag for manual review.
            rec.sell_price = bucket_price
            rec.needs_manual_price = True
            continue
        rule = PricingRule(markup=base_rule.markup, round_to=base_rule.round_to,
                           floor=bucket_price, condition_multipliers=base_rule.condition_multipliers)
        rec.sell_price = rule.sell_price(rec.base_price, rec.condition)

    if hasattr(source, "stats"):
        s = source.stats
        stats.warnings.append(f"scg: requests={s.requests} cache_hits={s.cache_hits} "
                              f"cap_skipped={s.cap_skipped} errors={s.errors} "
                              f"no_result={s.no_result} not_implemented={s.not_implemented}")
        stats.warnings.extend(s.error_messages[:20])
    return stats


def write_needs_manual_price(path: Path, records: list[Record]) -> int:
    rows = [r for r in records if r.needs_manual_price]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "set", "collector_number", "finish", "condition", "count",
                    "assigned_price", "moxfield_line"])
        for r in rows:
            w.writerow([r.name, r.set, r.collector_number, r.finish, r.condition, r.count,
                        r.sell_price if r.sell_price is not None else "", r.moxfield_line])
    return len(rows)


def write_price_comparison(path: Path, records: list[Record], settings: Settings,
                           scg: StarCityPriceSource | None = None) -> int:
    """--compare-prices: both sources side by side, nothing imported."""
    scryfall = ScryfallPriceSource()
    scg = scg or build_source(settings, "scg")
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "set", "collector_number", "finish", "condition", "count",
                    "scryfall_price", "scg_price", "scg_condition", "scg_in_stock", "ratio_scg_to_scryfall"])
        for rec in records:
            a = scryfall.get(rec.scryfall_id, rec.set, rec.collector_number, rec.finish, record=rec)
            b = scg.get(rec.scryfall_id, rec.set, rec.collector_number, rec.finish, record=rec)
            ratio = ""
            if a and b and a.price:
                ratio = f"{b.price / a.price:.3f}"
            w.writerow([rec.name, rec.set, rec.collector_number, rec.finish, rec.condition, rec.count,
                        a.price if a else "", b.price if b else "", b.condition if b else "",
                        "" if b is None else b.in_stock, ratio])
            n += 1
    return n
