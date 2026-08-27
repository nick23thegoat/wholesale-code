"""Where the engine keeps its data, and how a deployment moves it.

By default everything lives inside the package — ``wholesale_engine/data`` —
which is right for a clone you run by hand and wrong for a server. On a VPS the
code directory should be replaceable by ``git pull`` and ideally read-only,
while the database, the cache and the quota ledger have to survive that.

``WHOLESALE_DATA_DIR`` moves all of them at once. Set it to ``/var/lib/wholesale``
in the systemd unit's environment file and the deployed service reads and
writes there instead.

Every path returned is **absolute**, resolved from this file rather than from
the current directory. That is what stops a service with a different
WorkingDirectory quietly creating a second, empty database beside the first —
the failure mode where a dashboard shows nothing and the leads are still fine,
somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

#: Overrides the data directory wholesale. Absolute paths only.
DATA_DIR_VAR = "WHOLESALE_DATA_DIR"
#: Overrides the buy box on its own, since it is configuration rather than data.
BUYBOX_PATH_VAR = "BUYBOX_PATH"


def data_dir() -> Path:
    """Where the database, cache and ledger live.

    A relative override is ignored rather than honoured: resolving it against
    whatever the working directory happens to be is exactly the bug this
    module exists to prevent.
    """
    raw = os.environ.get(DATA_DIR_VAR, "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
    return PACKAGE_ROOT / "data"


def database_path() -> Path:
    return data_dir() / "leads.db"


def cache_dir() -> Path:
    return data_dir() / "cache"


def ledger_path() -> Path:
    return data_dir() / "api_usage.json"


def config_dir() -> Path:
    """Configuration, which is edited rather than generated."""
    raw = os.environ.get(DATA_DIR_VAR, "").strip()
    if raw and Path(raw).is_absolute():
        return Path(raw) / "config"
    return REPO_ROOT / "config"


def describe() -> str:
    """Every resolved path, for the health check and the runbook."""
    overridden = bool(os.environ.get(DATA_DIR_VAR, "").strip())
    lines = [
        "RESOLVED PATHS",
        f"  data directory      {data_dir()}",
        f"    database          {database_path()}",
        f"    response cache    {cache_dir()}",
        f"    quota ledger      {ledger_path()}",
        f"  config directory    {config_dir()}",
        "",
        f"  {DATA_DIR_VAR} is "
        + (f"set to {os.environ.get(DATA_DIR_VAR)}" if overridden else "not set (using the package default)"),
    ]
    return "\n".join(lines)
