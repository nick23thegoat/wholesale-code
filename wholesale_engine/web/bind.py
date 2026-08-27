"""Deciding whether a bind address is safe, in one place.

The dashboard has no authentication. Whether it is private therefore comes
down entirely to what it is listening on, which makes the bind address a
security control rather than a tuning knob — and a security control with two
implementations has one implementation and one bug waiting.

Both entry points come here: the development server in :mod:`.app` and the
gunicorn configuration the systemd unit uses. Both **fail closed**. A value
that cannot be parsed is rejected rather than assumed safe, because the
failure mode of guessing wrong is every lead and owner name on the public
internet.

Loopback is decided by :mod:`ipaddress`, not by a list of strings, so the
whole of ``127.0.0.0/8`` and ``::1`` count and nothing else does.
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Tuple

#: Hostnames that resolve to loopback and are worth accepting by name.
LOOPBACK_NAMES = ("localhost", "localhost.localdomain")

#: What gunicorn does with a bare port, and the reason a bare port is refused:
#: it means "every interface", which is the one thing this must never be.
BARE_PORT_MEANS = "0.0.0.0"


class UnsafeBind(ValueError):
    """A bind address that would expose the dashboard beyond this machine."""


def split_bind(spec: str) -> Tuple[Optional[str], str]:
    """``"127.0.0.1:8000"`` -> ``("127.0.0.1", "8000")``.

    Returns ``(None, spec)`` for a unix socket, which has no host at all.
    A bare port returns the host gunicorn would actually use for it, rather
    than ``None`` — the point is to judge what will really happen.
    """
    text = (spec or "").strip()
    if not text:
        raise UnsafeBind(
            "empty bind address. Set WEB_BIND to 127.0.0.1:8000, or unset it "
            "and take the default."
        )
    if text.startswith("unix:"):
        return None, text

    if text.startswith("["):
        # [::1]:8000 — the bracketed IPv6 form.
        closing = text.find("]")
        if closing == -1:
            raise UnsafeBind(f"malformed IPv6 bind address {spec!r}")
        host = text[1:closing]
        port = text[closing + 2:] if text[closing + 1:closing + 2] == ":" else ""
        return host, port

    if ":" not in text:
        # gunicorn reads a bare "8000" as every interface. Say so plainly
        # rather than silently treating it as loopback.
        return BARE_PORT_MEANS, text

    host, _, port = text.rpartition(":")
    if not host:
        # ":8000" is also every interface.
        return BARE_PORT_MEANS, port
    if host.count(":") >= 1 and not host.startswith("["):
        # ":::8000" and other unbracketed IPv6 — treat as the wildcard it is.
        return host, port
    return host, port


def is_loopback(host: Optional[str]) -> bool:
    """True only for an address that cannot be reached from another machine.

    A name other than ``localhost`` is **not** trusted: resolving it would
    depend on DNS and on this host's own resolver, and a bind guard that can
    be moved by a hosts file is not a guard.
    """
    if host is None:
        return True  # a unix socket is not on the network at all
    candidate = host.strip().strip("[]")
    if not candidate:
        return False
    if candidate.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def validate_bind(spec: str, source: str = "WEB_BIND") -> str:
    """Return ``spec`` if it is safe to listen on; raise :class:`UnsafeBind` if not.

    Raising is the point. Gunicorn reads its configuration at startup, so a
    refusal here stops the service rather than starting one that is quietly
    reachable from the internet — the unit fails, systemd reports it, and
    ``journalctl`` says why.
    """
    host, _ = split_bind(spec)
    if is_loopback(host):
        return spec

    shown = host if host is not None else spec
    meaning = ""
    if shown == BARE_PORT_MEANS and ":" not in (spec or ""):
        meaning = (
            f" A bare port means {BARE_PORT_MEANS}, which is every interface "
            "on this machine."
        )
    raise UnsafeBind(
        f"refusing to bind {spec!r} ({source}): {shown} is not a loopback "
        f"address.{meaning}\n\n"
        "This dashboard has NO AUTHENTICATION. Anything other than loopback "
        "hands every lead, owner name and mailing address to whoever can "
        "reach the port.\n\n"
        "To read it from your phone, leave the bind at 127.0.0.1:8000 and let "
        "Tailscale carry it:\n"
        "    sudo tailscale serve --bg 8000\n"
        "Use `serve`, not `funnel` — funnel publishes to the public internet."
    )
