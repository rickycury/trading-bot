"""Minimal .env loader — no external dependency.

Reads KEY=VALUE lines from the project-root .env and puts them in os.environ
(without clobbering anything already set in the real environment). Keeps secrets
like the Telegram token out of source. Call load_env() once at startup.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_env(path: Optional[str] = None) -> None:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)   # real env wins over the file
