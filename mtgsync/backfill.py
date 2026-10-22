"""Add missing images to stock that is already in Square (uploaded before image support).

No Moxfield file is involved: the catalog itself is the input. Every variation's SKU names
its printing (`<set>-<number>-<finish>-<condition>[-<lang>]`), so the Scryfall object is
found from the SKU alone, and the two image passes from square_images run over those
records. Both passes treat Square as the source of truth, so items and variations that
already carry an image are left alone and the backfill can be run as often as needed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings
from .naming import FINISH_CODE, record_sku, sku_collector_number
from .records import Record
from .scryfall import ScryfallCard, ScryfallIndex, load_index
from .square_images import ImageRunReport, VariationImageReport, run_images, run_variation_images

_CODE_TO_FINISH = {v: k for k, v in FINISH_CODE.items()}          # nf -> nonfoil, ...
_CONDITIONS = ("nm", "lp", "mp", "hp", "dmg")
_SKU_RE = re.compile(
    r"^(?P<set>[a-z0-9]+)-(?P<number>[a-z0-9]+)-(?P<finish>nf|f|e)-(?P<condition>nm|lp|mp|hp|dmg)"
    r"(?:-(?P<lang>[a-z]{2,3}))?$")


@dataclass(frozen=True)
class ParsedSku:
    set: str
    number: str            # as it appears in the SKU: ASCII-safe form of the collector number
    finish: str            # nonfoil | foil | etched
    condition: str         # NM | LP | ...
    lang: str              # Scryfall code; "en" when the SKU has no suffix


def parse_sku(sku: str) -> ParsedSku | None:
    m = _SKU_RE.match(sku.strip().lower())
    if not m:
        return None
    return ParsedSku(set=m["set"], number=m["number"], finish=_CODE_TO_FINISH[m["finish"]],
                     condition=m["condition"].upper(), lang=m["lang"] or "en")


class PrintingLookup:
    """(set, SKU-form collector number) -> the Scryfall objects of that printing by language."""

    def __init__(self, index: ScryfallIndex):
        self._by_printing: dict[tuple[str, str], dict[str, ScryfallCard]] = defaultdict(dict)
        for card in index.cards():
            self._by_printing[(card.set, sku_collector_number(card.collector_number))].setdefault(card.lang, card)

    def find(self, set_code: str, number: str, lang: str = "en") -> ScryfallCard | None:
        langs = self._by_printing.get((set_code, number))
        if not langs:
            return None
        return langs.get(lang) or langs.get("en") or next(iter(langs.values()))


def records_from_catalog(items: list[dict[str, Any]], index: ScryfallIndex,
                         category_id: str | None = None) -> tuple[list[Record], list[str]]:
    """One Record per variation in Square whose SKU names a printing Scryfall knows.

    Returns (records, skipped) where skipped describes every variation left out and why.
    The record's SKU (with the language suffix on) round-trips to the Square SKU, which is
    how the image passes find the item and variation again.
    """
    lookup = PrintingLookup(index)
    records: list[Record] = []
    skipped: list[str] = []
    for item in items:
        data = item.get("item_data", {}) or {}
        if category_id and not any(c.get("id") == category_id for c in data.get("categories", []) or []) \
                and (data.get("reporting_category") or {}).get("id") != category_id \
                and data.get("category_id") != category_id:
            continue
        item_name = str(data.get("name", "")).strip()
        for var in data.get("variations", []) or []:
            sku = str(var.get("item_variation_data", {}).get("sku") or "").strip()
            if not sku:
                skipped.append(f"{item_name}: variation without SKU")
                continue
            parsed = parse_sku(sku)
            if parsed is None:
                skipped.append(f"{item_name}: SKU {sku!r} is not in mtgsync's format")
                continue
            card = lookup.find(parsed.set, parsed.number, parsed.lang)
            if card is None:
                skipped.append(f"{item_name}: SKU {sku!r} matches no Scryfall printing")
                continue
            rec = Record(moxfield_line=0, count=1, tradelist_count=0, condition=parsed.condition,
                         finish=parsed.finish, tier="tracked", scryfall_id=card.id,
                         name=item_name or card.name, set=card.set, set_name=card.set_name,
                         collector_number=card.collector_number, rarity=card.rarity, lang=parsed.lang,
                         lang_fallback=card.lang != parsed.lang, type_line=card.type_line,
                         prices=dict(card.prices), image_uri=card.image_uri, released_at=card.released_at)
            if record_sku(rec, include_language=True) != sku.lower():
                skipped.append(f"{item_name}: SKU {sku!r} does not round-trip "
                               f"({record_sku(rec, include_language=True)!r})")
                continue
            if not rec.image_uri:
                skipped.append(f"{item_name}: Scryfall has no image for {sku}")
                continue
            records.append(rec)
    return records, skipped


@dataclass
class BackfillReport:
    dry_run: bool = False
    items_seen: int = 0
    variations_seen: int = 0
    variations_matched: int = 0
    skipped: list[str] = field(default_factory=list)
    item_images: ImageRunReport = field(default_factory=ImageRunReport)
    variation_images: VariationImageReport = field(default_factory=VariationImageReport)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


ALL_CATEGORIES = ""     # pass as `category` to look at every item in the library


def category_names(client) -> list[str]:
    """The account's category names, sorted, for a chooser."""
    return sorted({str(c.get("category_data", {}).get("name", "")).strip()
                   for c in client.list_categories()} - {""}, key=str.lower)


