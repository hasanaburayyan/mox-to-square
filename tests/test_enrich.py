"""Stage 1 language matching, including sets that were never printed in English."""

from __future__ import annotations

from mtgsync.config import Settings
from mtgsync.enrich import enrich_rows
from mtgsync.moxfield import parse_row
from mtgsync.scryfall import ScryfallIndex, trim_card


def _card(sid: str, name: str, set_code: str, num: str, lang: str, **extra) -> dict:
    base = {"id": sid, "name": name, "set": set_code, "set_name": extra.pop("set_name", set_code.upper()),
            "collector_number": num, "rarity": "rare", "lang": lang, "type_line": "Land",
            "finishes": ["nonfoil"], "prices": {"usd": "500.00"},
            "image_uris": {"normal": f"https://img/{set_code}/{num}/{lang}.jpg"}, "released_at": "1994-04-01"}
    return trim_card(base | extra)


def _row(line_no: int, name: str, edition: str, num: str, language: str) -> object:
    return parse_row({"Count": "1", "Name": name, "Edition": edition, "Collector Number": num,
                      "Condition": "Near Mint", "Language": language, "Foil": ""}, line_no)


def test_foreign_only_printing_matches_in_every_language(settings: Settings):
    # Scryfall's default_cards has exactly one object for FBB #286, and it is French.
    index = ScryfallIndex([
        _card("fbb-286-fr", "Scrubland", "fbb", "286", "fr", set_name="Foreign Black Border"),
        _card("4ed-286-en", "Scrubland", "4ed", "286", "en", set_name="Fourth Edition"),
    ])
    rows = [
        _row(2, "Scrubland", "fbb", "286", "French"),
        _row(3, "Scrubland", "fbb", "286", "German"),
        _row(4, "Scrubland", "fbb", "286", "Italian"),
    ]
    records, unmatched, stats = enrich_rows(rows, index, settings)

    assert unmatched == []
    assert stats.matched == 3
    assert [r.lang for r in records] == ["fr", "de", "it"]
    # Every row joined to the same printing; the French one is a direct hit, the others fell back.
    assert {r.scryfall_id for r in records} == {"fbb-286-fr"}
    assert [r.lang_fallback for r in records] == [False, True, True]
    assert stats.lang_fallbacks == 2
    assert all(r.set == "fbb" and r.collector_number == "286" for r in records)


def test_english_object_still_preferred_over_other_languages(settings: Settings):
    # When the bulk file has both an English and a Japanese object, a German row uses English.
    index = ScryfallIndex([
        _card("sta-42-ja", "Lightning Bolt", "sta", "42", "ja"),
        _card("sta-42-en", "Lightning Bolt", "sta", "42", "en"),
    ])
    records, unmatched, _ = enrich_rows([_row(2, "Lightning Bolt", "sta", "42", "German")], index, settings)
    assert unmatched == []
    assert records[0].scryfall_id == "sta-42-en"
    assert (records[0].lang, records[0].lang_fallback) == ("de", True)


def test_unknown_printing_is_still_unmatched(settings: Settings):
    index = ScryfallIndex([_card("fbb-286-fr", "Scrubland", "fbb", "286", "fr")])
    records, unmatched, _ = enrich_rows([_row(2, "Scrubland", "fbb", "999", "German")], index, settings)
    assert records == []
    assert len(unmatched) == 1 and unmatched[0].language == "de"


def test_lookup_any_language_and_languages_for():
    index = ScryfallIndex([
        _card("x-1-fr", "Scrubland", "fbb", "286", "fr"),
        _card("x-1-de", "Scrubland", "fbb", "286", "de"),
    ])
    assert index.lookup_any_language("FBB", "286").id == "x-1-fr"
    assert index.lookup_any_language("fbb", "1") is None
    assert index.languages_for("fbb", "286") == ["de", "fr"]
