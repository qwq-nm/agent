import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import SplitResult, urlsplit


class BlockedUrl(ValueError):
    pass


def default_resolver(host: str) -> list[str]:
    try:
        return sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(
                    host, None, type=socket.SOCK_STREAM
                )
            }
        )
    except OSError as exc:
        raise BlockedUrl(f"blocked unresolved host: {host}") from exc


class UrlGuard:
    def __init__(
        self,
        allowed_hosts: set[str],
        resolver: Callable[[str], list[str]] = default_resolver,
    ) -> None:
        self.allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
        self.resolver = resolver

    def check(self, url: str) -> SplitResult:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise BlockedUrl("blocked URL syntax or scheme")
        host = parsed.hostname.lower().rstrip(".")
        if "*" in self.allowed_hosts:
            # Explicit operator opt-out: allow every target, including
            # private/lab addresses, for local sandbox deployments.
            return parsed
        if host in self.allowed_hosts:
            return parsed
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = self.resolver(host)
        if not addresses:
            raise BlockedUrl(f"blocked unresolved host: {host}")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (
                not address.is_global
                or address.is_loopback
                or address.is_link_local
                or address.is_private
                or address.is_reserved
            ):
                raise BlockedUrl(f"blocked non-public address for {host}")
        return parsed
