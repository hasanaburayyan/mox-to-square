"""Stage 4: attach one Scryfall image per tracked item through the Square Catalog API.

Runs after the Dashboard import has landed. Talks to Square's REST API directly with
`requests` (ListCatalog + CreateCatalogImage) so the code does not depend on a specific
SDK generation. Needs SQUARE_ACCESS_TOKEN (ITEMS_WRITE scope) and SQUARE_ENV in .env.

Not yet exercised against a live Square account: run with --dry-run and in the sandbox
environment first.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .naming import record_sku
from .records import Record

SQUARE_API_VERSION = "2025-01-23"
BASE_URLS = {"sandbox": "https://connect.squareupsandbox.com",
             "production": "https://connect.squareup.com"}
IMAGES_STATE_FILENAME = "images_uploaded.json"


class SquareApiError(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, no export, no interpolation."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class SquareClient:
    def __init__(self, access_token: str, env: str = "sandbox", *,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 requests_per_second: float = 3.0):
        if env not in BASE_URLS:
            raise ValueError(f"SQUARE_ENV must be sandbox or production, got {env!r}")
        self.base = BASE_URLS[env]
        self.env = env
        self.sess = session or requests.Session()
        self.sess.headers.update({"Authorization": f"Bearer {access_token}",
                                  "Square-Version": SQUARE_API_VERSION,
                                  "Accept": "application/json"})
        self._sleep = sleep
        self._min_interval = 1.0 / requests_per_second
        self._last = 0.0

    def _request(self, method: str, path: str, *, retries: int = 5, **kw) -> dict[str, Any]:
        for attempt in range(retries):
            wait = self._min_interval - (time.time() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.time()
            resp = self.sess.request(method, self.base + path, timeout=60, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                backoff = float(resp.headers.get("Retry-After") or 2 ** attempt)
                self._sleep(backoff)
                continue
            if resp.status_code >= 400:
                raise SquareApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.content else {}
        raise SquareApiError(f"{method} {path}: gave up after {retries} attempts")

    def list_locations(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/locations").get("locations", [])

    def retrieve_counts(self, variation_ids: list[str], location_id: str) -> dict[str, float]:
        """variation id -> IN_STOCK quantity at the location (absent = never counted)."""
        out: dict[str, float] = {}
        for i in range(0, len(variation_ids), 100):
            body = {"catalog_object_ids": variation_ids[i:i + 100], "location_ids": [location_id],
                    "states": ["IN_STOCK"]}
            cursor = None
            while True:
                if cursor:
                    body["cursor"] = cursor
                data = self._request("POST", "/v2/inventory/counts/batch-retrieve", json=body)
                for c in data.get("counts", []):
                    out[c["catalog_object_id"]] = float(c.get("quantity", 0))
                cursor = data.get("cursor")
                if not cursor:
                    break
        return out

    def change_inventory(self, changes: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v2/inventory/changes/batch-create",
                             json={"idempotency_key": str(uuid.uuid4()), "changes": changes,
                                   "ignore_unchanged_counts": True})

    def delete_objects(self, object_ids: list[str]) -> list[str]:
        """BatchDeleteCatalogObjects, 200 ids per call; returns the ids Square deleted."""
        deleted: list[str] = []
        for i in range(0, len(object_ids), 200):
            data = self._request("POST", "/v2/catalog/batch-delete",
                                 json={"object_ids": object_ids[i:i + 200]})
            deleted.extend(data.get("deleted_object_ids", []))
        return deleted

    def upsert_objects(self, objects: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v2/catalog/batch-upsert",
                             json={"idempotency_key": str(uuid.uuid4()),
                                   "batches": [{"objects": objects}]})

    def list_categories(self) -> list[dict[str, Any]]:
        return self.list_items(types="CATEGORY")

    def list_items(self, types: str = "ITEM") -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"types": types}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v2/catalog/list", params=params)
            objects.extend(data.get("objects", []))
            cursor = data.get("cursor")
            if not cursor:
                return objects

    def create_item_image(self, item_id: str, image_path: Path, caption: str) -> str:
        body = {"idempotency_key": str(uuid.uuid4()), "object_id": item_id, "is_primary": True,
                "image": {"type": "IMAGE", "id": "#image",
                          "image_data": {"caption": caption[:255]}}}
        with open(image_path, "rb") as fh:
            files = {"request": (None, json.dumps(body), "application/json"),
                     "image_file": (image_path.name, fh, "image/jpeg")}
            data = self._request("POST", "/v2/catalog/images", files=files)
        return data.get("image", {}).get("id", "")


def item_ids_by_sku(items: list[dict[str, Any]]) -> dict[str, str]:
    """Map every variation SKU to its parent ITEM id."""
    out: dict[str, str] = {}
    for obj in items:
        for var in obj.get("item_data", {}).get("variations", []) or []:
            sku = var.get("item_variation_data", {}).get("sku")
            if sku:
                out[str(sku)] = obj["id"]
    return out


@dataclass
class ImagePlan:
    item_name: str
    scryfall_id: str
    image_uri: str
    sku: str                     # SKU of the printing whose image was chosen
    all_skus: list[str] = field(default_factory=list)


def choose_images(records: list[Record], include_language: bool = False) -> list[ImagePlan]:
    """One image per item (card name).

    Preference: highest sell price in stock, then the oldest printing (release date), then
    set code. With no prices (pricing.source: none) that means the oldest printing you hold.
    English objects are preferred over language-fallback ones so the scan is in English.
    """
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.tier == "tracked" and r.image_uri and r.count > 0:
            groups[r.name].append(r)
    plans: list[ImagePlan] = []
    for name, recs in sorted(groups.items()):
        best = sorted(recs, key=lambda r: (-(r.sell_price or 0.0), r.lang_fallback,
                                           r.released_at or "9999-99-99", r.set))[0]
        plans.append(ImagePlan(item_name=name, scryfall_id=best.scryfall_id, image_uri=best.image_uri,
                               sku=record_sku(best, include_language),
                               all_skus=sorted({record_sku(r, include_language) for r in recs})))
    return plans


def download_image(uri: str, dest: Path, user_agent: str, session: requests.Session | None = None) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    sess = session or requests.Session()
    resp = sess.get(uri, headers={"User-Agent": user_agent}, timeout=60)
    resp.raise_for_status()
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return dest


def load_images_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_images_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@dataclass
class ImageRunReport:
    planned: int = 0
    uploaded: int = 0
    already_done: int = 0
    stale_dropped: int = 0
    not_in_square: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_images(records: list[Record], *, cache_dir: Path, state_dir: Path, user_agent: str,
               include_language: bool, dry_run: bool, client: SquareClient | None,
               log: Callable[[str], None] = print) -> ImageRunReport:
    plans = choose_images(records, include_language)
    report = ImageRunReport(planned=len(plans))
    state_path = state_dir / IMAGES_STATE_FILENAME
    state = load_images_state(state_path)

    sku_to_item: dict[str, str] = {}
    has_image: set[str] = set()
    if client is not None:
        catalog = client.list_items()
        sku_to_item = item_ids_by_sku(catalog)
        # Square is the source of truth: an item that already carries an image is done,
        # whatever this machine's state file says (state files are per machine).
        has_image = {it["id"] for it in catalog if it.get("item_data", {}).get("image_ids")}
        log(f"Square catalog: {len(sku_to_item)} variation SKUs found, "
            f"{len(has_image)} items already have an image")
        # Entries for items that no longer exist (e.g. a deleted test category) would
        # otherwise block re-uploads to their replacements, so drop them.
        live_ids = set(sku_to_item.values())
        stale = [k for k in state if not k.startswith("sku:") and k not in live_ids]
        if stale:
            for k in stale:
                del state[k]
            save_images_state(state_path, state)
            log(f"dropped {len(stale)} image state entries for items no longer in Square")
            report.stale_dropped = len(stale)

    for plan in plans:
        item_id = next((sku_to_item[s] for s in plan.all_skus if s in sku_to_item), None)
        state_key = item_id or f"sku:{plan.sku}"
        if state.get(state_key) or (item_id is not None and item_id in has_image):
            report.already_done += 1
            continue
        if client is not None and item_id is None:
            report.not_in_square.append(plan.item_name)
            continue
        if dry_run:
            log(f"[dry-run] {plan.item_name}: {plan.image_uri} -> item {item_id or '?'}")
            continue
        try:
            img_path = download_image(plan.image_uri, cache_dir / "images" / f"{plan.scryfall_id}.jpg",
                                      user_agent)
            assert client is not None and item_id is not None
            image_id = client.create_item_image(item_id, img_path, plan.item_name)
            state[state_key] = {"item_name": plan.item_name, "image_id": image_id,
                                "scryfall_id": plan.scryfall_id, "sku": plan.sku}
            save_images_state(state_path, state)
            report.uploaded += 1
            log(f"uploaded {plan.item_name} ({image_id})")
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            report.errors.append(f"{plan.item_name}: {exc}")
            log(f"ERROR {plan.item_name}: {exc}")
    return report
