from mtgsync.naming import make_sku, sku_collector_number, variation_name, description
from mtgsync.records import Record


def _rec(**kw) -> Record:
    base = dict(moxfield_line=1, count=1, tradelist_count=0, condition="NM", finish="nonfoil",
                name="Lightning Bolt", set="sta", set_name="Strixhaven Mystical Archive",
                collector_number="42", rarity="rare", lang="en", type_line="Instant")
    base.update(kw)
    return Record(**base)


def test_sku_basic():
    assert make_sku("M21", "159", "foil", "NM") == "m21-159-f-nm"


def test_sku_etched_and_letters():
    assert make_sku("sta", "42", "etched", "LP") == "sta-42-e-lp"
    assert make_sku("sld", "63a", "foil", "NM") == "sld-63a-f-nm"


def test_sku_star_collector_number():
    assert sku_collector_number("207★") == "207star"
    assert make_sku("pdom", "207★", "foil", "NM") == "pdom-207star-f-nm"


def test_sku_language_suffix_only_when_enabled_and_non_english():
    assert make_sku("sta", "42", "nonfoil", "NM", "ja") == "sta-42-nf-nm"
    assert make_sku("sta", "42", "nonfoil", "NM", "ja", include_language=True) == "sta-42-nf-nm-ja"
    assert make_sku("sta", "42", "nonfoil", "NM", "en", include_language=True) == "sta-42-nf-nm"


def test_variation_name_and_description():
    rec = _rec(finish="foil")
    assert variation_name(rec) == "Strixhaven Mystical Archive #42 · NM · Foil"
    assert description(rec) == "Instant"          # no set name: must match across the item


def test_description_identical_across_printings_of_one_card():
    a = _rec(set="lea", set_name="Limited Edition Alpha", type_line="Artifact")
    b = _rec(set="3ed", set_name="Revised Edition", type_line="Artifact")
    assert description(a) == description(b)


def test_double_faced_name_kept_verbatim():
    rec = _rec(name="Fable of the Mirror-Breaker // Reflection of Kiki-Jiki")
    assert "//" in rec.name