def run_backfill(settings: Settings, client, *, category: str | None = None, dry_run: bool = False,
                 offline: bool = False, index: ScryfallIndex | None = None,
                 log: Callable[[str], None] = print) -> BackfillReport:
    """`category`: name of the (reporting) category to limit to; None = `square.category`
    from settings; ALL_CATEGORIES ("") = every item in the library."""
    s = settings
    report = BackfillReport(dry_run=dry_run)
    for key in ("paths.cache", "paths.state"):
        s.path(key).mkdir(parents=True, exist_ok=True)

    log("Reading the Square catalog ...")
    items = client.list_items()
    category_name = (str(s.get("square.category", "") or "") if category is None else category).strip()
    category_id = None
    if category_name:
        cat = next((c for c in client.list_categories()
                    if str(c.get("category_data", {}).get("name", "")).strip().lower() == category_name.lower()), None)
        if cat:
            category_id = cat["id"]
            log(f"  limiting to category {category_name!r}")
        else:
            report.errors.append(f"category {category_name!r} not found in Square; "
                                 f"available: {', '.join(category_names(client)) or 'none'}")
            log("ERROR: " + report.errors[-1])
            return report
    else:
        log("  looking at every item in the library")
    report.items_seen = len(items)
    report.variations_seen = sum(len(i.get("item_data", {}).get("variations", []) or []) for i in items)
    log(f"  {report.items_seen} items, {report.variations_seen} variations")

    if index is None:
        log("Loading Scryfall card data ...")
        index = load_index(s.path("paths.cache"), ttl_hours=float(s.get("scryfall.cache_ttl_hours", 24)),
                           offline=offline, user_agent=str(s.get("scryfall.user_agent")),
                           bulk_type=str(s.get("scryfall.bulk_type", "default_cards")), log=log)
    records, skipped = records_from_catalog(items, index, category_id)
    report.variations_matched = len(records)
    report.skipped = skipped
    log(f"  {len(records)} variations matched to a Scryfall printing, {len(skipped)} skipped")
    for line in skipped[:20]:
        log("  skipped: " + line)
    if len(skipped) > 20:
        log(f"  ... {len(skipped) - 20} more skipped")
    if not records:
        return report

    kw = dict(cache_dir=s.path("paths.cache"), state_dir=s.path("paths.state"),
              user_agent=str(s.get("scryfall.user_agent")), include_language=True,
              dry_run=dry_run, client=client, log=log)
    log("Item images (one per card name) ...")
    report.item_images = run_images(records, **kw)
    r1 = report.item_images
    log(f"  {r1.planned} items in scope, {r1.uploaded} uploaded, {r1.already_done} already had one, "
        f"{len(r1.errors)} errors")
    report.errors.extend(r1.errors)

    if bool(s.get("images.variations", True)):
        log("Variation images (one per printing) ...")
        report.variation_images = run_variation_images(records, **kw)
        r2 = report.variation_images
        log(f"  {r2.planned} printings in scope, {r2.uploaded} uploaded, {r2.reused} reused, "
            f"{r2.attached} variations got an image, {r2.already_done} printings already done, "
            f"{len(r2.errors)} errors")
        report.errors.extend(r2.errors)
    return report
