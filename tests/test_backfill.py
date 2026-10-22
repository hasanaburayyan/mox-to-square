"""Image backfill: printings recovered from Square SKUs, images added to what has none."""

from __future__ import annotations

from mtgsync import square_images
from mtgsync.backfill import (ALL_CATEGORIES, ParsedSku, category_names, parse_sku, records_from_catalog,
                              run_backfill)
from mtgsync.naming import record_sku


def test_parse_sku():
    assert parse_sku("sta-42-nf-nm") == ParsedSku("sta", "42", "nonfoil", "NM", "en")
    assert parse_sku("STA-42-F-LP-JA") == ParsedSku("sta", "42", "foil", "LP", "ja")
    assert parse_sku("pdom-207star-f-nm") == ParsedSku("pdom", "207star", "foil", "NM", "en")
    assert parse_sku("sld-63a-e-dmg-zhs") == ParsedSku("sld", "63a", "etched", "DMG", "zhs")
    for bad in ("", "sta-42", "sta-42-x-nm", "sta-42-nf-ok", "sta 42 nf nm", "sta-42-nf-nm-de-extra", "ABC123"):
        assert parse_sku(bad) is None, bad


def _item(iid, name, skus, category=None, image_ids=None):
    data = {"name": name, "variations": [
        {"type": "ITEM_VARIATION", "id": f"{iid}-{i}", "version": 1, "item_variation_data": {"item_id": iid, "sku": s}}
        for i, s in enumerate(skus)]}
    if category:
        data["categories"] = [{"id": category}]
    if image_ids:
        data["image_ids"] = image_ids
    return {"type": "ITEM", "id": iid, "item_data": data}


def test_records_from_catalog_round_trip_and_skips(scryfall_index):
    items = [
        _item("I1", "Lightning Bolt", ["sta-42-nf-nm", "sta-42-f-lp", "sta-42-nf-nm-ja", "sta-42-nf-nm-de"]),
        _item("I2", "Teferi, Hero of Dominaria", ["pdom-207star-f-nm"]),       # ★ in the collector number
        _item("I3", "Mystery", ["zzz-1-nf-nm", "not-a-sku", ""]),
        _item("I4", "Shirt", ["TSHIRT-RED"]),
    ]
    records, skipped = records_from_catalog(items, scryfall_index)
    by_sku = {record_sku(r, include_language=True): r for r in records}
    assert set(by_sku) == {"sta-42-nf-nm", "sta-42-f-lp", "sta-42-nf-nm-ja", "sta-42-nf-nm-de", "pdom-207star-f-nm"}
    assert by_sku["sta-42-nf-nm"].scryfall_id.endswith("0001") and by_sku["sta-42-nf-nm"].lang == "en"
    assert by_sku["sta-42-nf-nm-ja"].scryfall_id.endswith("0002")          # own Japanese object
    assert by_sku["sta-42-nf-nm-de"].scryfall_id.endswith("0001")          # German: English object, flagged
    assert by_sku["sta-42-nf-nm-de"].lang_fallback and by_sku["sta-42-nf-nm-de"].lang == "de"
    assert by_sku["sta-42-f-lp"].condition == "LP" and by_sku["sta-42-f-lp"].finish == "foil"
    assert by_sku["pdom-207star-f-nm"].collector_number == "207★"
    assert all(r.tier == "tracked" and r.count == 1 and r.image_uri for r in records)
    assert len(skipped) == 4 and any("zzz-1-nf-nm" in s for s in skipped) and any("TSHIRT-RED" in s for s in skipped)


def test_records_from_catalog_limits_to_category(scryfall_index):
    items = [_item("I1", "Lightning Bolt", ["sta-42-nf-nm"], category="CAT"),
             _item("I2", "Opt", ["eld-59-nf-nm"], category="OTHER")]
    records, _ = records_from_catalog(items, scryfall_index, category_id="CAT")
    assert [r.name for r in records] == ["Lightning Bolt"]
    records, _ = records_from_catalog(items, scryfall_index, category_id=None)
    assert sorted(r.name for r in records) == ["Lightning Bolt", "Opt"]


