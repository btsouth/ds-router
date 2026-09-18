"""Resolve Hermes paths the way Hermes itself does.

Hermes is profile-aware: `$HERMES_HOME` overrides `~/.hermes`, and profiles live
under `~/.hermes/profiles/<name>/`. Reading `Path.home() / ".hermes"` directly
silently picks the wrong tree when HERMES_HOME is set -- which happens in
delegated subprocesses, tests, and any tool run under a different HOME.

Every module here goes through this so the resolution cannot drift.
"""

from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    """The active Hermes home: $HERMES_HOME if set, else ~/.hermes."""
    override = str(os.environ.get("HERMES_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"


def env_file() -> Path:
    """The .env holding provider API keys."""
    return hermes_home() / ".env"


def state_db() -> Path:
    """Hermes' SQLite session store."""
    return hermes_home() / "state.db"


def config_file() -> Path:
    """Hermes' config.yaml."""
    return hermes_home() / "config.yaml"


def default_collector_state_dir() -> Path:
    """The *fallback* cache location, for reference and documentation only.

    The live value comes from ``routing.collector_state_dir`` in config.yaml and is
    resolved by ``router._expanded``; this function is not in that path. Two
    implementations of one rule is how they drift, so callers should read the
    config rather than call this.

    Not under HERMES_HOME: a usage cache belongs to whichever separate tool writes
    it, with its own XDG state dir, so this honours XDG_STATE_HOME the way such
    tools do.
    """
    xdg = str(os.environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "omarchy" / "ai-usage"
