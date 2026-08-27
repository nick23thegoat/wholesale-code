"""BatchData skip-trace adapter.

Implements :class:`~wholesale_engine.acquisitions.skip_trace.SkipTraceProvider`
against BatchData's published API. The wire format lives in
:mod:`wholesale_engine.acquisitions.batchdata_schema`, where every value is
labelled CONFIRMED, UNVERIFIED or NOT KNOWN with its source.

It reads ``BATCHDATA_API_KEY`` rather than the generic ``SKIP_TRACE_API_KEY``
and ``SKIP_TRACE_BASE_URL`` that the :class:`HttpSkipTraceProvider` template
expects, for the same reason the RentCast adapter uses its own variable: the
base URL is a published fact about this vendor, not something to be configured
per install.

**Nothing in this file logs a response.** A skip-trace payload is the most
sensitive data this engine ever touches — phone numbers, email addresses and
mailing addresses belonging to real people who did not ask to be looked up.
Printing one to stderr on a server writes it into journald in plaintext for as
long as the log rotation allows. Transport errors go through
:class:`SafeHttpClient`, whose messages are redacted by construction.

Standing compliance note, carried over from ``skip_trace.py``: before using
this for real outreach, confirm BatchData's terms permit your use, and put
consent tracking, DNC scrubbing and a suppression list in place. This engine
maintains the suppression list; the rest is on you. This adapter does **not**
scrub DNC for you and must not be mistaken for something that does.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..providers.http_client import HttpConfig, HttpError, SafeHttpClient
from .batchdata_schema import (
    API_KEY_VAR,
    AUTH_HEADER,
    AUTH_SCHEME,
    BASE_URL,
    CONFIDENCE_CANDIDATE_KEYS,
    COST_VAR,
    EMAIL_CANDIDATE_KEYS,
    EMAIL_VALUE_KEYS,
    MAILING_ADDRESS_CANDIDATE_KEYS,
    MAX_BULK_PROPERTIES,
    MIN_SECONDS_BETWEEN_CALLS,
    PHONE_CANDIDATE_KEYS,
    PHONE_TYPE_KEYS,
    PHONE_VALUE_KEYS,
    RESULT_LIST_KEYS,
    SKIP_TRACE_BULK_PATH,
    SKIP_TRACE_PATH,
)
from .skip_trace import SkipTraceNotConfigured, SkipTraceProvider, SkipTraceResult

#: Said once, in the one place a response shape can disappoint you.
UNMATCHED_NOTE = (
    "BatchData answered, but no phone, email or mailing field matched the "
    "candidate keys tried. The response shape is unconfirmed for this account "
    "— inspect one real payload and update the candidate keys in "
    "batchdata_schema.py. Nothing has been invented to fill the gap."
)


def _first_present(payload: Any, keys: Sequence[str]) -> Optional[Any]:
    """The first candidate key carrying a truthy value, or ``None``."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return None


