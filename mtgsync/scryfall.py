"""Scryfall bulk data: download `default_cards`, build a compact (set, number, lang) index.

The raw bulk file is several hundred MB. We parse it once, keep only the fields the
pipeline needs, and write that trimmed index next to it so later runs load in seconds.
Never call the per-card API from here.
"""

from __future__ import annotations

import gzip
import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

import requests

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"
BULK_TYPE = "default_cards"      # one language per card; "all_cards" has every language
RAW_FILENAME = "scryfall_default_cards.json"     # JSON-array format (older API, test fixture)
INDEX_FILENAME = "scryfall_index.json"
INDEX_VERSION = 1
# Scryfall serves bulk data as gzipped JSONL (`jsonl_download_uri`, Oct 2026). The older
# plain JSON array (`download_uri`) is still read if that is what was cached.
RAW_SUFFIXES = (".jsonl.gz", ".jsonl", ".json")


def raw_filename(bulk_type: str, suffix: str = ".jsonl.gz") -> str:
    return f"scryfall_{bulk_type}{suffix}"


def find_raw_file(cache_dir: Path, bulk_type: str) -> Path | None:
    for suffix in RAW_SUFFIXES:
        p = cache_dir / raw_filename(bulk_type, suffix)
        if p.exists():
            return p
    return None


def iter_cards(path: Path) -> Iterable[dict[str, Any]]:
    """Yield card objects from a cached bulk file in whichever format it was saved."""
    name = path.name
    if name.endswith(".jsonl.gz") or name.endswith(".jsonl"):
        opener = gzip.open if name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    else:
        yield from iter_json_array(path)


def iter_json_array(path: Path, chunk_size: int = 1 << 20) -> Iterable[dict[str, Any]]:
    """Stream the objects of a top-level JSON array without loading the whole file.

    The Scryfall bulk files are hundreds of MB; json.load would need several GB of RAM.
    """
    decoder = json.JSONDecoder()
    skip = re.compile(r"[\s,]*")
    with open(path, encoding="utf-8") as fh:
        buf = fh.read(chunk_size)
        pos = skip.match(buf).end()
        if pos >= len(buf) or buf[pos] != "[":
            raise ScryfallError(f"{path} does not start with a JSON array")
        pos += 1
        while True:
            # Skip separators, pulling more data if the buffer is exhausted.
            while True:
                pos = skip.match(buf, pos).end()
                if pos < len(buf):
                    break
                more = fh.read(chunk_size)
                if not more:
                    return
                buf, pos = buf[pos:] + more, 0
            if buf[pos] == "]":
                return
            # Decode one object, extending the buffer until it is complete.
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    break
                except json.JSONDecodeError:
                    more = fh.read(chunk_size)
                    if not more:
                        raise
                    buf, pos = buf[pos:] + more, 0
            yield obj
            pos = end
            if pos > chunk_size:
                buf, pos = buf[pos:], 0


class ScryfallError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScryfallCard:
    id: str
    name: str
    set: str
    set_name: str
    collector_number: str
    rarity: str
    lang: str
    type_line: str
    finishes: tuple[str, ...]
    prices: dict[str, str | None]
    image_uri: str | None
    released_at: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.set, self.collector_number, self.lang)


