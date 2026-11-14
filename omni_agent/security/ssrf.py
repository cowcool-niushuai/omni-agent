"""SSRF protection for ``web_fetch``.

Two stages are used:

``validate_url_static`` runs without network I/O and is safe to call from the
permission classifier. ``resolve_and_validate`` performs DNS resolution and
rejects hostnames that resolve to loopback, private, link-local, multicast,
reserved or cloud-metadata addresses.

The caller must additionally verify the address of the socket actually used
for the request (see ``tools.tool_web_fetch``), which closes the DNS-rebinding
window between resolution and connection.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_ALLOWED_PORTS = {80, 443, 8080, 8443}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


class UnsafeURLError(ValueError):
    """Raised when a URL could reach a non-public network target."""


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int


def _normalise_host(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith("."):
        host = host[:-1]
    return host


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False

    # CGNAT and benchmarking ranges are not always covered by is_private.
    if isinstance(address, ipaddress.IPv4Address):
        blocked_networks = (
            ipaddress.ip_network("100.64.0.0/10"),
            ipaddress.ip_network("198.18.0.0/15"),
            ipaddress.ip_network("192.0.0.0/24"),
            ipaddress.ip_network("240.0.0.0/4"),
        )
        if any(address in net for net in blocked_networks):
            return False
    return True


def is_public_ip(value: str) -> bool:
    """Public helper used by the HTTP client to verify the peer address."""
    return _is_public_ip(value)


def validate_url_static(
    url: str,
    *,
    allowed_ports: set[int] | None = None,
    allow_private: bool = False,
) -> ValidatedURL:
    """Validate URL shape and literal addresses without DNS resolution."""
    raw = (url or "").strip()
    if not raw:
        raise UnsafeURLError("URL 为空")
    if any(char in raw for char in ("\x00", "\n", "\r", "\t")):
        raise UnsafeURLError("URL 包含非法控制字符")
    if "\\" in raw:
        raise UnsafeURLError("URL 不允许包含反斜杠")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise UnsafeURLError(f"URL 解析失败: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("仅支持 http/https 协议")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL 不允许包含用户名或密码")
    host = _normalise_host(parsed.hostname or "")
    if not host:
        raise UnsafeURLError("URL 缺少主机名")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL 端口非法") from exc
    ports = allowed_ports or DEFAULT_ALLOWED_PORTS
    if port not in ports:
        raise UnsafeURLError(f"端口 {port} 不在允许列表 {sorted(ports)} 中")

    if host in BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"禁止访问本地/元数据主机: {host}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not allow_private and not _is_public_ip(str(literal)):
        raise UnsafeURLError(f"禁止访问非公网地址: {host}")

    return ValidatedURL(url=raw, scheme=scheme, host=host, port=port)


def resolve_and_validate(
    url: ValidatedURL,
    *,
    allow_private: bool = False,
) -> list[str]:
    """Resolve the hostname and require every address to be public."""
    if allow_private:
        return []
    try:
        infos = socket.getaddrinfo(url.host, url.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"域名解析失败: {exc}") from exc

    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeURLError("域名未解析到任何地址")
    for address in addresses:
        if not _is_public_ip(address):
            raise UnsafeURLError(f"域名解析到非公网地址，已阻止: {url.host} -> {address}")
    return addresses
