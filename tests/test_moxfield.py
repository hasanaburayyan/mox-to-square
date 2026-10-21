from pathlib import Path

import pytest

from mtgsync.moxfield import (MoxfieldHeaderError, REQUIRED_HEADERS, parse_row, read_moxfield_csv,
                              validate_headers)


def test_fixture_matches_header_contract(moxfield_csv: Path):
    rows, missing_optional, errors = read_moxfield_csv(moxfield_csv)
    assert missing_optional == []
    assert errors == []
    assert len(rows) == 19


def test_missing_required_header_fails_loudly():
    headers = [h for h in REQUIRED_HEADERS if h != "Collector Number"]
    with pytest.raises(MoxfieldHeaderError, match="Collector Number"):
        validate_headers(headers)


def test_row_normalisation(moxfield_csv: Path):
    rows, _, _ = read_moxfield_csv(moxfield_csv)
    by_line = {r.line_no: r for r in rows}
    bolt = by_line[2]
    assert (bolt.edition, bolt.collector_number, bolt.condition, bolt.finish, bolt.language) == \
        ("sta", "42", "NM", "nonfoil", "en")
    assert bolt.purchase_price == 2.0
    etched = by_line[4]
    assert etched.finish == "etched" and etched.condition == "LP"
    ja = by_line[6]
    assert ja.language == "ja"
    star = by_line[8]
    assert star.collector_number == "207★"
    proxy = by_line[18]
    assert proxy.proxy is True
    assert by_line[10].tradelist_count == 4


def test_unknown_condition_is_a_row_error():
    raw = {"Count": "1", "Name": "X", "Edition": "abc", "Collector Number": "1",
           "Condition": "Pristine", "Language": "English", "Foil": ""}
    with pytest.raises(ValueError, match="Condition"):
        parse_row(raw, 5)
