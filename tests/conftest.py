from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mtgsync.config import Settings, load_settings
from mtgsync.scryfall import ScryfallIndex, RAW_FILENAME

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def moxfield_csv() -> Path:
    return FIXTURES / "moxfield_sample.csv"


@pytest.fixture
def scryfall_index() -> ScryfallIndex:
    return ScryfallIndex.from_raw_file(FIXTURES / "scryfall_sample.json")


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A throwaway project root with the real settings.yaml and a cached Scryfall fixture."""
    (tmp_path / "config").mkdir()
    shutil.copy(PROJECT_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml")
    (tmp_path / "cache").mkdir()
    shutil.copy(FIXTURES / "scryfall_sample.json", tmp_path / "cache" / RAW_FILENAME)
    return tmp_path


@pytest.fixture
def settings(project_root: Path) -> Settings:
    s = load_settings(root=project_root)
    s.set("pricing.source", "scryfall")      # pricing tests exercise the Scryfall source
    s.set("square.quantity_mode", "set")     # builder tests do not pass a Square export
    return s
