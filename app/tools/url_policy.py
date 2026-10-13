from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_QUERY_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in {
        "access_token",
        "access-token",
        "auth_token",
        "auth-token",
        "api_key",
        "api-key",
        "apikey",
        "x-api-key",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "client-secret",
        "credential",
        "credentials",
        "key",
        "password",
        "passwd",
        "secret",
        "sig",
        "signature",
        "token",
        "accessToken",
        "authToken",
        "clientSecret",
        "jwt",
        "session",
        "session_id",
        "subscription-key",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
    }
}
_UNSAFE_IPV6_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_SPECIAL_USE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class URLPolicyError(ValueError):
    """A URL or resolved destination is outside the outbound fetch policy."""


class FetchLimitError(ValueError):
    """The response exceeded a configured safety limit."""


class Resolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> Sequence[str | IPAddress]: ...


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    url: str
    scheme: str
    host: str
    port: int
    literal_ip: IPAddress | None = None


@dataclass(frozen=True, slots=True)
class ApprovedTarget:
    url: str
    scheme: str
    host: str
    port: int
    address: IPAddress


@dataclass(frozen=True, slots=True)
class SafeHTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    encoding: str
    url: str

    @property
    def text(self) -> str:
        try:
            return self.body.decode(self.encoding or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class SystemResolver:
    async def resolve(self, hostname: str, port: int) -> Sequence[str | IPAddress]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(dict.fromkeys(record[4][0] for record in records))


def _legacy_ipv4_syntax(host: str) -> bool:
    """Detect non-canonical numeric IPv4 forms understood by URL clients."""
    labels = host.rstrip(".").split(".")
    if not labels:
        return False

    def is_number(label: str) -> bool:
        return bool(
            re.fullmatch(r"[0-9]+", label)
            or re.fullmatch(r"0[xX][0-9a-fA-F]+", label)
        )

    return all(is_number(label) for label in labels)


def _public_address(address: IPAddress) -> bool:
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if any(address.version == network.version and address in network for network in _SPECIAL_USE_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Mapped / transition addresses have alternate routing semantics. Do not
        # infer their safety from the outer IPv6 address.
        if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
            return False
        if any(address in network for network in _UNSAFE_IPV6_TRANSLATION_NETWORKS):
            return False
        if address.is_site_local or address.scope_id is not None:
            return False
    return True


def normalize_url(raw_url: str) -> NormalizedURL:
    if not isinstance(raw_url, str):
        raise URLPolicyError("URL must be text")
    value = raw_url.strip()
    if not value or "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise URLPolicyError("URL contains invalid characters")

    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise URLPolicyError("only http and https are allowed")
        if not parsed.netloc or "@" in parsed.netloc or "%" in parsed.netloc:
            raise URLPolicyError("ambiguous or credential-bearing authority")
        if parsed.username is not None or parsed.password is not None:
            raise URLPolicyError("URL userinfo is not allowed")
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        if isinstance(exc, URLPolicyError):
            raise
        raise URLPolicyError("invalid URL authority") from None

    if not hostname:
        raise URLPolicyError("URL hostname is missing")
    default_port = 80 if scheme == "http" else 443
    port = explicit_port if explicit_port is not None else default_port
    if port != default_port:
        raise URLPolicyError("only the standard HTTP and HTTPS ports are allowed")

    host_input = hostname
    if host_input.endswith(".."):
        raise URLPolicyError("invalid trailing dots in hostname")
    if host_input.endswith("."):
        host_input = host_input[:-1]

    literal_ip: IPAddress | None
    try:
        literal_ip = ipaddress.ip_address(host_input)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if isinstance(literal_ip, ipaddress.IPv6Address) and literal_ip.ipv4_mapped is not None:
            raise URLPolicyError("IPv4-mapped IPv6 literals are not supported")
        if not _public_address(literal_ip):
            raise URLPolicyError("destination address is not public")
        host = literal_ip.compressed
    else:
        if _legacy_ipv4_syntax(host_input):
            raise URLPolicyError("non-canonical numeric IP syntax is not allowed")
        try:
            host = host_input.casefold().encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise URLPolicyError("invalid internationalized hostname") from None
        labels = host.split(".")
        if (
            len(host) > 253
            or any(not label or len(label) > 63 for label in labels)
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels)
        ):
            raise URLPolicyError("invalid hostname")

    authority_host = f"[{host}]" if ":" in host else host
    authority = f"{authority_host}:{explicit_port}" if explicit_port is not None else authority_host
    normalized = urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))
    return NormalizedURL(normalized, scheme, host, port, literal_ip)


