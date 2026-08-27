"""The read-only web dashboard.

A thin view over :class:`~wholesale_engine.service.EngineService`. Every route
is a GET, every route calls the service, and nothing here decides anything
about a property: scoring lives in ``analysis``, filtering in ``hunt``, queries
in ``storage``. If a number appears on a page, the service handed it over
already computed. The point of this layer is to prove the service is enough to
build a phone interface on, not to become a second engine.

**THIS APPLICATION HAS NO AUTHENTICATION AND MUST NOT BE EXPOSED TO THE PUBLIC
INTERNET.**

Anyone who can reach the port can read every lead, every owner name the
provider returned, and the whole pipeline. :func:`create_app` binds nothing by
itself; :func:`run_dev_server` binds ``127.0.0.1`` and will refuse a public
bind. The intended deployment is a private Tailscale address, where the
tailnet is the perimeter — and even then, adding an authentication layer
before anything else reaches this port is the right order of work.

    from wholesale_engine.web import create_app

    app = create_app()

Run it locally with ``python3 -m wholesale_engine.web``.
"""

from __future__ import annotations

from .app import create_app, run_dev_server

__all__ = ["create_app", "run_dev_server"]