class _FakeClient:
    def __init__(self, items, categories=()):
        self.items, self.categories = items, list(categories)
        self.uploads: list[tuple[str, str | None]] = []
        self.upserts: list[dict] = []

    def list_items(self, types="ITEM"):
        return self.categories if types == "CATEGORY" else self.items

    def list_categories(self):
        return self.categories

    def create_image(self, path, caption, *, object_id=None, is_primary=False):
        self.uploads.append((caption, object_id))
        return f"IMG{len(self.uploads)}"

    def create_item_image(self, item_id, path, caption):
        return self.create_image(path, caption, object_id=item_id, is_primary=True)

    def upsert_objects(self, objects):
        self.upserts.extend(objects)
        for o in objects:
            for it in self.items:
                for v in it["item_data"]["variations"]:
                    if v["id"] == o["id"]:
                        v["item_variation_data"] = dict(o["item_variation_data"])
        return {}


def test_run_backfill_adds_images_then_is_a_no_op(settings, monkeypatch):
    monkeypatch.setattr(square_images, "download_image",
                        lambda uri, dest, ua, session=None: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                              dest.write_bytes(b"jpg"), dest)[2])
    client = _FakeClient([
        _item("I1", "Lightning Bolt", ["sta-42-nf-nm", "sta-42-f-nm", "sta-42-nf-nm-ja"], category="CAT"),
        _item("I2", "Opt", ["eld-59-nf-nm"], category="CAT", image_ids=["HAS"]),     # item image already there
        _item("I3", "Shirt", ["TSHIRT-RED"], category="OTHER"),
    ], categories=[{"id": "CAT", "category_data": {"name": "MTG Singles"}}])
    logs: list[str] = []
    report = run_backfill(settings, client, offline=True, log=logs.append)
    assert report.ok and not report.dry_run
    assert (report.items_seen, report.variations_seen, report.variations_matched) == (3, 5, 4)
    assert report.skipped == []                                     # the shirt is outside the category
    # Item pass: Bolt gets a primary image; Opt already had one.
    assert report.item_images.uploaded == 1 and report.item_images.already_done == 1
    assert ("Lightning Bolt", "I1") in client.uploads
    # Variation pass: the English sta-42 scan is reused from the item image; Japanese and Opt's
    # printing are uploaded unattached; every matched variation ends up with an image.
    assert report.variation_images.reused == 1 and report.variation_images.uploaded == 2
    assert report.variation_images.attached == 4
    imgs = {v["item_variation_data"]["sku"]: v["item_variation_data"].get("image_ids")
            for it in client.items for v in it["item_data"]["variations"]}
    assert imgs["sta-42-nf-nm"] == imgs["sta-42-f-nm"] == ["IMG1"]
    assert imgs["sta-42-nf-nm-ja"] and imgs["sta-42-nf-nm-ja"] != ["IMG1"]
    assert imgs["eld-59-nf-nm"] and imgs["TSHIRT-RED"] is None
    assert any("limiting to category 'MTG Singles'" in l for l in logs)

    # Running again changes nothing.
    before = (len(client.uploads), len(client.upserts))
    report = run_backfill(settings, client, offline=True, log=logs.append)
    assert report.ok and report.item_images.uploaded == 0 and report.variation_images.attached == 0
    assert (len(client.uploads), len(client.upserts)) == before


def test_run_backfill_dry_run_changes_nothing(settings):
    client = _FakeClient([_item("I1", "Lightning Bolt", ["sta-42-nf-nm"])])
    logs: list[str] = []
    report = run_backfill(settings, client, category=ALL_CATEGORIES, dry_run=True, offline=True, log=logs.append)
    assert report.ok and report.dry_run and report.variations_matched == 1
    assert client.uploads == [] and client.upserts == []
    assert any("[dry-run] Lightning Bolt" in l for l in logs)
    assert any("every item in the library" in l for l in logs)


def test_run_backfill_category_choice(settings):
    client = _FakeClient([_item("I1", "Lightning Bolt", ["sta-42-nf-nm"], category="A"),
                          _item("I2", "Opt", ["eld-59-nf-nm"], category="B")],
                         categories=[{"id": "A", "category_data": {"name": "MTG Singles"}},
                                     {"id": "B", "category_data": {"name": "Sealed"}}])
    assert category_names(client) == ["MTG Singles", "Sealed"]
    # Explicit category, case-insensitive.
    report = run_backfill(settings, client, category="sealed", dry_run=True, offline=True, log=lambda m: None)
    assert report.ok and report.variations_matched == 1
    # Default from settings (square.category: MTG Singles).
    report = run_backfill(settings, client, dry_run=True, offline=True, log=lambda m: None)
    assert report.ok and report.variations_matched == 1
    # Unknown category stops before touching anything and names the options.
    report = run_backfill(settings, client, category="Nope", dry_run=True, offline=True, log=lambda m: None)
    assert not report.ok and "MTG Singles, Sealed" in report.errors[0] and report.variations_matched == 0
