import os
from pathlib import Path

from mtgsync.env_store import read_env, square_credentials, write_env


def test_write_env_creates_updates_and_preserves(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN", raising=False)
    p = tmp_path / ".env"
    p.write_text("# comment\nOTHER=keep\nSQUARE_ENV=sandbox\n", encoding="utf-8")
    write_env(p, {"SQUARE_ACCESS_TOKEN": "tok123", "SQUARE_ENV": "production"})
    text = p.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# comment" and "OTHER=keep" in text
    assert text.count("SQUARE_ENV=") == 1 and "SQUARE_ENV=production" in text
    assert read_env(p) == {"OTHER": "keep", "SQUARE_ENV": "production", "SQUARE_ACCESS_TOKEN": "tok123"}
    assert os.environ["SQUARE_ACCESS_TOKEN"] == "tok123"       # effective immediately
    assert square_credentials(tmp_path) == ("tok123", "production")


def test_missing_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SQUARE_ENV", raising=False)
    assert read_env(tmp_path / ".env") == {}
    assert square_credentials(tmp_path) == ("", "production")
    write_env(tmp_path / ".env", {"SQUARE_ENV": "sandbox"})
    assert (tmp_path / ".env").read_text() == "SQUARE_ENV=sandbox\n"
