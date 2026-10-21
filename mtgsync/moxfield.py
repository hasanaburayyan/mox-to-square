"""Moxfield collection CSV reader with header validation.

Moxfield has renamed export columns before, so every header this module relies on is
declared in REQUIRED_HEADERS / OPTIONAL_HEADERS and checked up front. A mismatch raises
MoxfieldHeaderError listing exactly what is missing, so the failure is loud and early.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

REQUIRED_HEADERS = ("Count", "Name", "Edition", "Collector Number", "Condition", "Language", "Foil")
OPTIONAL_HEADERS = ("Tradelist Count", "Tags", "Alter", "Proxy", "Purchase Price", "Last Modified")

# Moxfield's current export wording (verified against a real export, Oct 2026):
#   Mint, Near Mint, Good (Lightly Played), Played, Heavily Played, Damaged
# plus older/alternate spellings and the short codes.
CONDITION_MAP = {
    "mint": "NM", "m": "NM", "near mint": "NM", "nm": "NM",
    "good (lightly played)": "LP", "good": "LP", "lightly played": "LP", "lp": "LP",
    "light play": "LP",
    "played": "MP", "moderately played": "MP", "mp": "MP", "moderate play": "MP",
    "heavily played": "HP", "hp": "HP", "heavy play": "HP",
    "damaged": "DMG", "dmg": "DMG", "d": "DMG",
}

# Moxfield language names -> Scryfall lang codes. Unknown values pass through lowercased.
LANGUAGE_MAP = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
    "portuguese": "pt", "japanese": "ja", "korean": "ko", "russian": "ru",
    "simplified chinese": "zhs", "chinese simplified": "zhs", "traditional chinese": "zht",
    "chinese traditional": "zht", "hebrew": "he", "latin": "la", "ancient greek": "grc",
    "arabic": "ar", "sanskrit": "sa", "phyrexian": "ph",
}

FINISH_MAP = {"": "nonfoil", "nonfoil": "nonfoil", "non-foil": "nonfoil", "normal": "nonfoil",
              "foil": "foil", "etched": "etched", "etched foil": "etched"}

TRUE_VALUES = {"true", "yes", "y", "1", "x"}


class MoxfieldHeaderError(ValueError):
    pass


class MoxfieldRowError(ValueError):
    pass


@dataclass(frozen=True)
class MoxfieldRow:
    line_no: int
    count: int
    tradelist_count: int
    name: str
    edition: str            # set code, lowercased
    collector_number: str   # whitespace stripped, original case
    condition: str          # NM | LP | MP | HP | DMG
    language: str           # Scryfall lang code
    finish: str             # nonfoil | foil | etched
    tags: str
    alter: bool
    proxy: bool
    purchase_price: float | None


def _to_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


def _to_int(value: str | None, field: str, line_no: int) -> int:
    raw = (value or "").strip()
    if raw == "":
        return 0
    try:
        return int(float(raw))
    except ValueError as exc:
        raise MoxfieldRowError(f"line {line_no}: {field} is not a number: {raw!r}") from exc


def _to_price(value: str | None) -> float | None:
    raw = (value or "").strip().lstrip("$")
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def validate_headers(headers: list[str]) -> list[str]:
    """Return the optional headers that are absent; raise on missing required ones."""
    present = {h.strip() for h in headers}
    missing = [h for h in REQUIRED_HEADERS if h not in present]
    if missing:
        raise MoxfieldHeaderError(
            "Moxfield export is missing required column(s): "
            + ", ".join(missing)
            + f". Found: {', '.join(sorted(present))}. "
            "If Moxfield renamed a column, update REQUIRED_HEADERS in mtgsync/moxfield.py."
        )
    return [h for h in OPTIONAL_HEADERS if h not in present]


def parse_row(raw: dict[str, str], line_no: int) -> MoxfieldRow:
    cond_raw = (raw.get("Condition") or "").strip().lower()
    condition = CONDITION_MAP.get(cond_raw)
    if condition is None:
        raise MoxfieldRowError(f"line {line_no}: unknown Condition {raw.get('Condition')!r}")

    finish_raw = (raw.get("Foil") or "").strip().lower()
    finish = FINISH_MAP.get(finish_raw)
    if finish is None:
        raise MoxfieldRowError(f"line {line_no}: unknown Foil value {raw.get('Foil')!r}")

    lang_raw = (raw.get("Language") or "English").strip().lower()
    language = LANGUAGE_MAP.get(lang_raw, lang_raw)

    name = (raw.get("Name") or "").strip()
    edition = (raw.get("Edition") or "").strip().lower()
    collector_number = "".join((raw.get("Collector Number") or "").split())
    if not name or not edition or not collector_number:
        raise MoxfieldRowError(f"line {line_no}: Name, Edition and Collector Number are all required")

    return MoxfieldRow(
        line_no=line_no,
        count=_to_int(raw.get("Count"), "Count", line_no),
        tradelist_count=_to_int(raw.get("Tradelist Count"), "Tradelist Count", line_no),
        name=name,
        edition=edition,
        collector_number=collector_number,
        condition=condition,
        language=language,
        finish=finish,
        tags=(raw.get("Tags") or "").strip(),
        alter=_to_bool(raw.get("Alter")),
        proxy=_to_bool(raw.get("Proxy")),
        purchase_price=_to_price(raw.get("Purchase Price")),
    )


def read_moxfield_csv(path: Path | str) -> tuple[list[MoxfieldRow], list[str], list[str]]:
    """Read a Moxfield collection export.

    Returns (rows, missing_optional_headers, row_errors). Rows that fail to parse are
    skipped and described in row_errors rather than aborting the whole run.
    """
    rows: list[MoxfieldRow] = []
    errors: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise MoxfieldHeaderError(f"{path}: empty file")
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        missing_optional = validate_headers(reader.fieldnames)
        for line_no, raw in enumerate(reader, start=2):
            if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
                continue
            try:
                rows.append(parse_row(raw, line_no))
            except MoxfieldRowError as exc:
                errors.append(str(exc))
    return rows, missing_optional, errors
