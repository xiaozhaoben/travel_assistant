from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

import httpx


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "text/html",
    "application/json",
    "application/xml",
    "text/xml",
    "text/markdown",
    "text/x-markdown",
    "application/markdown",
    "application/x-markdown",
}
LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
)


class SafeURLFetchError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


@dataclass(frozen=True)
class SafeFetchResult:
    text: str
    content: bytes
    content_type: str


Resolver = Callable[[str, int], Iterable[str]]


class SafeURLFetcher:
    """Fetch small public text resources after validating the destination.

    All DNS answers are checked immediately before the request and redirects are
    disabled. A DNS answer can still change between validation and the client's
    connection (TOCTOU rebinding). Production deployments should additionally
    enforce egress filtering or use a transport that pins the validated address.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        client: object | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        connect_timeout_seconds: float = 8.0,
        read_timeout_seconds: float = 20.0,
    ):
        self.resolver = resolver or _resolve_addresses
        self.client = client
        self.max_bytes = max(1, int(max_bytes))
        self.connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self.read_timeout_seconds = max(0.1, float(read_timeout_seconds))

    def fetch(self, url: str) -> SafeFetchResult:
        parsed = _parse_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._validate_destination(parsed.hostname or "", port)
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.read_timeout_seconds,
            pool=self.connect_timeout_seconds,
        )
        if self.client is not None:
            return self._fetch_with_client(self.client, url, timeout)
        try:
            with httpx.Client() as client:
                return self._fetch_with_client(client, url, timeout)
        except SafeURLFetchError:
            raise
        except Exception as exc:
            raise _fetch_failed() from exc

    def _validate_destination(self, host: str, port: int) -> None:
        normalized_host = host.rstrip(".").lower()
        if (
            normalized_host == "localhost"
            or not normalized_host
            or "." not in normalized_host and not _is_ip_literal(normalized_host)
            or any(normalized_host.endswith(suffix) for suffix in LOCAL_HOST_SUFFIXES)
        ):
            raise SafeURLFetchError("URL_FORBIDDEN", 403, "URL destination is not allowed")
        if _is_ip_literal(normalized_host):
            addresses = [normalized_host]
        else:
            try:
                addresses = list(self.resolver(normalized_host, port))
            except SafeURLFetchError:
                raise
            except Exception as exc:
                raise _fetch_failed() from exc
        if not addresses:
            raise _fetch_failed()
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise _fetch_failed() from exc
        if any(not _is_public_address(address) for address in parsed_addresses):
            raise SafeURLFetchError("URL_FORBIDDEN", 403, "URL destination is not allowed")

    def _fetch_with_client(self, client: object, url: str, timeout: httpx.Timeout) -> SafeFetchResult:
        try:
            with client.stream(
                "GET",
                url,
                follow_redirects=False,
                timeout=timeout,
                headers={
                    "User-Agent": "travel-assistant/1.0 (+https://localhost)",
                    "Accept": "text/html, text/plain, application/json, application/xml, text/xml, text/markdown",
                },
            ) as response:
                status_code = int(response.status_code)
                if 300 <= status_code < 400:
                    raise SafeURLFetchError(
                        "URL_REDIRECT_FORBIDDEN",
                        400,
                        "URL redirects are not allowed",
                    )
                if status_code < 200 or status_code >= 300:
                    raise _fetch_failed()
                content_type = _content_type(response.headers)
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise SafeURLFetchError(
                        "URL_CONTENT_TYPE_UNSUPPORTED",
                        415,
                        "URL content type is not supported",
                    )
                declared_length = _content_length(response.headers)
                if declared_length is not None and declared_length > self.max_bytes:
                    raise SafeURLFetchError(
                        "URL_CONTENT_TOO_LARGE",
                        413,
                        "URL content is too large",
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SafeURLFetchError(
                            "URL_CONTENT_TOO_LARGE",
                            413,
                            "URL content is too large",
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                return SafeFetchResult(
                    text=content.decode(_charset(response.headers), errors="replace"),
                    content=content,
                    content_type=content_type,
                )
        except SafeURLFetchError:
            raise
        except Exception as exc:
            raise _fetch_failed() from exc


def _parse_url(url: str):
    try:
        parsed = urlsplit(str(url).strip())
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise SafeURLFetchError("URL_INVALID", 400, "URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SafeURLFetchError("URL_INVALID", 400, "URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise SafeURLFetchError("URL_INVALID", 400, "URL is invalid")
    return parsed


def _resolve_addresses(host: str, port: int) -> list[str]:
    return list(
        {
            record[4][0]
            for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_private
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _content_type(headers: object) -> str:
    value = str(headers.get("Content-Type", ""))
    return value.split(";", 1)[0].strip().lower()


def _content_length(headers: object) -> int | None:
    value = str(headers.get("Content-Length", "")).strip()
    if not value:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return max(0, length)


def _charset(headers: object) -> str:
    value = str(headers.get("Content-Type", ""))
    for item in value.split(";")[1:]:
        key, separator, encoding = item.partition("=")
        if separator and key.strip().lower() == "charset" and encoding.strip():
            return encoding.strip().strip('"')
    return "utf-8"


def _fetch_failed() -> SafeURLFetchError:
    return SafeURLFetchError("URL_FETCH_FAILED", 502, "URL could not be fetched")
