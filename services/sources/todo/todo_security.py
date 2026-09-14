"""Request guards for the huskeliste phone page and API.

The device's services have no login — a trusted home network is the security
model (see the README). A trusted network still gets attacked *through the
browser* of someone on it, in two ways this module closes:

* **Cross-site requests.** Any web page the owner visits can make their
  browser send requests to a private address. Browsers attach an ``Origin``
  header to those, so a request whose Origin names a host this device does not
  answer to is refused — it can neither read the list nor change it.
* **DNS rebinding.** A hostile site can point its own hostname at the device's
  IP, making the request look same-origin. The ``Host`` header still carries
  the hostile name, so only requests addressed to one of this device's own
  names or addresses are served at all.

Requests with no Origin (curl, Home Assistant, the router) pass the Origin
check; they are not browser pages, and mutations additionally require a JSON
content type that an HTML form cannot send.
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.parse

PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def local_host_names() -> set[str]:
    """Names and addresses by which this device legitimately answers."""
    names = {"localhost", "127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname().lower()
        if hostname:
            short = hostname.split(".")[0]
            names.update({hostname, short, f"{short}.local"})
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=2).stdout
        names.update(ip.lower() for ip in out.split())
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def hostname_of(host_header: str) -> str:
    """``"BeoSound5c.local.:8793"`` → ``"beosound5c.local"``, ``"[::1]:80"`` → ``"::1"``."""
    value = (host_header or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


class HostPolicy:
    # The device's addresses can change (DHCP); an unknown Host triggers a
    # re-read, but no more often than this, so junk requests can't make it spawn.
    REFRESH_AFTER_S = 60

    def __init__(self, resolver=None):
        self._resolver = resolver or local_host_names
        self.refresh()

    def refresh(self) -> None:
        self._names = {name.lower() for name in self._resolver()}
        self._refreshed = time.monotonic()

    def refresh_due(self) -> bool:
        return time.monotonic() - self._refreshed >= self.REFRESH_AFTER_S

    def knows(self, host_header: str) -> bool:
        name = hostname_of(host_header)
        return bool(name) and name in self._names

    def origin_ok(self, origin: str) -> bool:
        if not origin:
            return True
        try:
            parts = urllib.parse.urlsplit(origin)
            host = (parts.hostname or "").rstrip(".")
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False            # includes the literal "null" origin
        return bool(host) and host in self._names


def apply_security_headers(response, origin: str) -> None:
    """Headers every response gets. ``origin`` must already have passed
    :meth:`HostPolicy.origin_ok` — it is echoed back instead of a wildcard."""
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cache-Control", "no-store")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
