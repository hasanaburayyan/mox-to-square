"""Create items and variations through the Square Catalog API (no Dashboard import).

Matching rules, in order, for each item (card name) in the build:
1. any of its SKUs already exists in Square -> that item; only the missing variations are added;
2. an item with the same name exists -> same, so Square never gets two items with one name;
3. otherwise the item is created with all of its variations.

Existing variations are never modified here. Prices: blank (VARIABLE_PRICING) unless the
row carries one. Tracking and the low-stock alert are set at creation time.
"""

from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .square_build import ImportRow


@dataclass
class CatalogPlan:
    category_name: str
    category_id: str | None             # None = must be created
    new_items: list[dict[str, Any]] = field(default_factory=list)       # ITEM objects with variations
    new_variations: list[dict[str, Any]] = field(default_factory=list)  # standalone ITEM_VARIATION objects
    existing_variations: int = 0
    new_item_names: list[str] = field(default_factory=list)
    items_extended: list[str] = field(default_factory=list)

    @property
    def new_variation_count(self) -> int:
        return len(self.new_variations) + sum(len(i["item_data"]["variations"]) for i in self.new_items)


def variation_object(row: ImportRow, temp_id: str, item_id: str, ordinal: int,
                     alert_count: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "item_id": item_id, "name": row.variation_name, "sku": row.sku, "ordinal": ordinal,
        "track_inventory": True, "sellable": True, "stockable": True,
    }
    if row.price is None:
        data["pricing_type"] = "VARIABLE_PRICING"
    else:
        data["pricing_type"] = "FIXED_PRICING"
        data["price_money"] = {"amount": int(round(row.price * 100)), "currency": "USD"}
    if alert_count is None:
        data["inventory_alert_type"] = "NONE"
    else:
        data["inventory_alert_type"] = "LOW_QUANTITY"
        data["inventory_alert_threshold"] = int(alert_count)
    return {"type": "ITEM_VARIATION", "id": temp_id, "present_at_all_locations": True,
            "item_variation_data": data}


def item_object(name: str, rows: list[ImportRow], temp_id: str, category_id: str | None,
                alert_count: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name, "product_type": "REGULAR"}
    desc = rows[0].description
    if desc:
        data["description_html"] = f"<p>{html.escape(desc)}</p>"
    if category_id:
        data["categories"] = [{"id": category_id}]
        data["reporting_category"] = {"id": category_id}
    data["variations"] = [variation_object(r, f"{temp_id}-v{i}", temp_id, i, alert_count)
                          for i, r in enumerate(rows, start=1)]
    return {"type": "ITEM", "id": temp_id, "present_at_all_locations": True, "item_data": data}


def plan_catalog(rows: list[ImportRow], items: list[dict[str, Any]], categories: list[dict[str, Any]],
                 category_name: str, alert_count: int | None) -> CatalogPlan:
    cat = next((c for c in categories
                if str(c.get("category_data", {}).get("name", "")).strip().lower() == category_name.strip().lower()),
               None)
    plan = CatalogPlan(category_name=category_name, category_id=cat["id"] if cat else None)

    sku_to_item: dict[str, dict[str, Any]] = {}
    name_to_item: dict[str, dict[str, Any]] = {}
    for it in items:
        name_to_item.setdefault(str(it.get("item_data", {}).get("name", "")).strip().lower(), it)
        for v in it.get("item_data", {}).get("variations", []) or []:
            sku = v.get("item_variation_data", {}).get("sku")
            if sku:
                sku_to_item[str(sku)] = it

    groups: dict[str, list[ImportRow]] = defaultdict(list)
    for r in rows:
        if r.tier == "tracked" or r.tier == "bulk":
            groups[r.item_name].append(r)

    for n, (name, group) in enumerate(sorted(groups.items()), start=1):
        target = next((sku_to_item[r.sku] for r in group if r.sku in sku_to_item), None) \
            or name_to_item.get(name.strip().lower())
        if target is None:
            plan.new_items.append(item_object(name, group, f"#item{n}", plan.category_id, alert_count))
            plan.new_item_names.append(name)
            continue
        existing_skus = {str(v.get("item_variation_data", {}).get("sku"))
                         for v in target.get("item_data", {}).get("variations", []) or []}
        ordinal = len(existing_skus)
        added = False
        for r in group:
            if r.sku in existing_skus:
                plan.existing_variations += 1
                continue
            ordinal += 1
            plan.new_variations.append(variation_object(r, f"#var{n}-{ordinal}", target["id"], ordinal, alert_count))
            added = True
        if added:
            plan.items_extended.append(name)
    return plan


def apply_catalog(client, plan: CatalogPlan, log: Callable[[str], None] = print,
                  batch_size: int = 100) -> dict[str, int]:
    """Create the category (if needed), new items, then new variations. Returns counts."""
    stats = {"category_created": 0, "items_created": 0, "variations_created": 0}
    if plan.category_id is None:
        data = client.upsert_objects([{"type": "CATEGORY", "id": "#cat", "present_at_all_locations": True,
                                       "category_data": {"name": plan.category_name}}])
        mapping = {m["client_object_id"]: m["object_id"] for m in data.get("id_mappings", [])}
        plan.category_id = mapping.get("#cat") or next(
            (o["id"] for o in data.get("objects", []) if o.get("type") == "CATEGORY"), None)
        if not plan.category_id:
            raise RuntimeError("category upsert returned no id")
        stats["category_created"] = 1
        log(f"created category {plan.category_name!r}")
        for it in plan.new_items:
            it["item_data"]["categories"] = [{"id": plan.category_id}]
            it["item_data"]["reporting_category"] = {"id": plan.category_id}

    for i in range(0, len(plan.new_items), batch_size):
        chunk = plan.new_items[i:i + batch_size]
        client.upsert_objects(chunk)
        stats["items_created"] += len(chunk)
        stats["variations_created"] += sum(len(it["item_data"]["variations"]) for it in chunk)
        log(f"created items {stats['items_created']}/{len(plan.new_items)}")
    for i in range(0, len(plan.new_variations), batch_size):
        chunk = plan.new_variations[i:i + batch_size]
        client.upsert_objects(chunk)
        stats["variations_created"] += len(chunk)
        log(f"added variations to existing items: {min(i + batch_size, len(plan.new_variations))}"
            f"/{len(plan.new_variations)}")
    return stats
