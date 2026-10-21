from pathlib import Path

from mtgsync.records import Record
from mtgsync.square_images import choose_images, item_ids_by_sku, run_images


def _rec(name, set_code, num, finish, price, count=1, sid=None, released=None, fallback=False):
    return Record(moxfield_line=1, count=count, tradelist_count=0, condition="NM", finish=finish,
                  name=name, set=set_code, collector_number=num, rarity="rare", tier="tracked",
                  sell_price=price, scryfall_id=sid or f"{set_code}-{num}", released_at=released,
                  lang_fallback=fallback, image_uri=f"https://img/{set_code}/{num}.jpg")


def test_choose_highest_value_then_oldest_printing():
    recs = [_rec("Bolt", "sta", "42", "nonfoil", 2.5, released="2021-04-23"),
            _rec("Bolt", "sta", "42", "foil", 5.25, released="2021-04-23"),
            _rec("Bolt", "clb", "1", "foil", 5.25, released="2022-06-10"),
            _rec("Zero", "x", "1", "nonfoil", 1.0, count=0)]
    plans = choose_images(recs)
    assert [p.item_name for p in plans] == ["Bolt"]       # zero-count item has no image
    assert plans[0].image_uri == "https://img/sta/42.jpg"  # tie on price -> older printing
    assert plans[0].all_skus == ["clb-1-f-nm", "sta-42-f-nm", "sta-42-nf-nm"]


def test_choose_without_prices_prefers_oldest_english_printing():
    recs = [_rec("Copy Artifact", "3ed", "53", "nonfoil", None, released="1994-04-01"),
            _rec("Copy Artifact", "lea", "53", "nonfoil", None, released="1993-08-05"),
            _rec("Copy Artifact", "2ed", "54", "nonfoil", None, released="1993-12-01"),
            _rec("Moat", "leg", "28", "nonfoil", None, released="1994-06-01", fallback=True),
            _rec("Moat", "leg", "28", "foil", None, released="1994-06-01")]
    plans = {p.item_name: p for p in choose_images(recs)}
    assert plans["Copy Artifact"].image_uri == "https://img/lea/53.jpg"
    assert plans["Moat"].sku == "leg-28-f-nm"            # English object over the fallback one


def test_item_ids_by_sku():
    items = [{"id": "ITEM1", "item_data": {"variations": [
        {"item_variation_data": {"sku": "a-1-nf-nm"}}, {"item_variation_data": {"sku": "a-1-f-nm"}}]}}]
    assert item_ids_by_sku(items) == {"a-1-nf-nm": "ITEM1", "a-1-f-nm": "ITEM1"}


class _FakeClient:
    def __init__(self, items):
        self._items = items
        self.uploads: list[tuple[str, str]] = []

    def list_items(self):
        return self._items

    def create_item_image(self, item_id, image_path, caption):
        self.uploads.append((item_id, caption))
        return "IMG_" + item_id


def test_run_images_prunes_stale_state_and_uploads(tmp_path: Path, monkeypatch):
    from mtgsync import square_images
    from mtgsync.square_images import load_images_state, save_images_state

    state_dir = tmp_path / "s"
    save_images_state(state_dir / "images_uploaded.json",
                      {"OLD_ITEM": {"item_name": "Bolt", "image_id": "x", "scryfall_id": "sta-42", "sku": "sta-42-nf-nm"}})
    client = _FakeClient([{"id": "NEW_ITEM", "item_data": {"variations": [
        {"item_variation_data": {"sku": "sta-42-nf-nm"}}]}}])
    monkeypatch.setattr(square_images, "download_image",
                        lambda uri, dest, ua, session=None: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                              dest.write_bytes(b"jpg"), dest)[2])
    report = square_images.run_images([_rec("Bolt", "sta", "42", "nonfoil", None)], cache_dir=tmp_path / "c",
                                      state_dir=state_dir, user_agent="t", include_language=False,
                                      dry_run=False, client=client, log=lambda m: None)
    assert report.stale_dropped == 1 and report.uploaded == 1 and report.errors == []
    assert client.uploads == [("NEW_ITEM", "Bolt")]
    state = load_images_state(state_dir / "images_uploaded.json")
    assert set(state) == {"NEW_ITEM"} and state["NEW_ITEM"]["image_id"] == "IMG_NEW_ITEM"


def test_run_images_skips_items_that_already_have_an_image_in_square(tmp_path: Path):
    client = _FakeClient([{"id": "ITEM_DONE", "item_data": {"image_ids": ["IMG1"], "variations": [
        {"item_variation_data": {"sku": "sta-42-nf-nm"}}]}}])
    report = run_images([_rec("Bolt", "sta", "42", "nonfoil", None)], cache_dir=tmp_path / "c",
                        state_dir=tmp_path / "s", user_agent="t", include_language=False,
                        dry_run=False, client=client, log=lambda m: None)
    assert report.already_done == 1 and report.uploaded == 0 and client.uploads == []


def test_run_images_dry_run_without_client(tmp_path: Path):
    logs: list[str] = []
    report = run_images([_rec("Bolt", "sta", "42", "nonfoil", 2.5)], cache_dir=tmp_path / "c",
                        state_dir=tmp_path / "s", user_agent="t", include_language=False,
                        dry_run=True, client=None, log=logs.append)
    assert report.planned == 1 and report.uploaded == 0
    assert any("[dry-run] Bolt" in l for l in logs)
