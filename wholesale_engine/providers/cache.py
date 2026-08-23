"""A disk cache for provider responses, so a re-run does not re-spend money.

On a 50-request-per-month plan, the difference between a cached hunt and an
uncached one is the difference between a tool you can iterate on and one you
can use twice. Everything downstream of the provider — scoring, filtering,
analysis, reports — is free and deterministic, so re-running the whole funnel
against a cached response costs nothing and answers most "what if" questions.

Design rules:

* **the cache key never contains the API key.** It is derived from the
  endpoint and the request parameters only, with any credential-shaped
  parameter dropped before hashing, so a cache file cannot leak a secret and
  rotating your key does not invalidate the cache.
* **only successful responses are cached.** An error is not an answer, and
  caching one would turn a transient failure into a persistent one.
* **entries expire.** Property records change slowly, valuations quickly, so
  the TTL is set per call rather than globally.
* **a corrupt or unreadable entry is a miss**, never an exception — a bad
  cache file must not break a run.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

#: Where cached responses live. Git-ignored: it is machine-local, and it
#: contains real property data pulled under your account.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

#: Parameter names dropped before the key is hashed. Belt and braces: no
#: adapter should be putting a key in a query parameter, and if one ever does,
#: it still will not reach a filename or a cache file.
CREDENTIAL_PARAMS = (
    "key", "apikey", "api_key", "token", "access_token", "auth", "password",
    "secret", "signature", "sig",
)

#: Sensible defaults. Public record data barely moves month to month; an
#: automated valuation does, so it is trusted for a much shorter window.
TTL_PROPERTY_RECORDS = 30 * 24 * 3600     # 30 days
TTL_VALUATION = 7 * 24 * 3600             # 7 days
TTL_LISTINGS = 24 * 3600                  # 1 day


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    expired: int = 0

    @property
    def requests_saved(self) -> int:
        """Billable requests this cache prevented."""
        return self.hits

    def render(self) -> str:
        total = self.hits + self.misses
        rate = f"{(self.hits / total * 100):.0f}%" if total else "—"
        return "\n".join([
            "RESPONSE CACHE",
            f"  Hits                {self.hits}   ({rate} of lookups)",
            f"  Misses              {self.misses}",
            f"  Entries written     {self.writes}",
            f"  Expired            {self.expired}",
            f"  Billable requests avoided: {self.requests_saved}",
        ])


@dataclass
class ResponseCache:
    """Cached provider responses on disk, keyed by endpoint and parameters."""

    directory: Path = DEFAULT_CACHE_DIR
    provider: str = "rentcast"
    #: Set False for --no-cache: lookups always miss and nothing is written.
    enabled: bool = True
    stats: CacheStats = field(default_factory=CacheStats)

    # ------------------------------------------------------------------

    def key(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """A stable hash of the request, with credentials excluded."""
        safe = {
            str(name): params[name]
            for name in sorted(params or {})
            if str(name).lower() not in CREDENTIAL_PARAMS
        }
        payload = json.dumps(
            {"provider": self.provider, "path": str(path).strip("/"), "params": safe},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def _file(self, key: str) -> Path:
        return self.directory / f"{self.provider}_{key}.json"

    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        ttl_seconds: int = TTL_PROPERTY_RECORDS,
    ) -> Optional[Any]:
        """The cached response, or ``None`` for a miss.

        A miss is also what you get for an expired, unreadable or malformed
        entry — the caller then makes the real request, which is always safe.
        """
        if not self.enabled:
            self.stats.misses += 1
            return None
        target = self._file(self.key(path, params))
        if not target.exists():
            self.stats.misses += 1
            return None
        try:
            entry = json.loads(target.read_text(encoding="utf-8"))
            stored_at = float(entry["stored_at"])
            payload = entry["response"]
        except (OSError, ValueError, TypeError, KeyError):
            self.stats.misses += 1
            return None
        if ttl_seconds >= 0 and (time.time() - stored_at) > ttl_seconds:
            self.stats.expired += 1
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return payload

    def put(
        self,
        path: str,
        params: Optional[Dict[str, Any]],
        response: Any,
    ) -> None:
        """Store a **successful** response. Errors are never cached."""
        if not self.enabled or response is None:
            return
        safe_params = {
            str(name): value
            for name, value in (params or {}).items()
            if str(name).lower() not in CREDENTIAL_PARAMS
        }
        entry = {
            "provider": self.provider,
            "path": str(path).strip("/"),
            "params": safe_params,
            "stored_at": time.time(),
            "stored_at_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
            "response": response,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._file(self.key(path, params)).write_text(
                json.dumps(entry, indent=2, default=str), encoding="utf-8"
            )
            self.stats.writes += 1
        except (OSError, TypeError, ValueError):
            # A cache we cannot write is a performance problem, not a
            # correctness one. The real response has already been returned.
            pass

    # ------------------------------------------------------------------

    def entries(self) -> int:
        if not self.directory.exists():
            return 0
        return len(list(self.directory.glob(f"{self.provider}_*.json")))

    def clear(self) -> int:
        """Delete every cached response for this provider. Returns the count."""
        removed = 0
        if not self.directory.exists():
            return 0
        for target in self.directory.glob(f"{self.provider}_*.json"):
            try:
                target.unlink()
                removed += 1
            except OSError:
                pass
        return removed
