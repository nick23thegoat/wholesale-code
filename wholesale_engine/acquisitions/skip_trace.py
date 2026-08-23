"""Skip tracing: the interface, and a mock that is obviously fake.

**No real provider is connected, and none will be until you choose one.**
Skip tracing is regulated — TCPA, state calling laws, DNC registries,
CAN-SPAM — and connecting one means an account, a contract, consent tracking,
DNC scrubbing and a suppression list. None of that is guessed at here.

What exists now:

* :class:`SkipTraceProvider` — the interface a real vendor implements
* :class:`UnconfiguredSkipTraceProvider` — the default, which refuses
* :class:`MockSkipTraceProvider` — **TEST DATA ONLY**

The mock exists so the acquisition workflow can be exercised end to end
without a vendor. Everything it returns is stamped ``is_test_data=True``,
uses the reserved 555-01xx range that can never connect to a real person, and
uses ``.invalid`` email addresses, which is a reserved TLD that can never
resolve. Every report renders such a contact as ``TEST DATA``. Nothing in the
engine can present a mock result as real.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from ..research.facts import Confidence
from .models import Contact, PhoneType


class SkipTraceNotConfigured(RuntimeError):
    """No skip-trace provider is connected."""


@dataclass
class SkipTraceResult:
    """What a skip trace came back with. Empty is a valid, common answer."""

    property_id: str = ""
    owner_name: Optional[str] = None
    phones: List[Dict[str, Any]] = field(default_factory=list)
    emails: List[Dict[str, Any]] = field(default_factory=list)
    mailing_address: Optional[str] = None
    source: str = ""
    source_date: Optional[date] = None
    is_test_data: bool = False
    cost: float = 0.0
    notes: str = ""

    @property
    def found_anything(self) -> bool:
        return bool(self.phones or self.emails or self.mailing_address)

    def best_phone(self) -> Optional[Dict[str, Any]]:
        """Highest-confidence phone, mobiles preferred at equal confidence."""
        if not self.phones:
            return None
        return max(
            self.phones,
            key=lambda p: (
                Confidence.parse(p.get("confidence")).rank,
                1 if PhoneType.parse(p.get("type")) is PhoneType.MOBILE else 0,
            ),
        )

    def best_email(self) -> Optional[Dict[str, Any]]:
        if not self.emails:
            return None
        return max(
            self.emails, key=lambda e: Confidence.parse(e.get("confidence")).rank
        )

    def to_contact(self, property_id: str = "") -> Contact:
        """Fold the result into a :class:`Contact`.

        A result with nothing in it produces a contact with every field blank,
        which is the honest outcome — not an empty string dressed up as data.
        """
        phone = self.best_phone() or {}
        email = self.best_email() or {}
        return Contact(
            property_id=property_id or self.property_id,
            owner_name=self.owner_name,
            phone=phone.get("number"),
            phone_type=PhoneType.parse(phone.get("type")),
            phone_confidence=Confidence.parse(phone.get("confidence")),
            email=email.get("address"),
            email_confidence=Confidence.parse(email.get("confidence")),
            mailing_address=self.mailing_address,
            source=self.source,
            source_date=self.source_date,
            verified=False,
            is_test_data=self.is_test_data,
            notes=self.notes,
        )


class SkipTraceProvider(ABC):
    """The interface a real skip-trace vendor implements.

    A vendor subclass supplies :meth:`skip_trace` from that vendor's published
    API documentation. Everything else — the contact model, the queue, the
    outreach log — is finished and needs no changes when one is added.

    Before connecting one: confirm the vendor's terms permit your use, and put
    consent tracking, DNC scrubbing and a suppression list in place first. The
    engine will not do that for you and does not pretend to.
    """

    name: str = "unconfigured"
    #: True for anything that returns fabricated data. Propagated onto every
    #: contact the provider produces so no report can misrepresent it.
    is_test_provider: bool = False
    #: Rough per-lookup cost, for the cost report. Skip tracing is billed per
    #: hit, which is why it runs last and only on leads worth pursuing.
    cost_per_lookup: float = 0.0

    def __init__(self) -> None:
        self.lookups = 0

    @abstractmethod
    def skip_trace(
        self,
        property_id: str,
        owner_name: Optional[str] = None,
        address: str = "",
        city: str = "",
        state: str = "",
        zip_code: str = "",
    ) -> SkipTraceResult:
        """Look up contact information for one owner."""

    def describe(self) -> str:
        kind = "TEST DATA ONLY" if self.is_test_provider else "live"
        return f"{self.name} ({kind}, ${self.cost_per_lookup:.2f}/lookup)"


class UnconfiguredSkipTraceProvider(SkipTraceProvider):
    """The default. Refuses, and says why."""

    name = "unconfigured"

    def skip_trace(self, property_id: str, **kwargs: Any) -> SkipTraceResult:
        raise SkipTraceNotConfigured(
            "No skip-trace provider is connected. This engine will never generate "
            "a phone number or an email address. Connecting a real provider needs "
            "a vendor account and, before you dial anyone, consent tracking, DNC "
            "scrubbing and a suppression list. Use --skip-trace-provider mock to "
            "exercise the workflow with clearly fictional test data."
        )


class MockSkipTraceProvider(SkipTraceProvider):
    """**TEST DATA ONLY.** Deterministic fictional contacts for the test suite.

    Everything it returns is unmistakably fake and unusable:

    * phone numbers in ``555-01xx``, reserved for fiction and never assignable
    * emails on ``.invalid``, a reserved TLD that can never resolve
    * every result stamped ``is_test_data=True``, which the reports render as
      ``TEST DATA`` and the exports carry as a column

    Deterministic by property id, so tests are stable.
    """

    name = "mock"
    is_test_provider = True
    cost_per_lookup = 0.0

    #: One in this many lookups returns nothing, so the "no contact found"
    #: path — which is common in reality — is actually exercised.
    MISS_EVERY = 5

    def __init__(self, hit_rate: Optional[float] = None) -> None:
        super().__init__()
        self.hit_rate = hit_rate

    def _seed(self, key: str) -> int:
        return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)

    def skip_trace(
        self,
        property_id: str,
        owner_name: Optional[str] = None,
        address: str = "",
        city: str = "",
        state: str = "",
        zip_code: str = "",
    ) -> SkipTraceResult:
        self.lookups += 1
        seed = self._seed(property_id or address or "unknown")

        result = SkipTraceResult(
            property_id=property_id,
            owner_name=owner_name,
            source="mock-skip-trace (FICTIONAL TEST DATA)",
            source_date=date.today(),
            is_test_data=True,
            notes=(
                "FICTIONAL TEST DATA from the mock skip-trace provider. These are "
                "reserved 555-01xx numbers and .invalid addresses. They do not "
                "belong to anyone and must never be dialled or emailed."
            ),
        )

        # Some lookups find nothing. That is the realistic case and the one
        # the contact queue has to handle.
        if seed % self.MISS_EVERY == 0:
            result.notes = "FICTIONAL TEST DATA: mock lookup returned no contact."
            return result

        line = 100 + (seed % 100)
        result.phones.append(
            {
                "number": f"555555{line:04d}",
                "type": "MOBILE" if seed % 2 else "LANDLINE",
                "confidence": "MEDIUM" if seed % 3 else "HIGH",
            }
        )
        if seed % 3 == 0:
            result.phones.append(
                {"number": f"555555{(line + 1) % 10000:04d}", "type": "LANDLINE",
                 "confidence": "LOW"}
            )
        if seed % 2 == 0:
            slug = (owner_name or f"owner{seed % 1000}").lower().replace(" ", ".")
            result.emails.append(
                {"address": f"{slug}@example.invalid", "confidence": "LOW"}
            )
        if address:
            result.mailing_address = f"{address}, {city} {state} {zip_code}".strip()
        return result


#: Registry for ``--skip-trace-provider``. Only the mock is available; adding
#: a real one means implementing SkipTraceProvider against its documentation.
SKIP_TRACE_PROVIDERS = {
    "none": UnconfiguredSkipTraceProvider,
    "mock": MockSkipTraceProvider,
}


def get_skip_trace_provider(name: str = "none") -> SkipTraceProvider:
    key = (name or "none").strip().lower()
    if key not in SKIP_TRACE_PROVIDERS:
        raise SkipTraceNotConfigured(
            f"unknown skip-trace provider '{name}'. Available: "
            f"{', '.join(SKIP_TRACE_PROVIDERS)}. No paid vendor is wired in — "
            "adding one means reading that vendor's API documentation and "
            "meeting its compliance requirements first."
        )
    return SKIP_TRACE_PROVIDERS[key]()
