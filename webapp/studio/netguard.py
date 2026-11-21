"""SSRF-safe outbound fetch for Studio data sources.

Defences (all enforced at CONNECT time, not just on the URL string):
  * only http/https, ports 80/443 (+ explicit allow-list), no userinfo in URL
  * hostname is resolved ONCE; every returned address must be public; the socket
    then connects to that exact validated IP (defeats DNS rebinding / TOCTOU)
  * redirects are followed manually (max 3) and each hop is re-validated
  * response size and total time are bounded
Blocked ranges include loopback, RFC1918, link-local (cloud metadata
169.254.169.254), CGNAT, ULA/link-local IPv6, multicast, unspecified,
IPv4-mapped IPv6, and anything else Python's ipaddress marks non-global.
"""
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

ALLOWED_PORTS = (80, 443)
MAX_BYTES = 256 * 1024
MAX_REDIRECTS = 3
TIMEOUT_S = 8


class BlockedError(Exception):
    pass


def ip_is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT is not "global" but be explicit
        return False
    return a.is_global and not a.is_multicast


def validate_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedError("only http/https URLs are allowed")
    if parts.username or parts.password:
        raise BlockedError("credentials in URLs are not allowed")
    if not parts.hostname:
        raise BlockedError("missing host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedError("port not allowed")
    return parts, port


def resolve_public(host, port):
    """Resolve once; return public IPs only. Raises if ANY answer is non-public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise BlockedError("host did not resolve")
    ips = []
    for info in infos:
        ip = info[4][0]
        if not ip_is_public(ip):
            raise BlockedError("host resolves to a non-public address")
        ips.append(ip)
    if not ips:
        raise BlockedError("host did not resolve")
    return ips


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # verifies cert vs hostname


def fetch(url, max_bytes=MAX_BYTES, timeout=TIMEOUT_S):
    """GET `url` safely; returns bytes. Raises BlockedError / OSError."""
    deadline = time.monotonic() + timeout
    for _hop in range(MAX_REDIRECTS + 1):
        parts, port = validate_url(url)
        ip = resolve_public(parts.hostname, port)[0]
        cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        conn = cls(parts.hostname, ip, port, max(1.0, deadline - time.monotonic()))
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request("GET", path, headers={"Host": parts.netloc, "User-Agent": "BlueStream-Studio/1",
                                               "Accept": "application/json, text/plain"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise BlockedError("redirect without location")
                url = urljoin(url, loc)
                continue
            if resp.status != 200:
                raise BlockedError("unexpected HTTP status %d" % resp.status)
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise BlockedError("response too large")
            return body
        finally:
            conn.close()
    raise BlockedError("too many redirects")


def extract(doc, path):
    """Dotted/indexed JSON path ("data.items.0.count") -> str, or raise KeyError."""
    cur = doc
    for part in [p for p in path.split(".") if p != ""]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    if isinstance(cur, (dict, list)):
        raise KeyError("path does not point at a scalar")
    return str(cur)


def fetch_value(url, path):
    return extract(json.loads(fetch(url).decode("utf-8", "replace")), path)
