from __future__ import annotations

from contextlib import contextmanager
from threading import Event
from time import monotonic

import httpx
import pytest

from app.security.url_fetcher import SafeURLFetchError, SafeURLFetcher


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/plain; charset=utf-8"}
        self._chunks = chunks or [b"public travel guide"]

    def iter_bytes(self):
        yield from self._chunks


class FakeClient:
    def __init__(self, response: FakeResponse | Exception):
        self.response = response
        self.calls: list[dict[str, object]] = []

    @contextmanager
    def stream(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        yield self.response


def public_resolver(host: str, port: int) -> list[str]:
    assert port in {80, 443}
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("file:///etc/passwd", "URL_INVALID"),
        ("https://user:password@example.com/private", "URL_INVALID"),
        ("http://localhost/admin", "URL_FORBIDDEN"),
        ("http://service.local/admin", "URL_FORBIDDEN"),
        ("http://127.0.0.1/admin", "URL_FORBIDDEN"),
        ("http://10.20.30.40/admin", "URL_FORBIDDEN"),
        ("http://169.254.169.254/latest/meta-data", "URL_FORBIDDEN"),
        ("http://[::1]/admin", "URL_FORBIDDEN"),
        ("http://[fe80::1]/admin", "URL_FORBIDDEN"),
        ("http://224.0.0.1/admin", "URL_FORBIDDEN"),
        ("http://240.0.0.1/admin", "URL_FORBIDDEN"),
    ],
)
def test_rejects_unsafe_urls_before_http_request(url, code):
    client = FakeClient(FakeResponse())
    fetcher = SafeURLFetcher(resolver=public_resolver, client=client)

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch(url)

    assert raised.value.code == code
    assert client.calls == []


def test_rejects_hostname_when_any_resolved_address_is_not_public():
    client = FakeClient(FakeResponse())
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["93.184.216.34", "10.0.0.8"],
        client=client,
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/guide")

    assert raised.value.code == "URL_FORBIDDEN"
    assert client.calls == []


def test_rejects_redirect_without_following_location():
    client = FakeClient(
        FakeResponse(
            status_code=302,
            headers={"Content-Type": "text/plain", "Location": "http://127.0.0.1/admin"},
        )
    )
    fetcher = SafeURLFetcher(resolver=public_resolver, client=client)

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/guide")

    assert raised.value.code == "URL_REDIRECT_FORBIDDEN"
    assert client.calls[0]["follow_redirects"] is False


def test_rejects_unsupported_binary_content_type():
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(FakeResponse(headers={"Content-Type": "application/octet-stream"})),
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/archive")

    assert raised.value.code == "URL_CONTENT_TYPE_UNSUPPORTED"


def test_rejects_declared_content_length_before_reading_body():
    response = FakeResponse(
        headers={"Content-Type": "text/plain", "Content-Length": "11"},
        chunks=[b"not-read"],
    )
    fetcher = SafeURLFetcher(resolver=public_resolver, client=FakeClient(response), max_bytes=10)

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/large")

    assert raised.value.code == "URL_CONTENT_TOO_LARGE"


def test_rejects_actual_streamed_body_over_limit():
    response = FakeResponse(headers={"Content-Type": "text/plain"}, chunks=[b"123456", b"78901"])
    fetcher = SafeURLFetcher(resolver=public_resolver, client=FakeClient(response), max_bytes=10)

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/large")

    assert raised.value.code == "URL_CONTENT_TOO_LARGE"


def test_allows_public_https_text_and_returns_decoded_content():
    client = FakeClient(
        FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Length": "15"},
            chunks=["成都旅行指南".encode("utf-8")],
        )
    )
    fetcher = SafeURLFetcher(resolver=public_resolver, client=client)

    result = fetcher.fetch("https://example.com/guide")

    assert result.text == "成都旅行指南"
    assert result.content_type == "text/html"
    assert client.calls[0]["method"] == "GET"


def test_allows_xhtml_document_content():
    client = FakeClient(
        FakeResponse(
            headers={"Content-Type": "application/xhtml+xml"},
            chunks=[b"<html><body>guide</body></html>"],
        )
    )
    fetcher = SafeURLFetcher(resolver=public_resolver, client=client)

    result = fetcher.fetch("https://example.com/guide.xhtml")

    assert result.content_type == "application/xhtml+xml"


