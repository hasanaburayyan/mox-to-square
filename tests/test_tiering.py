from pathlib import Path

from mtgsync.records import Record
from mtgsync.tiering import (assign_tiers, bucket_for, load_buckets, load_tracked_state,
                             save_tracked_state)

BUCKETS = load_buckets([
    {"name": "Bulk C/U", "rarities": ["common", "uncommon"], "finish": "nonfoil", "price": 0.25},
    {"name": "Bulk C/U", "rarities": ["common", "uncommon"], "finish": "foil", "price": 0.50},
    {"name": "Bulk R/M", "rarities": ["rare", "mythic"], "finish": "nonfoil", "price": 1.00},
])


def _rec(set_code: str, num: str, rarity: str, finish: str, base: float | None) -> Record:
    return Record(moxfield_line=1, count=1, tradelist_count=0, condition="NM", finish=finish,
                  name="X", set=set_code, collector_number=num, rarity=rarity, base_price=base)


def test_threshold_zero_tracks_everything_even_null_prices():
    recs = [_rec("eld", "59", "common", "nonfoil", 0.08), _rec("4ed", "246", "common", "nonfoil", None)]
    tracked = assign_tiers(recs, 0, BUCKETS, set())
    assert all(r.tier == "tracked" for r in recs)
    assert tracked == {"eld-59", "4ed-246"}


def test_threshold_boundary():
    at = _rec("a", "1", "rare", "nonfoil", 1.00)
    below = _rec("b", "1", "rare", "nonfoil", 0.99)
    assign_tiers([at, below], 1.00, BUCKETS, set())
    assert at.tier == "tracked"
    assert below.tier == "bulk" and below.bulk_bucket == "Bulk R/M"


def test_any_finish_over_threshold_tracks_the_printing():
    nf = _rec("eld", "59", "common", "nonfoil", 0.08)
    foil = _rec("eld", "59", "common", "foil", 1.50)
    assign_tiers([nf, foil], 1.00, BUCKETS, set())
    assert nf.tier == "tracked" and foil.tier == "tracked"


def test_previously_tracked_stays_tracked_after_price_dip(tmp_path: Path):
    state = tmp_path / "tracked.json"
    save_tracked_state(state, {"eld-59"})
    rec = _rec("eld", "59", "common", "nonfoil", 0.05)
    tracked = assign_tiers([rec], 1.00, BUCKETS, load_tracked_state(state))
    assert rec.tier == "tracked" and "eld-59" in tracked


def test_etched_falls_into_foil_bucket_and_mythic_foil_has_no_bucket():
    assert bucket_for(BUCKETS, "common", "etched").name == "Bulk C/U"
    assert bucket_for(BUCKETS, "common", "etched").finish == "foil"
    assert bucket_for(BUCKETS, "mythic", "foil") is None
