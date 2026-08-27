"""The service layer: the engine's operations, callable by anything.

The CLI, a Flask route and a scheduled job all want the same six or seven
things — run a hunt, read the stored leads, open one property, read and write
the buy box, look at run history and at why properties were rejected. Before
this package those sequences existed only inside ``main.py``, wrapped around
``argparse.Namespace``, so anything that was not the CLI had to either
fabricate a Namespace or write a second copy of the logic.

:class:`EngineService` is that logic with the CLI taken off. It holds no deal
math of its own: analysis stays in :mod:`wholesale_engine.analysis`, filtering
in :mod:`wholesale_engine.hunt`, queries in
:mod:`wholesale_engine.storage.database`. This layer picks inputs, manages
database and provider lifecycles, and reports outcomes.

    from wholesale_engine.service import EngineService, HuntRequest

    service = EngineService()
    outcome = service.run_hunt(HuntRequest(source="csv"))
    if outcome.ok:
        for entry in outcome.leads:
            ...

Nothing here prints. Pass ``on_notice`` to receive progress as it happens.

This package is deliberately **not** imported by ``wholesale_engine/__init__``:
importing the library to analyze one property should not pull in the storage
and provider stack.
"""

from __future__ import annotations

from .engine import EngineService, Notice, resolve_price_band
from .models import (
    BuyBoxView,
    HuntOutcome,
    HuntRequest,
    ProviderChoice,
    ProviderStatus,
    SaveResult,
)
from .paths import (
    DEFAULT_HOT_OUTPUT,
    DEFAULT_LEAD_OUTPUT,
    DEFAULT_OUTPUT,
    DEFAULT_OUTPUT_DIR,
    PACKAGE_ROOT,
    SAMPLE_COMPS,
    SAMPLE_LEAD_COMPS,
    SAMPLE_LEADS,
    SAMPLE_PROPERTIES,
)

__all__ = [
    "BuyBoxView",
    "DEFAULT_HOT_OUTPUT",
    "DEFAULT_LEAD_OUTPUT",
    "DEFAULT_OUTPUT",
    "DEFAULT_OUTPUT_DIR",
    "EngineService",
    "HuntOutcome",
    "HuntRequest",
    "Notice",
    "PACKAGE_ROOT",
    "ProviderChoice",
    "ProviderStatus",
    "SAMPLE_COMPS",
    "SAMPLE_LEADS",
    "SAMPLE_LEAD_COMPS",
    "SAMPLE_PROPERTIES",
    "SaveResult",
    "resolve_price_band",
]
