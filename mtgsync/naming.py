"""Item name, variation name and SKU rules (Model A tracked tier)."""

from __future__ import annotations

import re

from .records import Record

FINISH_CODE = {"nonfoil": "nf", "foil": "f", "etched": "e"}
FINISH_LABEL = {"nonfoil": "Nonfoil", "foil": "Foil", "etched": "Etched"}

_SKU_SEP = "-"
_NON_SKU_CHARS = re.compile(r"[^a-z0-9]")


def sku_collector_number(collector_number: str) -> str:
    """Collector numbers may contain letters, daggers and stars; keep the SKU ASCII-safe.

    `123a` -> `123a`, `★123` -> `star123`, `GN3-1` -> `gn31`.
    """
    s = collector_number.strip().lower().replace("★", "star").replace("†", "dagger")
    return _NON_SKU_CHARS.sub("", s)


def make_sku(set_code: str, collector_number: str, finish: str, condition: str,
             lang: str = "en", include_language: bool = False) -> str:
    parts = [set_code.lower(), sku_collector_number(collector_number),
             FINISH_CODE[finish], condition.lower()]
    if include_language and lang != "en":
        parts.append(lang.lower())
    return _SKU_SEP.join(parts)


def record_sku(rec: Record, include_language: bool = False) -> str:
    return make_sku(rec.set, rec.collector_number, rec.finish, rec.condition,
                    rec.lang, include_language)


def item_name(rec: Record) -> str:
    """Card name exactly as Scryfall prints it, `//` for double-faced cards."""
    return rec.name


def variation_name(rec: Record, include_language: bool = False) -> str:
    base = f"{rec.set_name} #{rec.collector_number} · {rec.condition} · {FINISH_LABEL[rec.finish]}"
    if include_language and rec.lang != "en":
        base += f" · {rec.lang.upper()}"
    return base


def description(rec: Record) -> str:
    """Type line only.

    Square requires every variation row of an item to carry the same description (and
    category); rows that disagree make Square drop the whole item on import. The set name
    varies between printings of one card, so it stays in the variation name instead.
    """
    return rec.type_line or ""
