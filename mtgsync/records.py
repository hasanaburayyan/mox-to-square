"""The enriched record: one per Moxfield row, carried through every stage as JSONL."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Any, Iterable, Iterator

ENRICHED_FILENAME = "enriched.jsonl"
PRICED_FILENAME = "priced.jsonl"


@dataclass
class Record:
    # From Moxfield
    moxfield_line: int
    count: int
    tradelist_count: int
    condition: str          # NM | LP | MP | HP | DMG
    finish: str             # nonfoil | foil | etched
    tags: str = ""
    alter: bool = False
    purchase_price: float | None = None
    # From Scryfall
    scryfall_id: str = ""
    name: str = ""
    set: str = ""
    set_name: str = ""
    collector_number: str = ""
    rarity: str = ""
    lang: str = "en"
    lang_fallback: bool = False     # True when a non-English row was matched to the English object
    type_line: str = ""
    prices: dict[str, Any] | None = None
    image_uri: str | None = None
    released_at: str | None = None
    finish_available: bool = True   # False when Scryfall lists no such finish for this printing
    # From Stage 2
    base_price: float | None = None
    price_source: str | None = None
    price_as_of: str | None = None
    price_condition: str | None = None
    sell_price: float | None = None
    tier: str | None = None         # tracked | bulk
    bulk_bucket: str | None = None
    needs_manual_price: bool = False

    @property
    def printing_key(self) -> str:
        return f"{self.set}-{self.collector_number}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def write_jsonl(path: Path, records: Iterable[Record]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[Record]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Record.from_dict(json.loads(line))
