"""Controlled vocabularies used across the engine.

Every enum has an ``UNKNOWN`` member. Missing information is a first-class
state in this system: the engine must be able to say "I do not know" rather
than silently substituting a default that would flatter a bad deal.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class _ParsableEnum(Enum):
    """Enum with a forgiving parser for messy CSV / human input."""

    @classmethod
    def parse(cls, raw: Optional[str]) -> "_ParsableEnum":
        if raw is None:
            return cls.UNKNOWN  # type: ignore[attr-defined]
        text = str(raw).strip().lower().replace("-", " ").replace("_", " ")
        if not text:
            return cls.UNKNOWN  # type: ignore[attr-defined]
        for member in cls:
            if text == member.value.replace("_", " "):
                return member
        for token, member in cls._aliases().items():
            if token in text:
                return member
        return cls.UNKNOWN  # type: ignore[attr-defined]

    @classmethod
    def _aliases(cls) -> dict:
        return {}

    def __str__(self) -> str:
        return self.value.replace("_", " ").upper()


class Condition(_ParsableEnum):
    """Seller-reported / inspector-reported physical condition."""

    TURNKEY = "turnkey"
    COSMETIC = "cosmetic"
    MODERATE = "moderate"
    HEAVY = "heavy"
    TEARDOWN = "teardown"
    UNKNOWN = "unknown"

    @classmethod
    def _aliases(cls) -> dict:
        return {
            "rent ready": cls.TURNKEY,
            "move in": cls.TURNKEY,
            "updated": cls.TURNKEY,
            "excellent": cls.TURNKEY,
            "good": cls.COSMETIC,
            "light": cls.COSMETIC,
            "paint": cls.COSMETIC,
            "carpet": cls.COSMETIC,
            "dated": cls.MODERATE,
            "average": cls.MODERATE,
            "fair": cls.MODERATE,
            "needs work": cls.MODERATE,
            "poor": cls.HEAVY,
            "major": cls.HEAVY,
            "gut": cls.HEAVY,
            "rehab": cls.HEAVY,
            "fire": cls.TEARDOWN,
            "condemn": cls.TEARDOWN,
            "shell": cls.TEARDOWN,
            "tear down": cls.TEARDOWN,
        }


class Occupancy(_ParsableEnum):
    VACANT = "vacant"
    OWNER_OCCUPIED = "owner_occupied"
    TENANT_OCCUPIED = "tenant_occupied"
    UNKNOWN = "unknown"

    @classmethod
    def _aliases(cls) -> dict:
        return {
            "vacant": cls.VACANT,
            "empty": cls.VACANT,
            "owner": cls.OWNER_OCCUPIED,
            "tenant": cls.TENANT_OCCUPIED,
            "renter": cls.TENANT_OCCUPIED,
            "leased": cls.TENANT_OCCUPIED,
            "squatter": cls.UNKNOWN,
        }


class PropertyType(_ParsableEnum):
    SINGLE_FAMILY = "single_family"
    TOWNHOUSE = "townhouse"
    CONDO = "condo"
    DUPLEX = "duplex"
    TRIPLEX = "triplex"
    FOURPLEX = "fourplex"
    MULTI_FAMILY = "multi_family"
    MOBILE = "mobile"
    LAND = "land"
    COMMERCIAL = "commercial"
    UNKNOWN = "unknown"

    @classmethod
    def _aliases(cls) -> dict:
        return {
            "single family": cls.SINGLE_FAMILY,
            "sfr": cls.SINGLE_FAMILY,
            "sfh": cls.SINGLE_FAMILY,
            "house": cls.SINGLE_FAMILY,
            "town": cls.TOWNHOUSE,
            "condo": cls.CONDO,
            "duplex": cls.DUPLEX,
            "triplex": cls.TRIPLEX,
            "3 plex": cls.TRIPLEX,
            "fourplex": cls.FOURPLEX,
            "4 plex": cls.FOURPLEX,
            "quad": cls.FOURPLEX,
            "multi": cls.MULTI_FAMILY,
            "apartment": cls.MULTI_FAMILY,
            "commercial": cls.COMMERCIAL,
            "retail": cls.COMMERCIAL,
            "office": cls.COMMERCIAL,
            "warehouse": cls.COMMERCIAL,
            "mobile": cls.MOBILE,
            "manufactured": cls.MOBILE,
            "trailer": cls.MOBILE,
            "lot": cls.LAND,
            "land": cls.LAND,
            "vacant lot": cls.LAND,
        }


class SaleStatus(_ParsableEnum):
    """Status of a comparable sale. Closed sales carry the most weight."""

    CLOSED = "closed"
    PENDING = "pending"
    ACTIVE = "active"
    UNKNOWN = "unknown"

    @classmethod
    def _aliases(cls) -> dict:
        return {
            "sold": cls.CLOSED,
            "closed": cls.CLOSED,
            "settled": cls.CLOSED,
            "pending": cls.PENDING,
            "under contract": cls.PENDING,
            "contingent": cls.PENDING,
            "active": cls.ACTIVE,
            "listed": cls.ACTIVE,
            "for sale": cls.ACTIVE,
        }


class SellerMotivation(_ParsableEnum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    UNKNOWN = "unknown"

    @classmethod
    def _aliases(cls) -> dict:
        return {
            "very motivated": cls.HIGH,
            "highly motivated": cls.HIGH,
            "must sell": cls.HIGH,
            "urgent": cls.HIGH,
            "asap": cls.HIGH,
            "high": cls.HIGH,
            "somewhat": cls.MODERATE,
            "moderate": cls.MODERATE,
            "medium": cls.MODERATE,
            "testing the market": cls.LOW,
            "not motivated": cls.LOW,
            "firm": cls.LOW,
            "low": cls.LOW,
        }


class ARVConfidence(Enum):
    """How much the engine trusts the after-repair value it is using."""

    VERIFIED_SUPPORTED = "VERIFIED/SUPPORTED ARV"
    ESTIMATED = "ESTIMATED ARV"
    USER_PROVIDED = "USER-PROVIDED ARV (UNVERIFIED)"
    INSUFFICIENT_DATA = "INSUFFICIENT DATA"

    def __str__(self) -> str:
        return self.value


class CompConfidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

    def __str__(self) -> str:
        return self.value


class RepairConfidence(Enum):
    USER_PROVIDED = "USER-PROVIDED (not a contractor quote)"
    CONDITION_BASED = "CONDITION-BASED ESTIMATE (not a contractor quote)"
    INSUFFICIENT_DATA = "INSUFFICIENT DATA"

    def __str__(self) -> str:
        return self.value


class Severity(Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    def __str__(self) -> str:
        return self.value


class Classification(Enum):
    HOT = "🔥 HOT"
    STRONG = "🟠 STRONG"
    POSSIBLE = "🟡 POSSIBLE"
    WEAK = "🔵 WEAK"
    PASS = "❌ PASS"

    def __str__(self) -> str:
        return self.value


class Decision(Enum):
    GO = "🔥 GO"
    NEGOTIATE = "🟠 NEGOTIATE"
    NEED_MORE_DATA = "🟡 NEED MORE DATA"
    PASS = "❌ PASS"

    def __str__(self) -> str:
        return self.value
