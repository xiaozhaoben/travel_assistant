from __future__ import annotations

from contextlib import contextmanager

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

