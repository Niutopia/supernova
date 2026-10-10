"""Reject browser requests that did not come from this local workbench.

The Web UI has no login: it is meant to be reachable only from this machine.
Binding to 127.0.0.1 alone does not stop another website open in the same
browser from reaching it, either by DNS rebinding (a foreign hostname that
resolves to 127.0.0.1) or by a cross-site write.  Two checks close that:

* the ``Host`` header must name an allowed host, which defeats rebinding;
* a state-changing request that carries ``Origin`` must come from that same
  allowed host, which defeats cross-site writes.

Deployments reached through another name (a reverse proxy or LAN address)
list it in ``SUPERNOVA_ALLOWED_HOSTS``, comma separated.
"""

import os
from typing import Iterable
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(value: str) -> str:
    """Return the lower-cased host part of a ``host[:port]`` value."""

    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def allowed_hosts(extra: str | Iterable[str] | None = None) -> frozenset[str]:
    if extra is None:
        extra = os.environ.get("SUPERNOVA_ALLOWED_HOSTS", "")
    if isinstance(extra, str):
        extra = extra.split(",")
    configured = {_hostname(item) for item in extra if item and item.strip()}
    return DEFAULT_ALLOWED_HOSTS | configured


def install_request_guard(app: Flask) -> None:
    hosts = allowed_hosts(app.config.get("ALLOWED_HOSTS"))

    def _forbidden():
        return jsonify(
            status=False,
            msg="Request origin is not allowed",
            code="origin_forbidden",
        ), 403

    @app.before_request
    def reject_foreign_requests():
        if _hostname(request.host or "") not in hosts:
            return _forbidden()
        if request.method in _SAFE_METHODS:
            return None
        origin = request.headers.get("Origin")
        if origin is None:
            # Non-browser clients (curl, scripts) send no Origin header.
            return None
        if origin == "null" or _hostname(urlsplit(origin).netloc) not in hosts:
            return _forbidden()
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response
