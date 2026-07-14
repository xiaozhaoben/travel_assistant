from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from queue import Empty, Queue
import socket
from threading import BoundedSemaphore, Thread
from time import monotonic
from typing import Callable, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DOCUMENT_CONTENT_TYPES = frozenset({
    "text/html",
    "text/plain",
    "application/xhtml+xml",
})
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
_FETCH_WORKER_SLOTS = BoundedSemaphore(8)


class SafeURLFetcher:
    """Fetch small public text resources after validating the destination.

    All DNS answers are checked and the HTTP transport connects to one selected,
    validated IP address. The original hostname is retained only for the Host
    header and HTTPS SNI/certificate verification. Redirects are disabled.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        client: object | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        connect_timeout_seconds: float = 8.0,
        read_timeout_seconds: float = 20.0,
        total_timeout_seconds: float = 30.0,
        allowed_content_types: Iterable[str] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self.resolver = resolver or _resolve_addresses
        self.client = client
        self.max_bytes = max(1, int(max_bytes))
        self.connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self.read_timeout_seconds = max(0.1, float(read_timeout_seconds))
        self.total_timeout_seconds = max(0.001, float(total_timeout_seconds))
        self.clock = clock
        self.allowed_content_types = _normalize_content_types(
            allowed_content_types or DOCUMENT_CONTENT_TYPES
        )

    def fetch(
        self,
        url: str,
        *,
        allowed_content_types: Iterable[str] | None = None,
    ) -> SafeFetchResult:
        wall_deadline = monotonic() + self.total_timeout_seconds
        if not _FETCH_WORKER_SLOTS.acquire(timeout=self.total_timeout_seconds):
            raise _fetch_failed()
        outcome: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def run_fetch() -> None:
            try:
                outcome.put((True, self._fetch_sync(url, allowed_content_types)))
            except BaseException as exc:  # daemon worker boundary
                outcome.put((False, exc))
            finally:
                _FETCH_WORKER_SLOTS.release()

        worker = Thread(target=run_fetch, name="safe-url-fetch", daemon=True)
        try:
            worker.start()
        except Exception as exc:
            _FETCH_WORKER_SLOTS.release()
            raise _fetch_failed() from exc
        remaining = wall_deadline - monotonic()
        if remaining <= 0:
            raise _fetch_failed()
        try:
            succeeded, value = outcome.get(timeout=remaining)
        except Empty as exc:
            raise _fetch_failed() from exc
        if succeeded:
            if isinstance(value, SafeFetchResult):
                return value
            raise _fetch_failed()
        if isinstance(value, SafeURLFetchError):
            raise value
        if isinstance(value, BaseException):
            raise _fetch_failed() from value
        raise _fetch_failed()

    def _fetch_sync(
        self,
        url: str,
        allowed_content_types: Iterable[str] | None,
    ) -> SafeFetchResult:
        deadline = self.clock() + self.total_timeout_seconds
        self._check_deadline(deadline)
        parsed = _parse_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = self._validate_destination(parsed.hostname or "", port, deadline)
        pinned_url = _pinned_url(parsed, addresses[0], port)
        remaining = self._remaining(deadline)
        timeout = httpx.Timeout(
            connect=min(self.connect_timeout_seconds, remaining),
            read=min(self.read_timeout_seconds, remaining),
            write=min(self.read_timeout_seconds, remaining),
            pool=min(self.connect_timeout_seconds, remaining),
        )
        self._check_deadline(deadline)
        effective_content_types = (
            _normalize_content_types(allowed_content_types)
            if allowed_content_types is not None
            else self.allowed_content_types
        )
        if self.client is not None:
            return self._fetch_with_client(
                self.client,
                pinned_url,
                parsed,
                timeout,
                effective_content_types,
                deadline,
            )
        try:
            self._check_deadline(deadline)
            with httpx.Client() as client:
                self._check_deadline(deadline)
                return self._fetch_with_client(
                    client,
                    pinned_url,
                    parsed,
                    timeout,
                    effective_content_types,
                    deadline,
                )
        except SafeURLFetchError:
            raise
        except Exception as exc:
            raise _fetch_failed() from exc

    def _validate_destination(self, host: str, port: int, deadline: float) -> tuple[str, ...]:
        self._check_deadline(deadline)
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
                self._check_deadline(deadline)
                addresses = list(self.resolver(normalized_host, port))
                self._check_deadline(deadline)
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
        return tuple(str(address) for address in parsed_addresses)

    def _fetch_with_client(
        self,
        client: object,
        pinned_url: str,
        original_url: SplitResult,
        timeout: httpx.Timeout,
        allowed_content_types: frozenset[str],
        deadline: float,
    ) -> SafeFetchResult:
        try:
            self._check_deadline(deadline)
            with client.stream(
                "GET",
                pinned_url,
                follow_redirects=False,
                timeout=timeout,
                headers={
                    "Host": _host_header(original_url),
                    "User-Agent": "travel-assistant/1.0 (+https://localhost)",
                    "Accept": ", ".join(sorted(allowed_content_types)),
                },
                extensions={
                    "sni_hostname": _ascii_hostname(original_url.hostname or "")
                }
                if original_url.scheme.lower() == "https"
                else {},
            ) as response:
                self._check_deadline(deadline)
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
                if content_type not in allowed_content_types:
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
                iterator = iter(response.iter_bytes())
                while True:
                    self._check_deadline(deadline)
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        break
                    self._check_deadline(deadline)
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SafeURLFetchError(
                            "URL_CONTENT_TOO_LARGE",
                            413,
                            "URL content is too large",
                        )
                    chunks.append(chunk)
                self._check_deadline(deadline)
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

    def _check_deadline(self, deadline: float) -> None:
        if self.clock() >= deadline:
            raise _fetch_failed()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise _fetch_failed()
        return remaining


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


def _pinned_url(parsed: SplitResult, address: str, port: int) -> str:
    ip = ipaddress.ip_address(address)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            f"{host}:{port}",
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _host_header(parsed: SplitResult) -> str:
    hostname = _ascii_hostname(parsed.hostname or "")
    if _is_ip_literal(hostname) and ipaddress.ip_address(hostname).version == 6:
        hostname = f"[{hostname}]"
    return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname


def _ascii_hostname(hostname: str) -> str:
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SafeURLFetchError("URL_INVALID", 400, "URL is invalid") from exc


def _resolve_addresses(host: str, port: int) -> list[str]:
    return sorted(
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


def _normalize_content_types(content_types: Iterable[str]) -> frozenset[str]:
    return frozenset(str(item).strip().lower() for item in content_types if str(item).strip())


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