def _is_fresh(path: Path, ttl_hours: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600


def trim_card(card: dict[str, Any]) -> dict[str, Any]:
    """Reduce a full Scryfall card object to the fields we keep."""
    image_uris = card.get("image_uris")
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris")
    type_line = card.get("type_line")
    if not type_line and card.get("card_faces"):
        type_line = " // ".join(f.get("type_line", "") for f in card["card_faces"])
    return {
        "id": card["id"],
        "name": card["name"],
        "set": card["set"].lower(),
        "set_name": card.get("set_name", ""),
        "collector_number": str(card["collector_number"]),
        "rarity": card.get("rarity", ""),
        "lang": card.get("lang", "en"),
        "type_line": type_line or "",
        "finishes": list(card.get("finishes") or []),
        "prices": card.get("prices") or {},
        "image_uri": (image_uris or {}).get("normal"),
        "released_at": card.get("released_at"),
    }


def ensure_bulk_file(cache_dir: Path, ttl_hours: float = 24, offline: bool = False,
                     user_agent: str = "mtgsync/0.1", session: requests.Session | None = None,
                     bulk_type: str = BULK_TYPE, log=None) -> Path:
    """Return the path to the cached bulk file (None if only the trimmed index exists),
    downloading when the cache is stale."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = find_raw_file(cache_dir, bulk_type)
    if offline:
        if existing is None and not (cache_dir / INDEX_FILENAME).exists():
            raise ScryfallError("--offline set but no cached Scryfall data in " + str(cache_dir))
        return existing
    if existing is not None and _is_fresh(existing, ttl_hours):
        return existing

    sess = session or requests.Session()
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    resp = sess.get(BULK_INDEX_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    entry = next((e for e in resp.json().get("data", []) if e.get("type") == bulk_type), None)
    if entry is None:
        raise ScryfallError(f"bulk-data index has no {bulk_type!r} entry")
    if entry.get("jsonl_download_uri"):
        uri, suffix, size = entry["jsonl_download_uri"], ".jsonl.gz", entry.get("compressed_size", 0)
    elif entry.get("download_uri"):
        uri, suffix, size = entry["download_uri"], ".json", entry.get("size", 0)
    else:
        raise ScryfallError(f"bulk-data entry for {bulk_type!r} has no download URI; "
                            f"fields: {sorted(entry)}")
    if log:
        log(f"Scryfall: downloading {bulk_type} ({size / 1e6:.0f} MB) "
            f"updated {entry.get('updated_at', '?')}")

    raw_path = cache_dir / raw_filename(bulk_type, suffix)
    tmp = raw_path.with_name(raw_path.name + ".part")
    with sess.get(uri, headers=headers, stream=True, timeout=600) as dl:
        dl.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in dl.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(raw_path)
    # Older-format copies and the trimmed index are now stale.
    for s in RAW_SUFFIXES:
        if s != suffix:
            (cache_dir / raw_filename(bulk_type, s)).unlink(missing_ok=True)
    (cache_dir / INDEX_FILENAME).unlink(missing_ok=True)
    return raw_path


class ScryfallIndex:
    """Lookup table keyed by (set, collector_number, lang)."""

    def __init__(self, cards: Iterable[dict[str, Any]]):
        self._by_key: dict[tuple[str, str, str], ScryfallCard] = {}
        self._by_id: dict[str, ScryfallCard] = {}
        for c in cards:
            card = ScryfallCard(
                id=c["id"], name=c["name"], set=c["set"], set_name=c["set_name"],
                collector_number=c["collector_number"], rarity=c["rarity"], lang=c["lang"],
                type_line=c["type_line"], finishes=tuple(c["finishes"]), prices=c["prices"],
                image_uri=c.get("image_uri"), released_at=c.get("released_at"),
            )
            self._by_key[card.key] = card
            self._by_id[card.id] = card

    def __len__(self) -> int:
        return len(self._by_key)

    def lookup(self, set_code: str, collector_number: str, lang: str = "en") -> ScryfallCard | None:
        return self._by_key.get((set_code.lower(), collector_number, lang))

    def languages_for(self, set_code: str, collector_number: str) -> list[str]:
        set_code = set_code.lower()
        return sorted(k[2] for k in self._by_key if k[0] == set_code and k[1] == collector_number)

    def by_id(self, scryfall_id: str) -> ScryfallCard | None:
        return self._by_id.get(scryfall_id)

    def cards(self) -> Iterable[ScryfallCard]:
        return self._by_key.values()

    # --- persistence -------------------------------------------------------------------

    @classmethod
    def from_raw_file(cls, raw_path: Path) -> "ScryfallIndex":
        return cls(trim_card(c) for c in iter_cards(raw_path))

    def save(self, index_path: Path) -> None:
        payload = {"version": INDEX_VERSION,
                   "cards": [asdict(c) | {"finishes": list(c.finishes)} for c in self._by_key.values()]}
        tmp = index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(index_path)

    @classmethod
    def load(cls, index_path: Path) -> "ScryfallIndex":
        with open(index_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("version") != INDEX_VERSION:
            raise ScryfallError("stale index version; delete " + str(index_path))
        return cls(payload["cards"])


def load_index(cache_dir: Path, ttl_hours: float = 24, offline: bool = False,
               user_agent: str = "mtgsync/0.1", bulk_type: str = BULK_TYPE, log=None) -> ScryfallIndex:
    """Get a ready-to-use index, downloading and/or re-trimming only when needed."""
    cache_dir = Path(cache_dir)
    index_path = cache_dir / INDEX_FILENAME
    raw_path = ensure_bulk_file(cache_dir, ttl_hours, offline, user_agent, bulk_type=bulk_type, log=log)
    if index_path.exists() and (raw_path is None
                                or index_path.stat().st_mtime >= raw_path.stat().st_mtime):
        try:
            return ScryfallIndex.load(index_path)
        except ScryfallError:
            pass
    if raw_path is None:
        raise ScryfallError("no Scryfall bulk file available in " + str(cache_dir))
    if log:
        log(f"Scryfall: building trimmed index from {raw_path.name} (one-time, a minute or two)")
    index = ScryfallIndex.from_raw_file(raw_path)
    index.save(index_path)
    return index
