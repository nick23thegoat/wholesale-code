"""WSGI entry point for a production server.

    gunicorn --config deploy/gunicorn.conf.py deploy.wsgi:application

Importing this module builds the app once, at worker start. It reads
``WHOLESALE_DATA_DIR`` through :mod:`wholesale_engine.paths`, so the database a
worker opens is whatever the systemd environment file points at — never a path
guessed from the working directory.

The application object is deliberately plain: no host, no port, no bind. Where
it listens is the server's business, and on this deployment the answer is
loopback only, with Tailscale publishing it onto the tailnet. See deploy/README.md.
"""

from __future__ import annotations

import os

from wholesale_engine.settings import load_dotenv
from wholesale_engine.web import create_app

# systemd does not read a login shell, so nothing exports the .env for us.
# EnvironmentFile= handles the service's own variables; this covers a .env
# sitting in the checkout for the CLI's benefit. Real environment wins.
load_dotenv()

application = create_app()

# Some tooling looks for `app`. Same object, not a second one.
app = application