def test_default_document_allowlist_rejects_json():
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(FakeResponse(headers={"Content-Type": "application/json"})),
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/guide.json")

    assert raised.value.code == "URL_CONTENT_TYPE_UNSUPPORTED"


def test_per_fetch_allowlist_can_enable_rss_content_type():
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(FakeResponse(headers={"Content-Type": "application/rss+xml"})),
    )

    result = fetcher.fetch(
        "https://example.com/feed.xml",
        allowed_content_types={"application/rss+xml"},
    )

    assert result.content_type == "application/rss+xml"


def test_network_error_uses_stable_code_without_leaking_details():
    secret = "https://internal.example/secret?token=value"
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(RuntimeError(secret)),
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/guide")

    assert raised.value.code == "URL_FETCH_FAILED"
    assert secret not in str(raised.value)


def test_httpx_timeout_maps_to_url_fetch_failed():
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(httpx.ReadTimeout("private upstream timed out")),
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/guide")

    assert raised.value.code == "URL_FETCH_FAILED"
    assert "private upstream" not in str(raised.value)


def test_total_deadline_stops_slow_drip_and_closes_stream():
    class FakeClock:
        def __init__(self):
            self.now = 100.0

        def __call__(self):
            return self.now

    class SlowResponse(FakeResponse):
        def __init__(self, clock):
            super().__init__(headers={"Content-Type": "text/plain"})
            self.clock = clock

        def iter_bytes(self):
            for chunk in (b"one", b"two", b"three"):
                self.clock.now += 0.6
                yield chunk

    class ClosingClient(FakeClient):
        def __init__(self, response):
            super().__init__(response)
            self.stream_closed = False

        @contextmanager
        def stream(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            try:
                yield self.response
            finally:
                self.stream_closed = True

    clock = FakeClock()
    client = ClosingClient(SlowResponse(clock))
    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=client,
        read_timeout_seconds=5.0,
        total_timeout_seconds=1.0,
        clock=clock,
    )

    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/slow")

    assert raised.value.code == "URL_FETCH_FAILED"
    assert client.stream_closed is True


def test_request_pins_validated_ip_and_preserves_host_and_https_sni():
    client = FakeClient(FakeResponse())
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["93.184.216.34"],
        client=client,
    )

    fetcher.fetch("https://example.com:8443/guide?q=chengdu")

    call = client.calls[0]
    assert call["url"] == "https://93.184.216.34:8443/guide?q=chengdu"
    assert call["headers"]["Host"] == "example.com:8443"
    assert call["extensions"]["sni_hostname"] == "example.com"


def test_ipv6_pinned_request_uses_brackets():
    client = FakeClient(FakeResponse())
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["2606:2800:220:1:248:1893:25c8:1946"],
        client=client,
    )

    fetcher.fetch("https://example.com/guide")

    assert client.calls[0]["url"].startswith(
        "https://[2606:2800:220:1:248:1893:25c8:1946]:443/guide"
    )
    assert client.calls[0]["headers"]["Host"] == "example.com"


def _assert_hard_timeout(fetcher: SafeURLFetcher):
    started = monotonic()
    with pytest.raises(SafeURLFetchError) as raised:
        fetcher.fetch("https://example.com/blocked")
    elapsed = monotonic() - started

    assert raised.value.code == "URL_FETCH_FAILED"
    assert elapsed < 0.4


def test_hard_total_timeout_interrupts_blocking_resolver():
    blocker = Event()
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: (blocker.wait(0.5) or ["93.184.216.34"]),
        client=FakeClient(FakeResponse()),
        total_timeout_seconds=0.05,
    )

    _assert_hard_timeout(fetcher)


def test_hard_total_timeout_interrupts_blocking_stream_entry():
    blocker = Event()

    class BlockingClient(FakeClient):
        @contextmanager
        def stream(self, method, url, **kwargs):
            self.calls.append({"method": method, "url": url, **kwargs})
            blocker.wait(0.5)
            yield self.response

    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=BlockingClient(FakeResponse()),
        total_timeout_seconds=0.05,
    )

    _assert_hard_timeout(fetcher)


def test_hard_total_timeout_interrupts_blocking_stream_read():
    blocker = Event()

    class BlockingResponse(FakeResponse):
        def iter_bytes(self):
            blocker.wait(0.5)
            yield b"late"

    fetcher = SafeURLFetcher(
        resolver=public_resolver,
        client=FakeClient(BlockingResponse()),
        total_timeout_seconds=0.05,
    )

    _assert_hard_timeout(fetcher)
