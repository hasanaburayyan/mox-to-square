"""Star City Games price source (opt-in, experimental).

SCG has no official API, so this is a scraper. This module ships the plumbing the brief
asks for (disk cache with TTL, rate limit, per-run card cap, graceful fallback) around a
single `fetcher` callable that does the actual HTTP work. The live fetcher is NOT
implemented yet: SCG's storefront search endpoint and its terms of service need to be
confirmed first. Until then, construct the source with a `fetcher` (tests use a fake
one) or it will return None for every card and count the miss in `stats`.

A fetcher has the signature  fetcher(card_name: str) -> list[dict]  where each dict is
{"set": str, "collector_number": str, "finish": str, "condition": str,
 "price": float, "in_stock": bool}.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..records import Record
from .base import PriceQuote

Listing = dict
Fetcher = Callable[[str], list[Listing]]

CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DMG"]


@dataclass
class ScgStats:
    requests: int = 0
    cache_hits: int = 0
    cap_skipped: int = 0
    errors: int = 0
    no_result: int = 0
    not_implemented: int = 0
    error_messages: list[str] = field(default_factory=list)


def _cache_key(card_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", card_name.lower()).strip("_") or "card"


class StarCityPriceSource:
    name = "scg"

    def __init__(self, cache_dir: Path, *, fetcher: Fetcher | None = None,
                 rate_limit_seconds: float = 2.0, max_cards: int = 200,
                 cache_ttl_hours: float = 168, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher
        self.rate_limit_seconds = rate_limit_seconds
        self.max_cards = max_cards
        self.cache_ttl_seconds = cache_ttl_hours * 3600
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self._live_lookups = 0
        self.stats = ScgStats()

    # --- cache -------------------------------------------------------------------------

    def _cache_path(self, card_name: str) -> Path:
        return self.cache_dir / f"{_cache_key(card_name)}.json"

    def _read_cache(self, card_name: str) -> list[Listing] | None:
        p = self._cache_path(card_name)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - float(payload.get("fetched_at", 0)) > self.cache_ttl_seconds:
            return None
        return payload.get("listings", [])

    def _write_cache(self, card_name: str, listings: list[Listing]) -> None:
        payload = {"fetched_at": self._clock(),
                   "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
                   "listings": listings}
        self._cache_path(card_name).write_text(json.dumps(payload), encoding="utf-8")

    # --- fetching ----------------------------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self.rate_limit_seconds - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def listings_for(self, card_name: str) -> list[Listing] | None:
        cached = self._read_cache(card_name)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached
        if self.fetcher is None:
            self.stats.not_implemented += 1
            return None
        if self._live_lookups >= self.max_cards:
            self.stats.cap_skipped += 1
            return None
        self._throttle()
        self._live_lookups += 1
        self.stats.requests += 1
        try:
            listings = self.fetcher(card_name)
        except Exception as exc:  # noqa: BLE001 - any scrape failure falls back to Scryfall
            self.stats.errors += 1
            self.stats.error_messages.append(f"{card_name}: {exc}")
            return None
        self._write_cache(card_name, listings)
        return listings

    # --- PriceSource -------------------------------------------------------------------

    def get(self, scryfall_id: str, set_code: str, collector_number: str, finish: str,
            record: Record | None = None) -> PriceQuote | None:
        if record is None:
            return None
        listings = self.listings_for(record.name)
        if not listings:
            if listings is not None:
                self.stats.no_result += 1
            return None
        matches = [l for l in listings
                   if str(l.get("set", "")).lower() == set_code.lower()
                   and str(l.get("collector_number", "")) == collector_number
                   and str(l.get("finish", "nonfoil")) == finish
                   and l.get("in_stock", True)]
        if not matches:
            self.stats.no_result += 1
            return None
        # NM preferred; otherwise the lowest in-stock price.
        nm = [m for m in matches if str(m.get("condition", "NM")).upper() == "NM"]
        chosen = min(nm, key=lambda m: m["price"]) if nm else min(matches, key=lambda m: m["price"])
        return PriceQuote(price=float(chosen["price"]), currency="USD", source=self.name,
                          as_of=datetime.now(timezone.utc).date().isoformat(),
                          condition=str(chosen.get("condition", "NM")).upper(), in_stock=True)