def _first_list(payload: Any, keys: Sequence[str]) -> List[Dict[str, Any]]:
    """The first key holding a list of records, or an empty list."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _confidence(entry: Dict[str, Any]) -> Optional[str]:
    """BatchData's confidence for one contact, passed through unchanged.

    ``Confidence.parse`` handles both the word forms and the numeric scores,
    and an unrecognised value becomes UNKNOWN. No score is manufactured for an
    entry that came without one — an unrated number is not a good number.
    """
    raw = _first_present(entry, CONFIDENCE_CANDIDATE_KEYS)
    return _text(raw) or None


def _normalize_contacts(
    raw: Any, value_keys: Sequence[str], out_field: str, type_keys: Sequence[str] = ()
) -> List[Dict[str, Any]]:
    """BatchData's phone/email entries into the shape ``SkipTraceResult`` reads.

    ``SkipTraceResult.best_phone`` looks for ``number``/``type``/``confidence``
    and ``best_email`` for ``address``/``confidence``, so an entry whose value
    lives under ``phoneNumber`` has to be moved, not passed through — passing
    it through is how a found number silently becomes a blank contact.

    Entries with no recognisable value are dropped rather than kept as empty
    records.
    """
    if not raw:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    out: List[Dict[str, Any]] = []
    for item in entries:
        if isinstance(item, str):
            value = item.strip()
            if value:
                out.append({out_field: value})
            continue
        if not isinstance(item, dict):
            continue
        value = _text(_first_present(item, value_keys))
        if not value:
            continue
        contact: Dict[str, Any] = {out_field: value}
        if type_keys:
            kind = _text(_first_present(item, type_keys))
            if kind:
                contact["type"] = kind
        confidence = _confidence(item)
        if confidence:
            contact["confidence"] = confidence
        out.append(contact)
    return out


class BatchDataSkipTraceProvider(SkipTraceProvider):
    """BatchData skip trace, single and bulk.

    ``skip_trace`` satisfies the base interface, one property per request.
    ``skip_trace_bulk`` is the one a lead-list run should use: BatchData
    accepts up to 100 properties in a single request, and a hundred separate
    calls for the same hundred owners is a hundred times the rate-limit
    pressure for the same answer.
    """

    name = "batchdata"
    is_test_provider = False
    #: NOT KNOWN — per-lookup pricing is contract-specific and is never
    #: invented here. Set SKIP_TRACE_COST_PER_LOOKUP in .env with your own
    #: rate and the cost report will use it; left at zero it reports zero,
    #: which the report labels as unknown rather than free.
    cost_per_lookup: float = 0.0

    def __init__(self, client: Optional[SafeHttpClient] = None) -> None:
        super().__init__()
        self.cost_per_lookup = _env_float(COST_VAR, 0.0)
        #: Reasons a lookup produced nothing, for the run report. Never a
        #: response body — these are safe to print.
        self.warnings: List[str] = []

        if client is not None:
            # Injected for tests. No credentials are read and nothing is sent.
            self.client = client
            return

        api_key = os.environ.get(API_KEY_VAR, "").strip()
        if not api_key:
            raise SkipTraceNotConfigured(
                f"batchdata is NOT CONNECTED: {API_KEY_VAR} is not set. Create a "
                "server token in your BatchData account, put it in .env, and never "
                "commit it. A sandbox token returns mock data at no cost if you "
                "want to prove the wiring first."
            )
        self.client = SafeHttpClient(
            BASE_URL,
            api_key,
            HttpConfig(min_interval_seconds=MIN_SECONDS_BETWEEN_CALLS),
            auth_header=AUTH_HEADER,
            auth_scheme=AUTH_SCHEME,
        )

    # ------------------------------------------------------------------

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        """One request. Raises :class:`HttpError` with a redacted message.

        Everything that makes this safe — timeouts, bounded retries with
        backoff and jitter, ``Retry-After``, the https-only check, credential
        redaction, and never retrying a rejected credential — comes from
        :class:`SafeHttpClient` rather than being re-implemented here, which
        is the only way those properties stay true.
        """
        return self.client.request(path, method="POST", body=body)

    def _failure(self, property_id: str, exc: HttpError) -> SkipTraceResult:
        """A failed lookup, with a reason that carries no credential."""
        if exc.is_auth_failure:
            note = (
                f"BatchData rejected the credential ({exc.status}). Check "
                f"{API_KEY_VAR} and that the token type is 'server'."
            )
        elif exc.is_rate_limit:
            note = "BatchData rate limit reached (429). The token works; the quota does not."
        else:
            note = str(exc)
        self.warnings.append(note)
        return SkipTraceResult(property_id=property_id, source=self.name, notes=note)

    @staticmethod
    def _request_entry(
        address: str, city: str, state: str, zip_code: str
    ) -> Dict[str, str]:
        return {
            "address": address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
        }

    def _parse(
        self,
        record: Dict[str, Any],
        property_id: str,
        owner_name: Optional[str] = None,
    ) -> SkipTraceResult:
        """One BatchData record -> a :class:`SkipTraceResult`.

        A record that yields nothing produces an empty result with a note, not
        an exception: "no contact found" is the single most common honest
        outcome of a skip trace and the queue is built to handle it.
        """
        phones = _normalize_contacts(
            _first_present(record, PHONE_CANDIDATE_KEYS),
            PHONE_VALUE_KEYS, "number", PHONE_TYPE_KEYS,
        )
        emails = _normalize_contacts(
            _first_present(record, EMAIL_CANDIDATE_KEYS), EMAIL_VALUE_KEYS, "address",
        )
        mailing_raw = _first_present(record, MAILING_ADDRESS_CANDIDATE_KEYS)
        if isinstance(mailing_raw, dict):
            mailing = _text(mailing_raw.get("formattedAddress")) or _text(
                ", ".join(
                    part for part in (
                        _text(mailing_raw.get("street")),
                        _text(mailing_raw.get("city")),
                        _text(mailing_raw.get("state")),
                        _text(mailing_raw.get("zip")),
                    ) if part
                )
            )
        else:
            mailing = _text(mailing_raw)

        result = SkipTraceResult(
            property_id=property_id,
            owner_name=owner_name,
            phones=phones,
            emails=emails,
            mailing_address=mailing or None,
            source=self.name,
            source_date=date.today(),
            is_test_data=False,
            cost=self.cost_per_lookup,
        )
        if not result.found_anything:
            result.notes = UNMATCHED_NOTE if record else "BatchData found no contact."
        return result

    # ------------------------------------------------------------------

    def skip_trace(
        self,
        property_id: str,
        owner_name: Optional[str] = None,
        address: str = "",
        city: str = "",
        state: str = "",
        zip_code: str = "",
    ) -> SkipTraceResult:
        if not address:
            return SkipTraceResult(
                property_id=property_id,
                source=self.name,
                notes="no address supplied — refusing to guess a search target",
            )

        body = {"requests": [self._request_entry(address, city, state, zip_code)]}
        try:
            payload = self._post(SKIP_TRACE_PATH, body)
        except HttpError as exc:
            return self._failure(property_id, exc)

        self.lookups += 1
        records = _first_list(payload, RESULT_LIST_KEYS)
        record = records[0] if records else (payload if isinstance(payload, dict) else {})
        return self._parse(record, property_id, owner_name)

    def skip_trace_bulk(
        self, properties: List[Dict[str, str]]
    ) -> List[SkipTraceResult]:
        """Up to 100 properties in one request.

        Results are matched to inputs **by position**, which is what
        BatchData's request/response arrays imply and which has not been
        confirmed against a live response. A property whose slot came back
        empty gets an empty result rather than someone else's contact details
        — mismatching those would be far worse than finding nothing.
        """
        if not properties:
            return []
        if len(properties) > MAX_BULK_PROPERTIES:
            raise ValueError(
                f"BatchData bulk skip trace accepts at most {MAX_BULK_PROPERTIES} "
                f"properties per request; got {len(properties)}. Split into "
                "smaller batches."
            )

        body = {
            "requests": [
                self._request_entry(
                    _text(p.get("address")), _text(p.get("city")),
                    _text(p.get("state")), _text(p.get("zip_code")),
                )
                for p in properties
            ]
        }
        try:
            payload = self._post(SKIP_TRACE_BULK_PATH, body)
        except HttpError as exc:
            return [self._failure(_text(p.get("property_id")), exc) for p in properties]

        self.lookups += len(properties)
        records = _first_list(payload, RESULT_LIST_KEYS)
        if len(records) != len(properties):
            self.warnings.append(
                f"BatchData returned {len(records)} record(s) for "
                f"{len(properties)} propert(ies). Results are matched by position, "
                "so the surplus or shortfall is left unmatched rather than guessed."
            )
        return [
            self._parse(
                records[index] if index < len(records) else {},
                _text(prop.get("property_id")),
                prop.get("owner_name") or None,
            )
            for index, prop in enumerate(properties)
        ]

    # ------------------------------------------------------------------

    def describe(self) -> str:
        rate = (
            f"${self.cost_per_lookup:.2f}/lookup"
            if self.cost_per_lookup
            else f"cost/lookup unknown — set {COST_VAR}"
        )
        return f"{self.name} (live, {rate})"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        return default
    return value if value >= 0 else default
