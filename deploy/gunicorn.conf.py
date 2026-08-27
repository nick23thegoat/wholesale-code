"""Gunicorn settings for the dashboard.

Two things here are load-bearing.

**bind is loopback.** Not 0.0.0.0, not the Tailscale address. The dashboard has
no authentication, so the only binding that is safe by construction rather than
by configuration is one the kernel will not accept an off-box connection to.
Tailscale publishes it onto the tailnet with `tailscale serve`; a firewall rule
is then defence in depth rather than the only thing standing between owner
records and the internet.

**Threads, not many workers.** The dashboard is one person on a phone reading
SQLite. EngineService opens and closes a connection per request, so threads are
safe; a large worker count would just multiply idle processes on a small VPS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# gunicorn execs this file before the application is importable, so put the
# checkout on the path ourselves rather than relying on the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wholesale_engine.web.bind import validate_bind  # noqa: E402

#: Loopback only. Changing this is a security decision, not a tuning one.
#:
#: WEB_BIND is honoured so a unix socket or a different port stays possible,
#: but it is VALIDATED and fails closed: a non-loopback value raises here,
#: gunicorn never starts, and systemd reports why. An environment file that
#: says 0.0.0.0 must not quietly put owner records on the internet.
bind = validate_bind(os.environ.get("WEB_BIND", "127.0.0.1:8000"))

workers = int(os.environ.get("WEB_WORKERS", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))
worker_class = "gthread"

#: A phone on a mobile connection is slow; a read-only page is not.
timeout = 30
graceful_timeout = 30
keepalive = 5

#: journald captures stdout/stderr, so log there and let systemd handle the rest.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("WEB_LOG_LEVEL", "info")
#: No query strings in the access log: the lead filters carry addresses.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'

proc_name = "wholesale-web"
preload_app = False
