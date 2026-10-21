"""Read and write the .env file next to the app (SQUARE_ACCESS_TOKEN, SQUARE_ENV)."""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"
TOKEN_KEY = "SQUARE_ACCESS_TOKEN"
ENV_KEY = "SQUARE_ENV"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Update keys in place, keep comments and unrelated lines, append new keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    for k, v in updates.items():
        os.environ[k] = v          # take effect in this process immediately


def square_credentials(root: Path) -> tuple[str, str]:
    """(token, env) from .env, falling back to the process environment."""
    env = read_env(root / ENV_FILENAME)
    token = env.get(TOKEN_KEY) or os.environ.get(TOKEN_KEY, "")
    square_env = env.get(ENV_KEY) or os.environ.get(ENV_KEY, "production")
    return token, square_env
