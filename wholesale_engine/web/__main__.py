"""``python3 -m wholesale_engine.web`` — a local development server.

Loopback only, and not a production server. Deployment is a later milestone
and needs a real WSGI server plus an authentication layer in front.
"""

from __future__ import annotations

import os

from .app import run_dev_server

if __name__ == "__main__":
    run_dev_server(port=int(os.environ.get("WEB_PORT", "8000")))
