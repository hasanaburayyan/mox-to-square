"""Settings loading. config/settings.yaml is the base; CLI flags override single keys."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")
DEFAULT_OVERRIDES_PATH = Path("config/set_code_overrides.yaml")


def default_root() -> Path:
    """Project root: the folder next to the packaged exe when frozen, else the cwd."""
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()

_DEFAULTS: dict[str, Any] = {
    "tier": {"threshold_usd": 0, "bulk_buckets": []},
    "sku": {"include_language": False},
    "square": {"location_name": "", "category": "MTG Singles", "variation_cap": 250,
               "template": "config/square_template.xlsx"},
    "pricing": {"source": "scryfall", "markup": 1.0, "round_to": 0.25,
                "condition_multipliers": {},
                "scg": {"enabled": False, "rate_limit_seconds": 2, "max_cards": 200,
                        "cache_ttl_hours": 168}},
    "scryfall": {"cache_ttl_hours": 24, "user_agent": "mtgsync/0.1"},
    "images": {"variations": True},
    "moxfield": {"subtract_tradelist": False, "english_only": False},
    "paths": {"cache": "cache", "work": "work", "output": "output", "state": "state"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Settings:
    data: dict[str, Any] = field(default_factory=dict)
    set_code_overrides: dict[str, str] = field(default_factory=dict)
    root: Path = field(default_factory=Path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def path(self, key: str) -> Path:
        """Resolve a path-valued key relative to the project root."""
        raw = self.get(key)
        if raw is None:
            raise KeyError(key)
        p = Path(raw)
        return p if p.is_absolute() else self.root / p


def load_settings(config_path: Path | str | None = None,
                  overrides_path: Path | str | None = None,
                  root: Path | str | None = None) -> Settings:
    root = Path(root) if root else default_root()
    config_path = Path(config_path) if config_path else root / DEFAULT_CONFIG_PATH
    overrides_path = Path(overrides_path) if overrides_path else root / DEFAULT_OVERRIDES_PATH

    data: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    overrides: dict[str, str] = {}
    if overrides_path.exists():
        with open(overrides_path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
            overrides = {str(k).lower(): str(v).lower() for k, v in loaded.items()}
    return Settings(data=_deep_merge(_DEFAULTS, data), set_code_overrides=overrides, root=root)
