"""Safe HTTP for provider adapters: timeouts, retries, backoff, redaction.

Every provider that reaches the network goes through :class:`SafeHttpClient`,
so the safety properties are written once and cannot be forgotten in an
adapter:

* a timeout on every request — no call can hang the run
* bounded retries with exponential backoff and jitter
* ``Retry-After`` honoured on 429 and 503
* a self-imposed rate limit, independent of the vendor's
* **credentials never reach a log line** — the redactor strips keys, tokens
  and auth headers from URLs, bodies and error text
* a health check that fails loudly rather than half-working

What this deliberately does not do: work around anything. No CAPTCHA solving,
no robots.txt evasion, no login or paywall circumvention, no anti-bot tricks.
An adapter that needs any of those is one this engine will not carry.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("wholesale_engine.http")

#: Query-parameter names whose values are secrets.
SECRET_PARAMS = (
    "key", "apikey", "api_key", "token", "access_token", "auth", "password",
    "secret", "client_secret", "signature", "sig",
)

#: Header names whose values are secrets.
SECRET_HEADERS = (
    "authorization", "x-api-key", "api-key", "x-auth-token", "cookie",
    "proxy-authorization", "x-access-token",
)

#: JSON body keys whose values are secrets.
SECRET_KEYS = SECRET_PARAMS + ("bearer", "credentials", "private_key")

_REDACTED = "***REDACTED***"

_PARAM_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(re.escape(p) for p in SECRET_PARAMS) + r")=([^&\s\"']+)"
)
#: Long opaque strings that look like keys even without a labelled parameter.
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._\-]{12,})")
_LONG_SECRET = re.compile(r"\b(sk|pk|key)[-_][A-Za-z0-9]{16,}\b", re.IGNORECASE)


def redact(text: object) -> str:
    """Strip anything that looks like a credential out of ``text``.

    Applied to every URL, error message and body before it can be logged.
    Over-redacting is fine; under-redacting is a leaked key.
    """
    value = str(text)
    value = _PARAM_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", value)
    value = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)} {_REDACTED}", value)
    value = _LONG_SECRET.sub(_REDACTED, value)
    return value


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """A copy of ``headers`` with every secret value replaced."""
    return {
        name: (_REDACTED if name.lower() in SECRET_HEADERS else redact(value))
        for name, value in headers.items()
    }


def redact_payload(payload: Any) -> Any:
    """Recursively redact secret-looking keys in a decoded JSON body."""
    if isinstance(payload, dict):
        return {
            key: (
                _REDACTED if key.lower() in SECRET_KEYS else redact_payload(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact(payload)
    return payload


class HttpError(RuntimeError):
    """A request failed after every retry. The message is already redacted."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(redact(message))
        self.status = status

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_auth_failure(self) -> bool:
        return self.status in (401, 403)


@dataclass
class RequestRecord:
    """One request, as it will appear in the log. No secrets, by construction."""

    method: str
    url: str  # already redacted
    status: Optional[int] = None
    duration_ms: float = 0.0
    attempt: int = 1
    error: str = ""

    def render(self) -> str:
        outcome = f"{self.status}" if self.status else f"ERROR {self.error}"
        return (
            f"{self.method} {self.url} -> {outcome} "
            f"({self.duration_ms:.0f}ms, attempt {self.attempt})"
        )


@dataclass
class HttpConfig:
    """Transport policy. Every default errs toward being a polite client."""

    timeout_seconds: float = 20.0
    max_retries: int = 3
    #: Seconds between requests this client makes, regardless of vendor limits.
    min_interval_seconds: float = 0.5
    #: Base for exponential backoff: wait base * 2**attempt.
    backoff_base_seconds: float = 1.0
    #: Ceiling on any single backoff wait.
    backoff_max_seconds: float = 60.0
    #: Random jitter fraction, so parallel runs do not retry in lockstep.
    jitter: float = 0.25
    #: Statuses worth retrying. 4xx other than 429 means our request is wrong.
    retry_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504)
    #: Cap on the honoured Retry-After, so a bad header cannot stall a run.
    max_retry_after_seconds: float = 120.0
    user_agent: str = "wholesale-engine/1.0"


@dataclass
class HttpStats:
    """Per-client counters, surfaced in the run's cost report."""

    requests: int = 0
    retries: int = 0
    failures: int = 0
    rate_limited: int = 0
    auth_failures: int = 0
    total_wait_seconds: float = 0.0
    log: List[RequestRecord] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "auth_failures": self.auth_failures,
            "total_wait_seconds": round(self.total_wait_seconds, 2),
        }


