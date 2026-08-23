"""Credential and endpoint configuration, read from the environment.

Nothing here ever contains a real key. Values come from the process
environment, optionally seeded from a local ``.env`` file that is git-ignored.
:func:`load_dotenv` is a deliberately small parser so the engine keeps its
zero-dependency runtime.

The public question this module answers is: *which providers, if any, are
actually configured?* Everything downstream keys off that answer instead of
guessing, so an unconfigured engine says so plainly rather than fabricating
results.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

#: The environment variables the engine reads. Mirrored in ``.env.example``.
ENV_VARS = (
    "WHOLESALE_MODE",
    "DATA_PROVIDER",
    "COMPS_PROVIDER",
    "SKIP_TRACE_PROVIDER",
    "NOTIFICATION_PROVIDER",
    "MAX_RAW_LEADS",
    "MAX_RESEARCH",
    "MAX_COMPS",
    "MAX_SKIP_TRACES",
    "PROPERTY_DATA_API_KEY",
    "PROPERTY_DATA_BASE_URL",
    "PUBLIC_RECORDS_API_KEY",
    "COMPS_API_KEY",
    "SKIP_TRACE_API_KEY",
)

#: Where a local .env is looked for, relative to the repository root.
DOTENV_NAME = ".env"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> Dict[str, str]:
    """Read ``KEY=value`` lines from ``path`` into ``os.environ``.

    Existing environment variables win unless ``override`` is set — a real
    exported credential should never be silently replaced by a stale file.
    Missing file is not an error: running without any credentials is a
    supported, first-class mode.

    Returns the mapping that was applied (never the values already set).
    """
    target = Path(path) if path else _repo_root() / DOTENV_NAME
    applied: Dict[str, str] = {}
    if not target.exists():
        return applied
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def env(name: str) -> Optional[str]:
    """One environment variable, with blank treated as absent."""
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class ProviderSettings:
    """The credentials and endpoints available to this run.

    Every field is ``None`` unless the corresponding environment variable is
    set. There are no defaults and no placeholder endpoints: a base URL has to
    come from the vendor's own documentation, so the engine will not invent one.
    """

    property_data_api_key: Optional[str] = None
    property_data_base_url: Optional[str] = None
    public_records_api_key: Optional[str] = None
    comps_api_key: Optional[str] = None
    skip_trace_api_key: Optional[str] = None

    @classmethod
    def from_env(cls, load_file: bool = True) -> "ProviderSettings":
        if load_file:
            load_dotenv()
        return cls(
            property_data_api_key=env("PROPERTY_DATA_API_KEY"),
            property_data_base_url=env("PROPERTY_DATA_BASE_URL"),
            public_records_api_key=env("PUBLIC_RECORDS_API_KEY"),
            comps_api_key=env("COMPS_API_KEY"),
            skip_trace_api_key=env("SKIP_TRACE_API_KEY"),
        )

    # -- what is actually usable ----------------------------------------

    @property
    def has_property_data(self) -> bool:
        """A live property search needs BOTH a key and an endpoint."""
        return bool(self.property_data_api_key and self.property_data_base_url)

    @property
    def has_comps(self) -> bool:
        return bool(self.comps_api_key)

    @property
    def has_public_records(self) -> bool:
        return bool(self.public_records_api_key)

    @property
    def has_skip_trace(self) -> bool:
        return bool(self.skip_trace_api_key)

    def missing_for_property_data(self) -> List[str]:
        """Which variables still need setting before a live search can run."""
        missing = []
        if not self.property_data_api_key:
            missing.append("PROPERTY_DATA_API_KEY")
        if not self.property_data_base_url:
            missing.append("PROPERTY_DATA_BASE_URL")
        return missing

    def describe(self) -> str:
        """A one-line, credential-free summary safe to print or log."""
        configured = [
            name
            for name, present in (
                ("property-data", self.has_property_data),
                ("public-records", self.has_public_records),
                ("comps", self.has_comps),
                ("skip-trace", self.has_skip_trace),
            )
            if present
        ]
        if not configured:
            return "no live data credentials configured"
        return "configured: " + ", ".join(configured)


#: Message shown whenever a live provider was asked for but is not configured.
NO_PROVIDER_MESSAGE = (
    "No live property-data provider configured. Running in CSV/test mode."
)
