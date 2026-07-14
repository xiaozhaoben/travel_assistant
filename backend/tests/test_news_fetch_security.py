from __future__ import annotations

from contextlib import contextmanager
import sys

import pytest

from app.knowledge.news_agent import TravelNewsIngestionAgent
from app.security.url_fetcher import SafeURLFetcher


class FakeResponse:
    def __init__(self, content_type: str, body: bytes):
        self.status_code = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.body = body

    def iter_bytes(self):
        yield self.body


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    @contextmanager
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield self.response


class FakeStore:
    def __init__(self):
        self.items = []

    def add_text(self, **kwargs):
        self.items.append(kwargs)
        return 1


RSS_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Travel</title><link>https://example.com</link>
<description>Travel updates</description><item><title>Chengdu guide</title>
<link>https://example.com/chengdu</link><description>Visit the panda base.</description>
</item></channel></rss>"""

ATOM_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Travel</title>
<entry><title>Nanjing guide</title><id>guide-1</id>
<link href="https://example.com/nanjing"/><summary>Book museums early.</summary>
</entry></feed>"""


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("application/rss+xml", RSS_BODY), ("application/atom+xml", ATOM_BODY)],
)
def test_news_agent_fetches_rss_and_atom_through_safe_fetcher(content_type, body):
    client = FakeClient(FakeResponse(content_type, body))
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["93.184.216.34"],
        client=client,
    )
    store = FakeStore()

    result = TravelNewsIngestionAgent(store, fetcher).fetch_travel_feeds(
        ["https://feeds.example.com/travel"]
    )

    assert result["total_seen"] == 1
    assert result["total_added"] == 1
    assert result["errors"] == []
    assert len(client.calls) == 1


def test_news_agent_rejects_private_destination_before_http_call():
    client = FakeClient(FakeResponse("application/rss+xml", RSS_BODY))
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["10.0.0.5"],
        client=client,
    )

    result = TravelNewsIngestionAgent(FakeStore(), fetcher).fetch_travel_feeds(
        ["https://feeds.example.com/internal"]
    )

    assert result["errors"] == ["NEWS_FEED_FETCH_FAILED"]
    assert client.calls == []


def test_news_agent_returns_stable_parse_error_without_parser_details():
    secret = "internal parser detail"
    client = FakeClient(FakeResponse("application/rss+xml", secret.encode()))
    fetcher = SafeURLFetcher(
        resolver=lambda _host, _port: ["93.184.216.34"],
        client=client,
    )

    result = TravelNewsIngestionAgent(FakeStore(), fetcher).fetch_travel_feeds(
        ["https://feeds.example.com/broken"]
    )

    assert result["errors"] == ["NEWS_FEED_PARSE_FAILED"]
    assert secret not in repr(result)


def test_news_agent_returns_stable_code_when_feedparser_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "feedparser", None)

    result = TravelNewsIngestionAgent(FakeStore()).fetch_travel_feeds(
        ["https://feeds.example.com/travel"]
    )

    assert result["errors"] == ["NEWS_PARSER_UNAVAILABLE"]

