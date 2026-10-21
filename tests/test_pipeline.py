"""The one-click upload against a fake Square, fully offline."""

from pathlib import Path

from mtgsync.config import load_settings
from mtgsync.pipeline import upload


class FakeSquare:
    env = "fake"

    def __init__(self):
        self.items: list[dict] = []
        self.categories: list[dict] = []
        self.changes: list[dict] = []
        self.upserts: list[dict] = []
        self.images: list[str] = []
        self._n = 0

    def _id(self):
        self._n += 1
        return f"ID{self._n}"

    def list_locations(self):
        return [{"id": "LOC", "name": "Provenance Learning, inc", "status": "ACTIVE"}]

    def list_items(self, types="ITEM"):
        return self.categories if types == "CATEGORY" else self.items

    def list_categories(self):
        return self.categories

    def retrieve_counts(self, ids, loc):
        return {}

    def upsert_objects(self, objects):
        mappings = []
        for o in objects:
            if o["type"] == "CATEGORY":
                cid = self._id(); mappings.append({"client_object_id": o["id"], "object_id": cid})
                self.categories.append({"id": cid, "category_data": o["category_data"]})
            elif o["type"] == "ITEM":
                iid = self._id()
                vs = []
                for v in o["item_data"]["variations"]:
                    vs.append({"id": self._id(), "created_at": "t", "item_variation_data": dict(v["item_variation_data"])})
                self.items.append({"id": iid, "item_data": {"name": o["item_data"]["name"], "variations": vs}})
            elif o["type"] == "ITEM_VARIATION":
                item = next(i for i in self.items if i["id"] == o["item_variation_data"]["item_id"])
                item["item_data"]["variations"].append({"id": self._id(), "created_at": "t",
                                                        "item_variation_data": dict(o["item_variation_data"])})
            else:  # tracking upsert of an existing variation
                pass
        self.upserts.extend(objects)
        return {"id_mappings": mappings}

    def change_inventory(self, changes):
        self.changes.extend(changes)

    def create_item_image(self, item_id, path, caption):
        self.images.append(item_id)
        return "IMG"


def test_upload_twice_is_refused_and_second_batch_extends(project_root: Path, moxfield_csv: Path, monkeypatch):
    from mtgsync import square_images
    monkeypatch.setattr(square_images, "download_image",
                        lambda uri, dest, ua, session=None: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                              dest.write_bytes(b"jpg"), dest)[2])
    s = load_settings(root=project_root)
    sq = FakeSquare()
    logs: list[str] = []

    r = upload(moxfield_csv, s, dry_run=True, offline=True, client=sq, log=logs.append)
    assert r.ok and r.dry_run and sq.items == [] and sq.changes == []

    r = upload(moxfield_csv, s, offline=True, client=sq, log=logs.append)
    assert r.ok, r.errors
    assert r.items_created == len(sq.items) == r.items_total
    assert r.variations_created == r.variations_total == 16
    assert sum(int(c["adjustment"]["quantity"]) for c in sq.changes) == r.quantity_total
    assert all(c["type"] == "ADJUSTMENT" for c in sq.changes)
    assert len(sq.images) == r.images_uploaded == r.items_total
    assert sq.categories[0]["category_data"]["name"] == "MTG Singles"
    assert (project_root / "output" / "upload_report.md").exists()

    # The same file again is refused before touching Square.
    before = (len(sq.changes), len(sq.upserts))
    r2 = upload(moxfield_csv, s, offline=True, client=sq, log=logs.append)
    assert not r2.ok and "already uploaded" in r2.errors[0]
    assert (len(sq.changes), len(sq.upserts)) == before

    # A new intake file with one existing SKU and one new card extends, never duplicates.
    new_csv = project_root / "intake2.csv"
    hdr, bolt = moxfield_csv.read_text(encoding="utf-8").splitlines()[:2]
    new_csv.write_text(hdr + "\n" + bolt + "\n"
                       + '"1","0","Black Lotus","lea","Near Mint","English","","","2026-10-18","232","FALSE","FALSE",""\n',
                       encoding="utf-8")
    r3 = upload(new_csv, s, offline=True, client=sq, log=logs.append)
    assert r3.ok, r3.errors
    assert r3.items_created == 1 and r3.variations_created == 1 and r3.items_extended == 0
    bolt_item = next(i for i in sq.items if i["item_data"]["name"] == "Lightning Bolt")
    assert sum(1 for v in bolt_item["item_data"]["variations"]
               if v["item_variation_data"]["sku"] == "sta-42-nf-nm") == 1
    assert sq.changes[-1]["adjustment"]["quantity"] in ("1", "4")
