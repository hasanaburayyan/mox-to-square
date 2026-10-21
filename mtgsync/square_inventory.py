"""Stock counts through the Square Inventory API.

The Dashboard import only applies quantity and stock-alert columns when the location name
in the header matches a location exactly; otherwise it ignores them silently. This path
uses the location ID instead, so it cannot miss:

- mode "set": PHYSICAL_COUNT per variation (stock becomes the Moxfield count).
- mode "add": ADJUSTMENT per variation (stock increases by the Moxfield count), which is
  the intake-log workflow. Each applied batch is fingerprinted in
  state/inventory_batches.json and refused a second time unless --force.

It also turns on inventory tracking and the low-stock alert on every variation it touches,
since the import left those off too.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .square_build import ImportRow, batch_fingerprint

INVENTORY_BATCHES_FILENAME = "inventory_batches.json"


class InventoryError(RuntimeError):
    pass


@dataclass
class InventoryPlan:
    sku: str
    variation_id: str
    item_name: str
    variation_name: str
    quantity: int               # count to set, or delta to add
    current: float | None       # Square's IN_STOCK count before the change
    needs_tracking: bool


@dataclass
class InventoryReport:
    location_id: str = ""
    location_name: str = ""
    mode: str = "set"
    planned: int = 0
    total_quantity: int = 0
    applied: int = 0
    tracking_enabled: int = 0
    not_in_square: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def resolve_location(locations: list[dict[str, Any]], wanted_name: str) -> tuple[str, str]:
    active = [l for l in locations if l.get("status", "ACTIVE") == "ACTIVE"]
    if wanted_name:
        hit = next((l for l in active if str(l.get("name", "")).strip().lower() == wanted_name.strip().lower()), None)
        if hit:
            return hit["id"], hit["name"]
    if len(active) == 1:
        return active[0]["id"], active[0].get("name", "")
    names = [l.get("name") for l in active]
    raise InventoryError(f"square.location_name {wanted_name!r} matches none of the active locations "
                         f"{names}; set it to one of them")


def variations_by_sku(items: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], str]]:
    """SKU -> (variation object, item name)."""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for it in items:
        name = it.get("item_data", {}).get("name", "")
        for v in it.get("item_data", {}).get("variations", []) or []:
            sku = v.get("item_variation_data", {}).get("sku")
            if sku:
                out[str(sku)] = (v, name)
    return out


def plan_inventory(rows: list[ImportRow], by_sku: dict[str, tuple[dict[str, Any], str]],
                   counts: dict[str, float]) -> tuple[list[InventoryPlan], list[str]]:
    plans: list[InventoryPlan] = []
    missing: list[str] = []
    for r in rows:
        hit = by_sku.get(r.sku)
        if hit is None:
            missing.append(r.sku)
            continue
        var, item_name = hit
        data = var.get("item_variation_data", {})
        plans.append(InventoryPlan(
            sku=r.sku, variation_id=var["id"], item_name=item_name,
            variation_name=str(data.get("name", "")), quantity=int(r.quantity),
            current=counts.get(var["id"]),
            needs_tracking=not bool(data.get("track_inventory")),
        ))
    return plans, missing


def tracking_upsert_object(var: dict[str, Any], alert_count: int | None) -> dict[str, Any]:
    """The variation object re-sent with tracking and the low-stock alert enabled."""
    obj = json.loads(json.dumps(var))     # deep copy
    data = obj.setdefault("item_variation_data", {})
    data["track_inventory"] = True
    if alert_count is None:
        data["inventory_alert_type"] = "NONE"
        data.pop("inventory_alert_threshold", None)
    else:
        data["inventory_alert_type"] = "LOW_QUANTITY"
        data["inventory_alert_threshold"] = int(alert_count)
    for k in ("updated_at", "created_at", "is_deleted"):
        obj.pop(k, None)
    return obj


def inventory_changes(plans: list[InventoryPlan], location_id: str, mode: str) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    changes: list[dict[str, Any]] = []
    for p in plans:
        if mode == "set":
            changes.append({"type": "PHYSICAL_COUNT", "physical_count": {
                "catalog_object_id": p.variation_id, "state": "IN_STOCK", "location_id": location_id,
                "quantity": str(p.quantity), "occurred_at": now}})
        else:
            if p.quantity == 0:
                continue
            changes.append({"type": "ADJUSTMENT", "adjustment": {
                "catalog_object_id": p.variation_id, "from_state": "NONE", "to_state": "IN_STOCK",
                "location_id": location_id, "quantity": str(p.quantity), "occurred_at": now}})
    return changes


@dataclass
class DuplicateVariation:
    item_name: str
    sku: str
    keep_id: str
    delete_id: str
    delete_created_at: str
    delete_quantity: float | None


def find_duplicate_variations(items: list[dict[str, Any]], counts: dict[str, float] | None = None
                              ) -> list[DuplicateVariation]:
    """Within each item, variations sharing a SKU. The oldest (created_at) is kept.

    The Dashboard import matches items by name but variations only by Token, so a
    re-import without tokens duplicates every variation."""
    counts = counts or {}
    out: list[DuplicateVariation] = []
    for it in items:
        name = it.get("item_data", {}).get("name", "")
        groups: dict[str, list[dict[str, Any]]] = {}
        for v in it.get("item_data", {}).get("variations", []) or []:
            sku = v.get("item_variation_data", {}).get("sku")
            if sku:
                groups.setdefault(str(sku), []).append(v)
        for sku, vs in groups.items():
            if len(vs) < 2:
                continue
            vs.sort(key=lambda v: (v.get("created_at") or "", v["id"]))
            keep = vs[0]
            for extra in vs[1:]:
                out.append(DuplicateVariation(item_name=name, sku=sku, keep_id=keep["id"],
                                              delete_id=extra["id"],
                                              delete_created_at=str(extra.get("created_at", "")),
                                              delete_quantity=counts.get(extra["id"])))
    return out


def run_dedupe(*, client, location_name: str, dry_run: bool, log: Callable[[str], None] = print
               ) -> tuple[list[DuplicateVariation], list[str]]:
    """Delete the newer copies of duplicated variations. Returns (duplicates, deleted ids)."""
    loc_id, loc_name = resolve_location(client.list_locations(), location_name)
    items = client.list_items()
    ids = [v["id"] for it in items for v in it.get("item_data", {}).get("variations", []) or []]
    counts = client.retrieve_counts(ids, loc_id)
    dups = find_duplicate_variations(items, counts)
    stock = sum(d.delete_quantity or 0 for d in dups)
    log(f"{len(items)} items, {len(ids)} variations, {len(dups)} duplicate variations to delete "
        f"(carrying {stock:g} stock) at {loc_name!r}")
    for d in dups[:15]:
        log(f"  {'[dry-run] ' if dry_run else ''}{d.item_name} / {d.sku}: delete {d.delete_id} "
            f"(created {d.delete_created_at}, qty {d.delete_quantity}) keep {d.keep_id}")
    if len(dups) > 15:
        log(f"  ... {len(dups) - 15} more")
    if dry_run or not dups:
        return dups, []
    deleted = client.delete_objects([d.delete_id for d in dups])
    log(f"deleted {len(deleted)} variations")
    return dups, deleted


def _load_batches(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("batches", [])


def run_inventory(rows: list[ImportRow], *, client, location_name: str, mode: str,
                  alert_count: int | None, state_dir: Path, dry_run: bool, force: bool = False,
                  log: Callable[[str], None] = print) -> InventoryReport:
    if mode not in ("set", "add"):
        raise InventoryError(f"mode must be set or add, got {mode!r}")
    report = InventoryReport(mode=mode)
    loc_id, loc_name = resolve_location(client.list_locations(), location_name)
    report.location_id, report.location_name = loc_id, loc_name
    if location_name and loc_name.lower() != location_name.lower():
        log(f"note: square.location_name is {location_name!r} but the account's only location is "
            f"{loc_name!r}; using it")

    by_sku = variations_by_sku(client.list_items())
    ids = [v["id"] for v, _ in by_sku.values()]
    counts = client.retrieve_counts(ids, loc_id)
    plans, missing = plan_inventory(rows, by_sku, counts)
    report.planned = len(plans)
    report.total_quantity = sum(p.quantity for p in plans)
    report.not_in_square = missing

    fp = batch_fingerprint(rows)
    batches_path = state_dir / INVENTORY_BATCHES_FILENAME
    earlier = next((b for b in _load_batches(batches_path) if b["fingerprint"] == fp), None)
    log(f"{mode}: {len(plans)} variations at {loc_name!r}, total quantity {report.total_quantity}, "
        f"{sum(p.needs_tracking for p in plans)} need tracking enabled, {len(missing)} SKUs not in Square")
    if mode == "add" and earlier and not force:
        msg = (f"this exact batch was already applied on {earlier['applied_at']}; "
               "adding it again would double stock (use --force if that is intended)")
        if not dry_run:
            raise InventoryError(msg)
        log("WARNING: " + msg)
    if dry_run:
        for p in plans[:15]:
            cur = "none" if p.current is None else f"{p.current:g}"
            log(f"  [dry-run] {p.sku} {p.item_name} / {p.variation_name}: current {cur} -> "
                f"{'set ' if mode == 'set' else '+'}{p.quantity}")
        if len(plans) > 15:
            log(f"  ... {len(plans) - 15} more")
        return report

    # 1. tracking + alert on the variations (catalog upsert, 1000 objects per batch limit)
    to_fix = [tracking_upsert_object(by_sku[p.sku][0], alert_count) for p in plans
              if p.needs_tracking or (by_sku[p.sku][0].get("item_variation_data", {}).get("inventory_alert_threshold") != alert_count)]
    for i in range(0, len(to_fix), 500):
        try:
            client.upsert_objects(to_fix[i:i + 500])
            report.tracking_enabled += len(to_fix[i:i + 500])
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"tracking upsert batch {i // 500}: {exc}")
            log(f"ERROR enabling tracking: {exc}")
            return report

    # 2. the counts (100 changes per request)
    changes = inventory_changes(plans, loc_id, mode)
    for i in range(0, len(changes), 100):
        chunk = changes[i:i + 100]
        try:
            client.change_inventory(chunk)
            report.applied += len(chunk)
            log(f"applied {report.applied}/{len(changes)} changes")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"inventory batch {i // 100}: {exc}")
            log(f"ERROR applying inventory: {exc}")
            break

    if report.applied and not report.errors:
        batches = _load_batches(batches_path)
        batches.append({"fingerprint": fp, "mode": mode, "applied_at": datetime.now().isoformat(timespec="seconds"),
                        "variations": len(plans), "quantity": report.total_quantity, "location": loc_name})
        state_dir.mkdir(parents=True, exist_ok=True)
        batches_path.write_text(json.dumps({"batches": batches[-200:]}, indent=1), encoding="utf-8")
    return report
