"""Wholesale Acquisition Engine — V1 deal analyzer.

Screens real-estate wholesale leads from data the user supplies. It has no
access to Zillow, the MLS, county records, or skip-tracing databases, and it
never fabricates comps, ownership, liens or contact information.

Typical use::

    from wholesale_engine import analyze_property, PropertyLead

    result = analyze_property(PropertyLead(address="123 Main St", asking_price=90_000))
    print(result.decision)
"""

from .analysis import analyze_properties, analyze_property
from .config import DEFAULT_CONFIG, EngineConfig
from .models import AnalysisResult, Comp, PropertyLead

__version__ = "1.0.0"

__all__ = [
    "AnalysisResult",
    "Comp",
    "DEFAULT_CONFIG",
    "EngineConfig",
    "PropertyLead",
    "__version__",
    "analyze_properties",
    "analyze_property",
]
