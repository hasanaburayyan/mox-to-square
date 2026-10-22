"""Per-variation images: one upload per printing, shared by every variation of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtgsync import square_images
from mtgsync.records import Record
from mtgsync.square_images import (IMAGES_STATE_FILENAME, VARIATION_IMAGES_STATE_FILENAME,
                                   choose_variation_images, load_images_state, run_variation_images,
                                   save_images_state, variation_with_image, variations_by_sku)


def _rec(name, set_code, num, finish, condition="NM", count=1, tier="tracked", sid=None, image=True,
         lang="en", fallback=False):
    return Record(moxfield_line=1, count=count, tradelist_count=0, condition=condition, finish=finish,
                  name=name, set=set_code, set_name=set_code.upper(), collector_number=num, rarity="rare",
                  tier=tier, lang=lang, lang_fallback=fallback, scryfall_id=sid or f"{set_code}-{num}",
                  image_uri=f"https://img/{set_code}/{num}.jpg" if image else None)


def _var(vid, sku, version=3, image_ids=None, item_id="ITEM1"):
    data = {"item_id": item_id, "sku": sku, "name": sku, "pricing_type": "VARIABLE_PRICING"}
    if image_ids is not None:
        data["image_ids"] = image_ids
    return {"type": "ITEM_VARIATION", "id": vid, "version": version, "updated_at": "2026-10-01T00:00:00Z",
            "present_at_all_locations": True, "item_variation_data": data}


class _FakeClient:
    def __init__(self, items):
        self._items = items
        self.uploads: list[tuple[str, str | None]] = []
        self.upserts: list[list[dict]] = []

    def list_items(self):
        return self._items

    def create_image(self, image_path, caption, *, object_id=None, is_primary=False):
        self.uploads.append((caption, object_id))
        return f"IMG{len(self.uploads)}"

    def upsert_objects(self, objects):
        self.upserts.append(objects)
        return {}


@pytest.fixture
def no_download(monkeypatch):
    monkeypatch.setattr(square_images, "download_image",
                        lambda uri, dest, ua, session=None: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                              dest.write_bytes(b"jpg"), dest)[2])


def test_choose_groups_variations_by_printing():
    recs = [_rec("Bolt", "sta", "42", "nonfoil"),
            _rec("Bolt", "sta", "42", "foil"),
            _rec("Bolt", "sta", "42", "nonfoil", condition="LP"),
            _rec("Bolt", "clb", "1", "foil"),
            _rec("Bolt", "m10", "146", "nonfoil", count=0),            # no stock
            _rec("Opt", "eld", "59", "nonfoil", tier="bulk"),            # bulk tier has no variation
            _rec("Opt", "xln", "65", "nonfoil", image=False)]            # nothing to upload
    plans = choose_variation_images(recs)
    assert [(p.item_name, p.set, p.collector_number) for p in plans] == \
        [("Bolt", "clb", "1"), ("Bolt", "sta", "42")]
    assert plans[1].skus == ["sta-42-f-nm", "sta-42-nf-lp", "sta-42-nf-nm"]
    assert plans[1].caption == "Bolt (STA #42)"


def test_choose_language_fallback_rows_share_the_printing():
    # A German FBB row matched the French object, so it is the same printing as the French row.
    recs = [_rec("Scrubland", "fbb", "286", "nonfoil", sid="fbb-286-fr", lang="fr"),
            _rec("Scrubland", "fbb", "286", "nonfoil", sid="fbb-286-fr", lang="de", fallback=True)]
    plans = choose_variation_images(recs, include_language=True)
    assert len(plans) == 1
    assert plans[0].skus == ["fbb-286-nf-nm-de", "fbb-286-nf-nm-fr"]


def test_merged_language_sku_gets_the_english_scan():
    # sku.include_language off: the English and Japanese copies are one variation, which must
    # be claimed by exactly one printing, the English one.
    recs = [_rec("Bolt", "sta", "42", "nonfoil", sid="sta-42-ja", lang="ja"),
            _rec("Bolt", "sta", "42", "nonfoil", sid="sta-42-en", lang="en"),
            _rec("Bolt", "sta", "42", "foil", sid="sta-42-ja", lang="ja")]
    plans = choose_variation_images(recs, include_language=False)
    assert [(p.printing, p.skus) for p in plans] == [("sta-42-en", ["sta-42-nf-nm"]), ("sta-42-ja", ["sta-42-f-nm"])]
    # With the suffix on, each language is its own variation and keeps its own scan.
    plans = choose_variation_images(recs, include_language=True)
    assert {p.printing: p.skus for p in plans} == {"sta-42-en": ["sta-42-nf-nm"],
                                                   "sta-42-ja": ["sta-42-f-nm-ja", "sta-42-nf-nm-ja"]}


def test_variations_by_sku_and_variation_with_image():
    v = _var("V1", "sta-42-nf-nm", image_ids=["OLD"])
    assert variations_by_sku([{"id": "ITEM1", "item_data": {"variations": [v]}}]) == {"sta-42-nf-nm": v}
    out = variation_with_image(v, "NEW")
    assert out["item_variation_data"]["image_ids"] == ["OLD", "NEW"]
    assert out["version"] == 3 and out["id"] == "V1" and "updated_at" not in out
    assert out["item_variation_data"]["sku"] == "sta-42-nf-nm"         # every other field kept
    assert v["item_variation_data"]["image_ids"] == ["OLD"]            # input not mutated
    assert variation_with_image(out, "NEW")["item_variation_data"]["image_ids"] == ["OLD", "NEW"]


def test_one_upload_per_printing_shared_by_its_variations(tmp_path: Path, no_download):
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": [
        _var("V1", "sta-42-nf-nm"), _var("V2", "sta-42-f-nm"), _var("V3", "clb-1-f-nm")]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "sta", "42", "foil"),
            _rec("Bolt", "clb", "1", "foil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None)
    assert report.errors == []
    assert (report.planned, report.uploaded, report.reused, report.attached) == (2, 2, 0, 3)
    assert client.uploads == [("Bolt (CLB #1)", None), ("Bolt (STA #42)", None)]   # unattached uploads
    objects = [o for batch in client.upserts for o in batch]
    assert {o["id"]: o["item_variation_data"]["image_ids"] for o in objects} == \
        {"V3": ["IMG1"], "V1": ["IMG2"], "V2": ["IMG2"]}
    assert all(o["version"] == 3 and o["type"] == "ITEM_VARIATION" for o in objects)
    state = load_images_state(tmp_path / "s" / VARIATION_IMAGES_STATE_FILENAME)
    assert state["sta-42"]["image_id"] == "IMG2" and state["sta-42"]["skus"] == ["sta-42-f-nm", "sta-42-nf-nm"]


def test_reuses_item_stage_image_and_sibling_image(tmp_path: Path, no_download):
    state_dir = tmp_path / "s"
    # The item stage already uploaded the sta-42 scan as the item's picture.
    save_images_state(state_dir / IMAGES_STATE_FILENAME,
                      {"ITEM1": {"item_name": "Bolt", "image_id": "ITEMIMG", "scryfall_id": "sta-42", "sku": "x"}})
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": [
        _var("V1", "sta-42-nf-nm"), _var("V2", "sta-42-f-nm"),
        # clb-1: one variation already carries a scan, the other does not.
        _var("V3", "clb-1-f-nm", image_ids=["SIB"]), _var("V4", "clb-1-nf-nm")]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "sta", "42", "foil"),
            _rec("Bolt", "clb", "1", "foil"), _rec("Bolt", "clb", "1", "nonfoil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=state_dir, user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None)
    assert report.errors == [] and client.uploads == []
    assert (report.uploaded, report.reused, report.attached) == (0, 2, 3)
    objects = {o["id"]: o["item_variation_data"]["image_ids"] for b in client.upserts for o in b}
    assert objects == {"V4": ["SIB"], "V1": ["ITEMIMG"], "V2": ["ITEMIMG"]}


def test_second_run_is_a_no_op(tmp_path: Path, no_download):
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": [
        _var("V1", "sta-42-nf-nm", image_ids=["IMG1"]), _var("V2", "sta-42-f-nm", image_ids=["IMG1"])]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "sta", "42", "foil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None)
    assert (report.already_done, report.uploaded, report.attached) == (1, 0, 0)
    assert client.uploads == [] and client.upserts == []


def test_missing_skus_are_reported_and_printing_skipped(tmp_path: Path, no_download):
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": [_var("V1", "sta-42-nf-nm")]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "sta", "42", "foil"),
            _rec("Opt", "eld", "59", "nonfoil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None)
    assert report.not_in_square == ["sta-42-f-nm", "eld-59-nf-nm"]      # plan order: Bolt, then Opt
    assert report.uploaded == 1 and report.attached == 1          # sta-42 still done for the SKU present


def test_dry_run_changes_nothing(tmp_path: Path):
    logs: list[str] = []
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": [
        _var("V1", "sta-42-nf-nm"), _var("V2", "sta-42-f-nm")]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "sta", "42", "foil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=True, client=client, log=logs.append)
    assert report.planned == 1 and report.uploaded == 0 and report.attached == 0
    assert client.uploads == [] and client.upserts == []
    assert any("[dry-run] Bolt (STA #42): upload https://img/sta/42.jpg -> 2 variation(s)" in l for l in logs)
    assert not (tmp_path / "s" / VARIATION_IMAGES_STATE_FILENAME).exists()

    # Without a token the dry run still lists the plan.
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=True, client=None, log=logs.append)
    assert report.planned == 1


def test_upload_failure_is_reported_and_others_continue(tmp_path: Path, no_download):
    class _Flaky(_FakeClient):
        def create_image(self, image_path, caption, *, object_id=None, is_primary=False):
            if "CLB" in caption:
                raise RuntimeError("boom")
            return super().create_image(image_path, caption, object_id=object_id, is_primary=is_primary)

    client = _Flaky([{"id": "ITEM1", "item_data": {"variations": [
        _var("V1", "sta-42-nf-nm"), _var("V3", "clb-1-f-nm")]}}])
    recs = [_rec("Bolt", "sta", "42", "nonfoil"), _rec("Bolt", "clb", "1", "foil")]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None)
    assert len(report.errors) == 1 and "boom" in report.errors[0]
    assert report.uploaded == 1 and report.attached == 1


def test_upserts_are_batched(tmp_path: Path, no_download):
    variations = [_var(f"V{i}", f"s{i}-1-nf-nm") for i in range(5)]
    client = _FakeClient([{"id": "ITEM1", "item_data": {"variations": variations}}])
    recs = [_rec("Card", f"s{i}", "1", "nonfoil") for i in range(5)]
    report = run_variation_images(recs, cache_dir=tmp_path / "c", state_dir=tmp_path / "s", user_agent="t",
                                  include_language=False, dry_run=False, client=client, log=lambda m: None,
                                  batch_size=2)
    assert report.attached == 5 and [len(b) for b in client.upserts] == [2, 2, 1]
