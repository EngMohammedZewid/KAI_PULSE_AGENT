"""Downloads of URLs that come from the model or the user.

The model decides which URLs end up in a report (images), an email (file_url)
or fetch_text_from_url, so a crafted prompt could point the server at
internal addresses: cloud metadata (169.254.169.254), localhost services,
the database, other machines on the private network. Every such download
goes through fetch(), which:

- allows only http and https;
- resolves the host and refuses private, loopback, link-local, reserved and
  other non-public addresses, on the first request and on every redirect;
- with trusted_only=True, also requires a known source host: QuickChart,
  AWS S3, IBM COS, plus any in KAI_FETCH_ALLOWED_HOSTS (comma-separated;
  "example.com" also allows its subdomains);
- stops reading after max_bytes.

Resolution happens before the connection, so a DNS answer that changes in
between (rebinding) is not covered; the host list for trusted downloads
limits that case to known providers.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TRUSTED_HOSTS = ("quickchart.io", "amazonaws.com", "cloud-object-storage.appdomain.cloud")
MAX_BYTES = 25 * 1024 * 1024


class UnsafeURL(ValueError):
    """The URL points somewhere the server must not fetch."""


def trusted_hosts() -> tuple[str, ...]:
    extra = os.environ.get("KAI_FETCH_ALLOWED_HOSTS", "")
    hosts = [h.strip().lower().lstrip(".") for h in extra.split(",") if h.strip()]
    return DEFAULT_TRUSTED_HOSTS + tuple(hosts)


def _host_trusted(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in trusted_hosts())


def check_url(url: str, trusted_only: bool = False) -> None:
    """Raise UnsafeURL unless the URL may be fetched."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"only http and https links can be fetched, not '{parts.scheme or url[:20]}'")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("the link has no host")
    if trusted_only and not _host_trusted(host):
        raise UnsafeURL(f"{host} is not an allowed source (allowed: {', '.join(trusted_hosts())})")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as e:
        raise UnsafeURL(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"{host} points to a non-public address ({ip})")


class _CheckRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, trusted_only: bool) -> None:
        self.trusted_only = trusted_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.trusted_only)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, trusted_only: bool = False, timeout: float = 30,
          max_bytes: int = MAX_BYTES, user_agent: str = "KAI-Pulse/1.0") -> tuple[bytes, str]:
    """Download url after the checks above. Returns (body, content type)."""
    url = (url or "").strip()
    check_url(url, trusted_only)
    opener = urllib.request.build_opener(_CheckRedirects(trusted_only))
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read(max_bytes + 1)
        ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    if len(body) > max_bytes:
        raise UnsafeURL(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
    return body, ctype
