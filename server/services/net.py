"""Network egress guards shared by anything that fetches a caller-supplied URL.

Lives here rather than inside one plugin because more than one consumer needs
the same check, and two copies of an SSRF guard is how one of them silently
falls behind the other.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Tuple
from urllib.parse import urlparse


def is_public_url(url: str) -> Tuple[bool, str]:
    """SSRF guard: allow only http/https to a publicly-routable host.

    Resolves every A/AAAA record and rejects loopback / private / link-local /
    reserved / multicast / unspecified addresses.

    Best-effort, not TOCTOU-proof: httpx re-resolves DNS when it connects, so a
    hostile record could change between this check and the request. Closing
    that needs connection-time pinning, which is a bigger change than the
    threat has so far justified. Returns ``(ok, reason)`` rather than raising
    so callers can phrase the refusal in their own error vocabulary.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"only http/https allowed (got {parsed.scheme or 'no scheme'!r})"
    host = parsed.hostname
    if not host:
        return False, "missing host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, f"blocked non-public address {ip}"
    return True, ""


__all__ = ["is_public_url"]
