"""HTTP URL checks shared by pasted-link intake and the retailer fetcher."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".test")


def public_http_url(value: str) -> bool:
    """Accept public HTTP(S) URLs only; reject credentials and internal destinations."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme.lower() not in {"http", "https"} or not host
            or parsed.username or parsed.password):
        return False
    if port is not None and port not in {80, 443}:
        return False
    host = host.rstrip(".").lower()
    if "." not in host or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return True
    return address.is_global


def resolve_public_http_url(value: str) -> bool:
    """Require every resolved address to be public before making a network request."""
    if not public_http_url(value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        try:
            address = ipaddress.ip_address(host)
            return address.is_global
        except ValueError:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    addresses = {record[4][0] for record in records}
    return bool(addresses) and all(_is_global_address(address) for address in addresses)


def _is_global_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False
