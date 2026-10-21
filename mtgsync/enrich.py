"""Stage 1: join Moxfield rows to Scryfall card objects."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .moxfield import MoxfieldRow
from .records import Record
from .scryfall import ScryfallIndex


@dataclass
class Unmatched:
    line_no: int
    name: str
    edition: str
    collector_number: str
    language: str
    reason: str


@dataclass
class EnrichStats:
    rows_in: int = 0
    proxies_dropped: int = 0
    non_english_dropped: int = 0
    zero_count_dropped: int = 0
    matched: int = 0
    unmatched: int = 0
    lang_fallbacks: int = 0
    finish_mismatches: list[str] = field(default_factory=list)
    name_mismatches: list[str] = field(default_factory=list)


def enrich_rows(rows: list[MoxfieldRow], index: ScryfallIndex, settings: Settings
                ) -> tuple[list[Record], list[Unmatched], EnrichStats]:
    overrides = settings.set_code_overrides
    subtract_tradelist = bool(settings.get("moxfield.subtract_tradelist", False))
    english_only = bool(settings.get("moxfield.english_only", False))

    records: list[Record] = []
    unmatched: list[Unmatched] = []
    stats = EnrichStats(rows_in=len(rows))

    for row in rows:
        if row.proxy:
            stats.proxies_dropped += 1
            continue
        if english_only and row.language != "en":
            stats.non_english_dropped += 1
            continue
        count = row.count - row.tradelist_count if subtract_tradelist else row.count
        if count <= 0:
            stats.zero_count_dropped += 1
            continue

        set_code = overrides.get(row.edition, row.edition)
        card = index.lookup(set_code, row.collector_number, row.language)
        lang_fallback = False
        if card is None and row.language != "en":
            # default_cards carries one language per card, so a non-English row usually
            # has no own object. Same set + number is the same printing: use the English
            # object and keep the row's language on the record.
            card = index.lookup(set_code, row.collector_number, "en")
            lang_fallback = card is not None
            if lang_fallback:
                stats.lang_fallbacks += 1
        if card is None:
            reason = f"set/number not in Scryfall ({set_code} #{row.collector_number})"
            if set_code != row.edition:
                reason += f" (after override from {row.edition})"
            unmatched.append(Unmatched(row.line_no, row.name, row.edition, row.collector_number,
                                       row.language, reason))
            stats.unmatched += 1
            continue

        finish_available = row.finish in card.finishes
        if not finish_available:
            stats.finish_mismatches.append(
                f"line {row.line_no}: {card.name} ({card.set} #{card.collector_number}) "
                f"has finishes {list(card.finishes)}, Moxfield says {row.finish}")
        # Name mismatches are informational: Moxfield may print only the front face.
        if row.name.lower() not in card.name.lower():
            stats.name_mismatches.append(
                f"line {row.line_no}: Moxfield {row.name!r} vs Scryfall {card.name!r}")

        records.append(Record(
            moxfield_line=row.line_no, count=count, tradelist_count=row.tradelist_count,
            condition=row.condition, finish=row.finish, tags=row.tags, alter=row.alter,
            purchase_price=row.purchase_price,
            scryfall_id=card.id, name=card.name, set=card.set, set_name=card.set_name,
            collector_number=card.collector_number, rarity=card.rarity,
            lang=row.language if lang_fallback else card.lang, lang_fallback=lang_fallback,
            type_line=card.type_line, prices=dict(card.prices), image_uri=card.image_uri,
            released_at=card.released_at, finish_available=finish_available,
        ))
        stats.matched += 1

    return records, unmatched, stats


def write_unmatched_csv(path: Path, unmatched: list[Unmatched]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["moxfield_line", "name", "edition", "collector_number", "language", "reason"])
        for u in unmatched:
            w.writerow([u.line_no, u.name, u.edition, u.collector_number, u.language, u.reason])