class SafeHttpClient:
    """An authenticated JSON client that cannot leak a key or hammer a vendor."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        config: Optional[HttpConfig] = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        if not base_url:
            raise ValueError(
                "a base URL is required and cannot be guessed — take it from the "
                "vendor's published API documentation"
            )
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"base URL must be https, got '{parsed.scheme or 'no scheme'}'. "
                "Credentials must never travel in the clear."
            )
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.config = config or HttpConfig()
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.extra_headers = dict(extra_headers or {})
        self.stats = HttpStats()
        self._last_request_at = 0.0

    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        # Never let a key reach a repr, a traceback or a debugger session.
        return f"<SafeHttpClient {self.base_url} key={'set' if self._api_key else 'unset'}>"

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.config.user_agent}
        headers.update(self.extra_headers)
        if self._api_key:
            headers[self.auth_header] = f"{self.auth_scheme} {self._api_key}".strip()
        return headers

    def _throttle(self) -> None:
        """Self-imposed rate limit. Never hammer a provider."""
        elapsed = time.monotonic() - self._last_request_at
        wait = self.config.min_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
            self.stats.total_wait_seconds += wait
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """How long to wait before the next attempt."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.config.max_retry_after_seconds)
        base = self.config.backoff_base_seconds * (2 ** attempt)
        capped = min(base, self.config.backoff_max_seconds)
        return capped * (1.0 + random.uniform(-self.config.jitter, self.config.jitter))

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.base_url}/{str(path).lstrip('/')}"
        return f"{url}?{query}" if query else url

    # ------------------------------------------------------------------

    def request(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One authenticated JSON request, with retries. Raises :class:`HttpError`."""
        url = self.build_url(path, params)
        safe_url = redact(url)
        payload = json.dumps(body).encode("utf-8") if body is not None else None

        last_error: Optional[Exception] = None
        last_status: Optional[int] = None

        for attempt in range(self.config.max_retries):
            self._throttle()
            request = urllib.request.Request(url, data=payload, method=method)
            for name, value in self._headers().items():
                request.add_header(name, value)
            if payload is not None:
                request.add_header("Content-Type", "application/json")

            started = time.monotonic()
            record = RequestRecord(method=method, url=safe_url, attempt=attempt + 1)
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read().decode("utf-8")
                    record.status = getattr(response, "status", 200)
                    record.duration_ms = (time.monotonic() - started) * 1000
                    self.stats.requests += 1
                    self.stats.log.append(record)
                    LOGGER.debug(record.render())
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                last_error, last_status = exc, exc.code
                record.status = exc.code
                record.duration_ms = (time.monotonic() - started) * 1000
                record.error = redact(exc.reason or "")
                self.stats.requests += 1
                self.stats.log.append(record)
                if exc.code == 429:
                    self.stats.rate_limited += 1
                if exc.code in (401, 403):
                    self.stats.auth_failures += 1
                    # Retrying a rejected credential just burns quota.
                    break
                if exc.code not in self.config.retry_statuses:
                    break
                if attempt + 1 < self.config.max_retries:
                    wait = self._backoff(attempt, self._retry_after(exc))
                    self.stats.retries += 1
                    self.stats.total_wait_seconds += wait
                    LOGGER.warning(
                        "%s %s -> %s; retrying in %.1fs", method, safe_url, exc.code, wait
                    )
                    time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                record.duration_ms = (time.monotonic() - started) * 1000
                record.error = redact(exc)
                self.stats.requests += 1
                self.stats.log.append(record)
                if attempt + 1 < self.config.max_retries:
                    wait = self._backoff(attempt)
                    self.stats.retries += 1
                    self.stats.total_wait_seconds += wait
                    time.sleep(wait)

        self.stats.failures += 1
        raise HttpError(
            f"{method} {safe_url} failed after {self.config.max_retries} attempt(s): "
            f"{last_error}",
            status=last_status,
        )

    # ------------------------------------------------------------------

    def health_check(self, path: str = "", params: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Is this provider reachable and are the credentials accepted?

        Returns ``(ok, message)``. The message is always safe to print.
        """
        if not self._api_key:
            return False, "no API key configured"
        try:
            self.request(path, params)
        except HttpError as exc:
            if exc.is_auth_failure:
                return False, "credentials rejected by the provider (401/403)"
            if exc.is_rate_limit:
                return False, "rate limited (429) — the key works, the quota does not"
            return False, str(exc)
        return True, "reachable, credentials accepted"
