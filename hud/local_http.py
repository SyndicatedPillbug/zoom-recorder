#!/usr/bin/env python3
"""Safe loopback HTTP binding shared by the HUD's local web surfaces."""

from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Tuple, Type


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def host_from_header(value: str) -> str:
    """Return a canonical host from an HTTP Host header."""
    raw = (value or "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else raw
    if raw.count(":") > 1:
        return raw
    return raw.split(":", 1)[0]


def url_host(host: str) -> str:
    """Format an address for an HTTP URL."""
    return "[{}]".format(host) if ":" in host and not host.startswith("[") else host


def bind_local_server(handler: Type[BaseHTTPRequestHandler], host: str, port: int
                      ) -> Tuple[ThreadingHTTPServer, str]:
    """Bind loopback, trying both address families when appropriate.

    A restricted runner or a managed macOS environment may deny one address
    family even though the other is usable. If neither can bind, retain the
    original error class but include the attempted addresses and a useful
    diagnosis instead of exposing a bare errno.
    """
    preferred = host.strip().lower().strip("[]") or "127.0.0.1"
    candidates = [preferred]
    if preferred in ("127.0.0.1", "localhost"):
        candidates.extend(["::1"])
    elif preferred == "::1":
        candidates.append("127.0.0.1")

    errors = []
    for candidate in candidates:
        server_type: Type[ThreadingHTTPServer] = (
            IPv6ThreadingHTTPServer if ":" in candidate else ThreadingHTTPServer)
        try:
            return server_type((candidate, port), handler), candidate
        except OSError as exc:
            errors.append("{}: {}".format(candidate, exc))

    detail = "; ".join(errors) or "no address was attempted"
    raise PermissionError(
        "could not bind the local HUD server; loopback permission or network "
        "policy denied all candidates ({}). Run this app in a normal logged-in "
        "user session and check local network/security tooling.".format(detail))
