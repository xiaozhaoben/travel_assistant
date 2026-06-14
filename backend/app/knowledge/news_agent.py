from __future__ import annotations

import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlparse

from dotenv import load_dotenv

from app.core.config import ENV_PATH
from app.knowledge.vector_store import PostgresTravelVectorStore, normalize_text

logger = logging.getLogger(__name__)
load_dotenv(ENV_PATH, override=False)


DEFAULT_RSSHUB_BASE_URL = "https://rsshub.app"
DEFAULT_FEED_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ENTRIES_PER_FEED = 5

DEFAULT_RSSHUB_ROUTES = [
    # ── 官方文旅公告 ──────────────────────────────────────
    "/mct/gkml",                          # 文化和旅游部 官方公告/政策
    "/ncha/gkml",                         # 国家文物局 博物馆开放/文保信息
    # ── 旅行攻略 & 目的地指南 ─────────────────────────────
    "/mafengwo/hot",                      # 马蜂窝 热门目的地/游记
    "/qyer/city",                         # 穷游网 城市攻略
    "/ctrip/picks/tours",                 # 携程 精选旅游线路
    # ── 旅行媒体 ──────────────────────────────────────────
    "/chinanews/ly",                      # 中新网 旅游频道
    # ── 国际开放指南（中文） ───────────────────────────────
    "https://zh.wikivoyage.org/w/api.php?action=feedrecentchanges&feedformat=atom",
]


def configured_travel_feeds() -> list[str]:
    raw = os.getenv("TRAVEL_FEEDS", "")
    if raw.strip():
        return [feed_url(item.strip()) for item in raw.split(",") if item.strip()]
    return [feed_url(route) for route in DEFAULT_RSSHUB_ROUTES]


def rsshub_url(route: str) -> str:
    base_url = os.getenv("RSSHUB_BASE_URL", DEFAULT_RSSHUB_BASE_URL).strip().rstrip("/")
    return f"{base_url}/{route.lstrip('/')}"


def feed_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return value
    return rsshub_url(value)


travel_feeds = configured_travel_feeds()


class TravelNewsIngestionAgent:
    name = "TravelNewsIngestionAgent"

    def __init__(self, vector_store: PostgresTravelVectorStore | None):
        self.vector_store = vector_store

    def fetch_travel_feeds(self, feed_urls: Iterable[str] | None = None) -> dict[str, Any]:
        if self.vector_store is None:
            return {
                "total_seen": 0,
                "total_added": 0,
                "feeds": [],
                "errors": ["PostgreSQL vector store is not configured."],
            }

        urls = [url for url in (feed_urls or travel_feeds) if is_usable_feed_url(url)]
        logger.info("开始获取RSS数据...")
        total_seen = 0
        total_added = 0
        feed_results: list[dict[str, Any]] = []
        errors: list[str] = []

        try:
            import feedparser
        except Exception as exc:
            return {
                "total_seen": 0,
                "total_added": 0,
                "feeds": [],
                "errors": [f"feedparser is required: {exc}"],
            }

        for feed_url in urls:
            logger.info("正在处理: %s", feed_url)
            try:
                feed = parse_feed(feedparser, feed_url)
                all_entries = list(getattr(feed, "entries", []) or [])
                entries = all_entries[:max_entries_per_feed()]
                if len(all_entries) > len(entries):
                    logger.info(
                        "RSS条目过多，仅处理前 %s 条: %s total=%s",
                        len(entries),
                        feed_url,
                        len(all_entries),
                    )
                if not all_entries and getattr(feed, "bozo", False):
                    errors.append(f"{feed_url}: {getattr(feed, 'bozo_exception', 'RSS parsed 0 entries')}")
                added_for_feed = 0
                for entry in entries:
                    total_seen += 1
                    content = entry_to_content(entry)
                    if not content:
                        continue
                    added_for_feed += self.vector_store.add_text(
                        content=content,
                        source_url=str(entry.get("link") or feed_url),
                        title=str(entry.get("title") or "旅行资讯"),
                        source_name=source_name_from_url(feed_url),
                        published_at=parse_entry_datetime(entry),
                        metadata={"feed_url": feed_url},
                    )
                total_added += added_for_feed
                feed_results.append({"url": feed_url, "seen": len(entries), "added": added_for_feed})
                logger.info("RSS处理完成: %s seen=%s added=%s", feed_url, len(entries), added_for_feed)
            except Exception as exc:
                logger.warning("RSS处理失败 %s: %s", feed_url, exc)
                errors.append(f"{feed_url}: {exc}")

        logger.info("RSS获取完成，新增 %s 条", total_added)
        return {
            "total_seen": total_seen,
            "total_added": total_added,
            "feeds": feed_results,
            "errors": errors,
        }


def parse_feed(feedparser: Any, feed_url: str) -> Any:
    import httpx

    timeout_seconds = feed_timeout_seconds()
    response = httpx.get(
        feed_url,
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
        headers={
            "User-Agent": "travel-assistant/1.0 (+https://localhost)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    response.raise_for_status()
    return feedparser.parse(response.content, response_headers=dict(response.headers))


def feed_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("TRAVEL_FEED_TIMEOUT_SECONDS", DEFAULT_FEED_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_FEED_TIMEOUT_SECONDS


def max_entries_per_feed() -> int:
    try:
        return max(1, int(os.getenv("TRAVEL_FEED_MAX_ENTRIES_PER_FEED", DEFAULT_MAX_ENTRIES_PER_FEED)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES_PER_FEED


def fetch_travel_feeds(feed_urls: Iterable[str], vector_store: PostgresTravelVectorStore | None) -> int:
    """抓取并存储新闻条目，保留与需求示例一致的函数入口。"""

    result = TravelNewsIngestionAgent(vector_store).fetch_travel_feeds(feed_urls)
    return int(result["total_added"])


def entry_to_content(entry: Any) -> str:
    parts = [
        str(entry.get("title", "")),
        str(entry.get("description", "")),
        str(entry.get("summary", "")),
        str(entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""),
    ]
    return normalize_text("\n".join(part for part in parts if part))


def parse_entry_datetime(entry: Any) -> datetime | None:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            return parsedate_to_datetime(value)
        except Exception:
            continue
    return None


def source_name_from_url(url: str) -> str:
    host = urlparse(url).netloc or "rss"
    return host.replace("www.", "")


def is_usable_feed_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.strip().lower()
    return "xxxxx" not in lowered


if __name__ == "__main__":
    fetch_travel_feeds(travel_feeds, vector_store=None)