async def resolve_approved_target(
    url: str,
    resolver: Resolver | None = None,
) -> ApprovedTarget:
    normalized = normalize_url(url)
    if normalized.literal_ip is not None:
        addresses = (normalized.literal_ip,)
    else:
        try:
            resolved = await (resolver or SystemResolver()).resolve(normalized.host, normalized.port)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise URLPolicyError("DNS resolution failed") from None
        addresses_list: list[IPAddress] = []
        for item in resolved:
            try:
                address = item if isinstance(item, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(item)
            except ValueError:
                raise URLPolicyError("DNS returned an invalid address") from None
            if address not in addresses_list:
                addresses_list.append(address)
        addresses = tuple(addresses_list)

    if not addresses:
        raise URLPolicyError("hostname resolved to no addresses")
    if any(not _public_address(address) for address in addresses):
        raise URLPolicyError("hostname resolved to a non-public address")
    return ApprovedTarget(
        url=normalized.url,
        scheme=normalized.scheme,
        host=normalized.host,
        port=normalized.port,
        address=addresses[0],
    )


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dial only one approved IP, while httpcore retains the URL host for TLS."""

    def __init__(
        self,
        host: str,
        port: int,
        address: IPAddress,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._host = host.casefold().rstrip(".")
        self._port = port
        self._address = address
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        if host.casefold().rstrip(".") != self._host or port != self._port:
            raise httpcore.ConnectError("unexpected origin at pinned connector")
        stream = await self._backend.connect_tcp(
            str(self._address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        peer = stream.get_extra_info("server_addr")
        try:
            peer_address = ipaddress.ip_address(peer[0].split("%", 1)[0]) if peer else None
        except (IndexError, TypeError, ValueError):
            peer_address = None
        if peer_address != self._address:
            await stream.aclose()
            raise httpcore.ConnectError("connected peer did not match approved address")
        return stream

    async def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not allowed by the URL fetch policy")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def create_pinned_transport(
    target: ApprovedTarget,
    verify: ssl.SSLContext | bool = True,
) -> httpx.AsyncHTTPTransport:
    """Create a no-proxy transport using the locked HTTPX/httpcore backend seam."""
    # The peer pin is installed through a private pool seam. Fail closed when
    # either dependency changes until the connector contract is re-reviewed.
    if (
        getattr(httpx, "__version__", None) != "0.28.1"
        or getattr(httpcore, "__version__", None) != "1.0.9"
    ):
        raise RuntimeError("pinned transport requires the reviewed HTTPX/httpcore versions")
    transport = httpx.AsyncHTTPTransport(
        verify=verify,
        trust_env=False,
        http1=True,
        http2=False,
        proxy=None,
        retries=0,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("locked HTTPX/httpcore pinned transport contract is unavailable")
    pool._network_backend = PinnedAsyncNetworkBackend(target.host, target.port, target.address)
    return transport


TransportFactory = Callable[[ApprovedTarget], httpx.AsyncBaseTransport]


class SafeHTTPFetcher:
    def __init__(
        self,
        resolver: Resolver | None = None,
        transport_factory: TransportFactory = create_pinned_transport,
        max_redirects: int = 5,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._resolver = resolver
        self._transport_factory = transport_factory
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes

    async def get(self, url: str, timeout: float) -> SafeHTTPResponse:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        current = normalize_url(url).url
        seen: set[str] = set()
        async with asyncio.timeout(timeout):
            for redirect_count in range(self._max_redirects + 1):
                if current in seen:
                    raise URLPolicyError("redirect loop")
                seen.add(current)
                target = await resolve_approved_target(current, self._resolver)
                transport = self._transport_factory(target)
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(timeout),
                ) as client:
                    async with client.stream(
                        "GET",
                        target.url,
                        headers={
                            "User-Agent": "qq-deepseek-bot/0.2",
                            "Accept": "text/html,text/plain,application/json,application/vnd.github+json",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if location:
                                if redirect_count >= self._max_redirects:
                                    raise URLPolicyError("redirect limit exceeded")
                                current = normalize_url(urljoin(target.url, location)).url
                                continue

                        content_encoding = response.headers.get("content-encoding", "").strip().casefold()
                        if content_encoding not in {"", "identity"}:
                            raise FetchLimitError("compressed HTTP response is not accepted")
                        length_header = response.headers.get("content-length", "").strip()
                        if length_header.isdecimal() and int(length_header) > self._max_response_bytes:
                            raise FetchLimitError("response exceeds the configured byte limit")

                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > self._max_response_bytes:
                                raise FetchLimitError("response exceeds the configured byte limit")
                            body.extend(chunk)

                        return SafeHTTPResponse(
                            status_code=response.status_code,
                            headers={key.lower(): value for key, value in response.headers.items()},
                            body=bytes(body),
                            encoding=response.encoding or "utf-8",
                            url=target.url,
                        )

        raise URLPolicyError("redirect limit exceeded")


def redact_url(url: str) -> str:
    """Remove credentials and common secret query values from user-visible URLs."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        from urllib.parse import parse_qsl, urlencode

        query_parts = [
            (
                key,
                "REDACTED"
                if re.sub(r"[^a-z0-9]", "", key.casefold()) in _SENSITIVE_QUERY_KEYS
                else value,
            )
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query_parts), ""))
    except ValueError:
        return ""
