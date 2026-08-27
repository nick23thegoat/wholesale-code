"""Owner research: who owns it, from records that publish ownership.

**Ownership is not contact information.** A name and a mailing address of
record are published facts. A phone number or an email address is skip
tracing — a separate, regulated activity behind its own interface, with no
provider attached and nothing here that touches it.

The service works from whatever the lead already carries and from a provider's
``get_owner()`` when one supports it. With neither, it returns a record of
unknowns naming what is missing. It never generates an owner name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..lead_hunter.models import Lead
from ..providers.base import Capability, PropertyDataProvider
from .facts import (
    SOURCE_DERIVED,
    SOURCE_LEAD_LIST,
    Confidence,
    Fact,
    best,
    missing_names,
)

#: Suffixes that mark a business rather than a person. An entity owner changes
#: who you negotiate with and how the deed gets signed, so it is worth knowing.
_ENTITY_MARKERS = (
    "llc", "l.l.c", "inc", "incorporated", "corp", "corporation", "company",
    "co.", "ltd", "limited", "trust", "tr", "estate", "partners", "lp", "llp",
    "properties", "holdings", "group", "capital", "investments", "enterprises",
    "management", "associates", "bank", "n.a.",
)

#: Names that mean "nobody looked", not "the owner is called this".
_PLACEHOLDER_NAMES = (
    "", "unknown", "n/a", "na", "none", "owner", "current owner",
    "owner of record", "-", "--",
)


def looks_like_entity(name: str) -> bool:
    """True when the owner name reads as a company, trust or estate."""
    if not name:
        return False
    tokens = re.split(r"[\s,./]+", name.lower())
    return any(token.strip(".") in {m.strip(".") for m in _ENTITY_MARKERS} for token in tokens)


def entity_kind(name: str) -> Optional[str]:
    """A rough label for the entity type, or None for a person."""
    lowered = name.lower()
    for kind, markers in (
        ("TRUST", ("trust", " tr ")),
        ("ESTATE", ("estate",)),
        ("LLC", ("llc", "l.l.c")),
        ("CORPORATION", ("inc", "corp", "incorporated", "corporation")),
        ("PARTNERSHIP", ("lp", "llp", "partners", "partnership")),
        ("BANK", ("bank", "n.a.")),
    ):
        if any(marker in lowered for marker in markers):
            return kind
    return "COMPANY" if looks_like_entity(name) else None


def is_placeholder(name: Optional[str]) -> bool:
    return (name or "").strip().lower() in _PLACEHOLDER_NAMES


@dataclass
class OwnerRecord:
    """Ownership of record. No contact information, ever."""

    owner_name: Fact[str] = field(default_factory=Fact.unknown)
    owner_mailing_address: Fact[str] = field(default_factory=Fact.unknown)
    ownership_years: Fact[float] = field(default_factory=Fact.unknown)
    properties_owned: Fact[int] = field(default_factory=Fact.unknown)
    absentee_owner: Fact[bool] = field(default_factory=Fact.unknown)
    is_entity: Fact[bool] = field(default_factory=Fact.unknown)
    entity_type: Fact[str] = field(default_factory=Fact.unknown)

    source: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def fields(self) -> Dict[str, Fact]:
        return {
            "owner_name": self.owner_name,
            "owner_mailing_address": self.owner_mailing_address,
            "ownership_years": self.ownership_years,
            "properties_owned": self.properties_owned,
            "absentee_owner": self.absentee_owner,
            "is_entity": self.is_entity,
            "entity_type": self.entity_type,
        }

    @property
    def missing_fields(self) -> List[str]:
        return missing_names(self.fields)

    @property
    def is_known(self) -> bool:
        return self.owner_name.is_known

    @property
    def is_likely_portfolio_owner(self) -> bool:
        """Several properties owned — the tired-landlord conversation."""
        return (self.properties_owned.value or 0) >= 3

    def describe(self) -> str:
        if not self.owner_name.is_known:
            return "owner unknown — no ownership source configured"
        parts = [str(self.owner_name.value)]
        if self.is_entity.is_true and self.entity_type.is_known:
            parts.append(f"({self.entity_type.value})")
        if self.absentee_owner.is_true:
            parts.append("— absentee")
        return " ".join(parts)


class OwnerResearchService:
    """Assembles an :class:`OwnerRecord` from the sources available.

    Order of preference: a provider's ownership record (a published fact),
    then the lead list (somebody's claim), then what can be inferred from
    those two. Nothing is fabricated at any step.
    """

    #: What a real ownership provider would need to supply.
    SUPPORTED_FIELDS = (
        "owner_name",
        "owner_mailing_address",
        "ownership_years",
        "properties_owned",
        "absentee_owner",
        "is_entity",
        "entity_type",
    )

    def __init__(self, provider: Optional[PropertyDataProvider] = None) -> None:
        self.provider = provider
        self.calls = 0
        self.unsupported_note = ""

    # ------------------------------------------------------------------

    def research(self, lead: Lead) -> OwnerRecord:
        """Everything knowable about this owner right now."""
        record = self._from_lead(lead)
        provider_record = self._from_provider(lead)
        if provider_record is not None:
            record = self._merge(record, provider_record)
        self._infer(record, lead)
        return record

    # ------------------------------------------------------------------

    def _from_lead(self, lead: Lead) -> OwnerRecord:
        record = OwnerRecord(source=SOURCE_LEAD_LIST)
        name = (lead.owner_name or "").strip()
        if name and not is_placeholder(name):
            record.owner_name = Fact.reported(name, SOURCE_LEAD_LIST, Confidence.MEDIUM)
        elif name:
            record.notes.append(
                f'Lead list gave "{name}" as the owner, which is a placeholder, not a name.'
            )
        if lead.absentee_owner is not None:
            record.absentee_owner = Fact.reported(
                lead.absentee_owner, SOURCE_LEAD_LIST, Confidence.MEDIUM
            )
        return record

    def _from_provider(self, lead: Lead) -> Optional[OwnerRecord]:
        if self.provider is None or not self.provider.supports(Capability.OWNER):
            if self.provider is not None:
                self.unsupported_note = (
                    f"{self.provider.name} has no ownership data, so the owner of "
                    "record has not been checked."
                )
            return None
        response = self.provider.get_owner(lead)
        self.calls += 1
        if not response.supported:
            self.unsupported_note = response.reason
            return None
        if not response.ok or not isinstance(response.data, dict):
            return None
        return self._from_payload(response.data, response.source or "provider")

    def _from_payload(self, data: Dict[str, Any], source: str) -> OwnerRecord:
        """Map a provider payload. Absent keys stay unknown."""
        record = OwnerRecord(source=source)
        name = data.get("owner_name")
        if isinstance(name, str) and name.strip() and not is_placeholder(name):
            record.owner_name = Fact.reported(name.strip(), source, Confidence.HIGH)
        mailing = data.get("owner_mailing_address")
        if isinstance(mailing, str) and mailing.strip():
            record.owner_mailing_address = Fact.reported(mailing.strip(), source, Confidence.HIGH)
        for key, attr in (
            ("ownership_years", "ownership_years"),
            ("properties_owned", "properties_owned"),
        ):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(record, attr, Fact.reported(value, source, Confidence.HIGH))
        absentee = data.get("absentee_owner")
        if isinstance(absentee, bool):
            record.absentee_owner = Fact.reported(absentee, source, Confidence.HIGH)
        return record

    @staticmethod
    def _merge(base: OwnerRecord, incoming: OwnerRecord) -> OwnerRecord:
        merged = OwnerRecord(source=incoming.source or base.source)
        for name in OwnerResearchService.SUPPORTED_FIELDS:
            setattr(merged, name, best(getattr(incoming, name), getattr(base, name)))
        merged.notes = base.notes + incoming.notes
        return merged

    def _infer(self, record: OwnerRecord, lead: Lead) -> None:
        """Fill what can be read off the facts already present.

        Two inferences, both marked derived:

        * entity ownership, from the shape of the name
        * absentee status, when the mailing address is in a different city
        """
        if record.owner_name.is_known and not record.is_entity.is_known:
            name = str(record.owner_name.value)
            entity = looks_like_entity(name)
            record.is_entity = Fact.derived(
                entity,
                f'"{name}" reads as a {"business or trust" if entity else "person"}',
                Confidence.MEDIUM if entity else Confidence.LOW,
            )
            if entity:
                kind = entity_kind(name)
                if kind:
                    record.entity_type = Fact.derived(
                        kind, f'derived from the owner name "{name}"', Confidence.MEDIUM
                    )
                record.notes.append(
                    "Entity owner: expect an authorised signer, and confirm who can bind "
                    "the entity before you paper a contract."
                )

        if not record.absentee_owner.is_known and record.owner_mailing_address.is_known:
            mailing = str(record.owner_mailing_address.value).lower()
            city = (lead.city or "").strip().lower()
            if city and city not in mailing:
                record.absentee_owner = Fact.derived(
                    True,
                    "owner's mailing address is not in the property's city",
                    Confidence.MEDIUM,
                )

        if self.unsupported_note and self.unsupported_note not in record.notes:
            record.notes.append(self.unsupported_note)
