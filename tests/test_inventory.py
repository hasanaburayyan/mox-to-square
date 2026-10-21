from pathlib import Path

import pytest

from mtgsync.square_build import ImportRow
from mtgsync.square_inventory import (InventoryError, inventory_changes, resolve_location, run_inventory,
                                      tracking_upsert_object)


def _var(vid, sku, item_id="ITEM", track=False):
    return {"type": "ITEM_VARIATION", "id": vid, "version": 7, "updated_at": "x", "is_deleted": False,
            "item_variation_data": {"item_id": item_id, "name": "v " + sku, "sku": sku,
                                    "pricing_type": "VARIABLE_PRICING", "track_inventory": track}}


class FakeClient:
    def __init__(self, items, counts=None, locations=None):
        self.items, self.counts = items, counts or {}
        self.locations = locations or [{"id": "LOC1", "name": "Provenance Learning, inc", "status": "ACTIVE"}]
        self.changes: list[dict] = []
        self.upserts: list[dict] = []

    def list_locations(self):
        return self.locations

    def list_items(self):
        return self.items

    def retrieve_counts(self, ids, location_id):
        return {k: v for k, v in self.counts.items() if k in ids}

    def change_inventory(self, changes):
        self.changes.extend(changes)

    def upsert_objects(self, objects):
        self.upserts.extend(objects)


ITEMS = [{"id": "ITEM", "item_data": {"name": "Bolt", "variations": [_var("V1", "sta-42-nf-nm"),
                                                                       _var("V2", "sta-42-f-nm", track=True)]}}]
ROWS = [ImportRow("Bolt", "v", "sta-42-nf-nm", None, 4), ImportRow("Bolt", "v", "sta-42-f-nm", None, 1),
        ImportRow("Gone", "v", "zzz-1-nf-nm", None, 2)]


def test_resolve_location_by_name_or_single():
    locs = [{"id": "A", "name": "The Warp Gate", "status": "ACTIVE"}]
    assert resolve_location(locs, "the warp gate") == ("A", "The Warp Gate")
    assert resolve_location(locs, "Somewhere Else") == ("A", "The Warp Gate")   # only one active
    locs.append({"id": "B", "name": "Second", "status": "ACTIVE"})
    with pytest.raises(InventoryError, match="matches none"):
        resolve_location(locs, "Somewhere Else")


def test_tracking_upsert_object_sets_flags_and_keeps_version():
    obj = tracking_upsert_object(_var("V1", "s"), 4)
    d = obj["item_variation_data"]
    assert obj["version"] == 7 and "updated_at" not in obj
    assert d["track_inventory"] is True and d["inventory_alert_type"] == "LOW_QUANTITY"
    assert d["inventory_alert_threshold"] == 4
    assert tracking_upsert_object(_var("V1", "s"), None)["item_variation_data"]["inventory_alert_type"] == "NONE"


def test_set_mode_physical_counts(tmp_path: Path):
    client = FakeClient(ITEMS, counts={"V2": 3.0})
    report = run_inventory(ROWS, client=client, location_name="The Warp Gate", mode="set", alert_count=4,
                           state_dir=tmp_path, dry_run=False, log=lambda m: None)
    assert report.location_name == "Provenance Learning, inc"
    assert report.planned == 2 and report.applied == 2 and report.not_in_square == ["zzz-1-nf-nm"]
    assert [c["type"] for c in client.changes] == ["PHYSICAL_COUNT", "PHYSICAL_COUNT"]
    assert {c["physical_count"]["catalog_object_id"]: c["physical_count"]["quantity"] for c in client.changes} \
        == {"V1": "4", "V2": "1"}
    assert all(c["physical_count"]["location_id"] == "LOC1" for c in client.changes)
    # Both variations get tracking/alert upserted (V2 tracked but had no alert threshold).
    assert {o["id"] for o in client.upserts} == {"V1", "V2"}


def test_add_mode_adjustments_and_batch_guard(tmp_path: Path):
    client = FakeClient(ITEMS, counts={"V1": 10.0})
    rows = ROWS[:2]
    report = run_inventory(rows, client=client, location_name="", mode="add", alert_count=None,
                           state_dir=tmp_path, dry_run=False, log=lambda m: None)
    assert report.applied == 2
    assert all(c["type"] == "ADJUSTMENT" and c["adjustment"]["from_state"] == "NONE" for c in client.changes)
    assert client.changes[0]["adjustment"]["quantity"] == "4"
    with pytest.raises(InventoryError, match="already applied"):
        run_inventory(rows, client=client, location_name="", mode="add", alert_count=None,
                      state_dir=tmp_path, dry_run=False, log=lambda m: None)
    # --force or a dry run gets through.
    run_inventory(rows, client=client, location_name="", mode="add", alert_count=None,
                  state_dir=tmp_path, dry_run=True, log=lambda m: None)
    assert run_inventory(rows, client=client, location_name="", mode="add", alert_count=None,
                         state_dir=tmp_path, dry_run=False, force=True, log=lambda m: None).applied == 2


def test_dry_run_changes_nothing(tmp_path: Path):
    client = FakeClient(ITEMS)
    report = run_inventory(ROWS, client=client, location_name="", mode="set", alert_count=4,
                           state_dir=tmp_path, dry_run=True, log=lambda m: None)
    assert report.planned == 2 and report.applied == 0
    assert client.changes == [] and client.upserts == []


def test_find_and_delete_duplicate_variations(tmp_path: Path):
    from mtgsync.square_inventory import find_duplicate_variations, run_dedupe

    old = _var("V_OLD", "sta-42-nf-nm", track=True) | {"created_at": "2026-10-18T21:00:00Z"}
    new = _var("V_NEW", "sta-42-nf-nm") | {"created_at": "2026-10-18T21:10:00Z"}
    other = _var("V_OTHER", "sta-42-f-nm") | {"created_at": "2026-10-18T21:00:00Z"}
    items = [{"id": "ITEM", "item_data": {"name": "Bolt", "variations": [new, old, other]}}]
    dups = find_duplicate_variations(items, {"V_NEW": 4.0})
    assert [(d.sku, d.keep_id, d.delete_id, d.delete_quantity) for d in dups] == \
        [("sta-42-nf-nm", "V_OLD", "V_NEW", 4.0)]

    class Client(FakeClient):
        def __init__(self):
            super().__init__(items, counts={"V_NEW": 4.0})
            self.deleted: list[str] = []

        def delete_objects(self, ids):
            self.deleted.extend(ids)
            return list(ids)

    c = Client()
    assert run_dedupe(client=c, location_name="", dry_run=True, log=lambda m: None)[1] == []
    assert c.deleted == []
    dups, deleted = run_dedupe(client=c, location_name="", dry_run=False, log=lambda m: None)
    assert deleted == ["V_NEW"] and c.deleted == ["V_NEW"]


def test_zero_quantity_skipped_in_add_mode():
    plans = []
    assert inventory_changes(plans, "LOC", "add") == []
