"""Tracked vs bulk tier assignment and bulk bucket definitions.

`tier.threshold_usd: 0` means Model A: every matched card is tracked. A positive threshold
switches to Model C: a printing is tracked when its base price for any finish in stock is
at or above the threshold, and once tracked it stays tracked (state/tracked.json).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .records import Record

TRACKED_STATE_FILENAME = "tracked.json"


@dataclass(frozen=True)
class BulkBucket:
    name: str
    rarities: tuple[str, ...]
    finish: str          # nonfoil | foil  (etched counts as foil)
    price: float

    def matches(self, rarity: str, finish: str) -> bool:
        f = "foil" if finish in ("foil", "etched") else "nonfoil"
        return rarity in self.rarities and self.finish == f

    @property
    def variation_name(self) -> str:
        return "Foil" if self.finish == "foil" else "Nonfoil"


def load_buckets(raw: Iterable[dict[str, Any]]) -> list[BulkBucket]:
    return [BulkBucket(name=str(b["name"]), rarities=tuple(str(r).lower() for r in b["rarities"]),
                       finish=str(b.get("finish", "nonfoil")).lower(), price=float(b["price"]))
            for b in raw]


def bucket_for(buckets: list[BulkBucket], rarity: str, finish: str) -> BulkBucket | None:
    return next((b for b in buckets if b.matches(rarity.lower(), finish)), None)


def load_tracked_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as fh:
        return set(json.load(fh).get("tracked", []))


def save_tracked_state(path: Path, tracked: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"tracked": sorted(tracked)}, fh, indent=0)
    tmp.replace(path)


def assign_tiers(records: list[Record], threshold: float, buckets: list[BulkBucket],
                 previously_tracked: set[str]) -> set[str]:
    """Set rec.tier / rec.bulk_bucket in place and return the new tracked set."""
    tracked = set(previously_tracked)
    if threshold <= 0:
        tracked.update(r.printing_key for r in records)
    else:
        for rec in records:
            if rec.base_price is not None and rec.base_price >= threshold:
                tracked.add(rec.printing_key)

    for rec in records:
        if rec.printing_key in tracked:
            rec.tier = "tracked"
            rec.bulk_bucket = None
        else:
            rec.tier = "bulk"
            b = bucket_for(buckets, rec.rarity, rec.finish)
            rec.bulk_bucket = b.name if b else None
    return tracked
