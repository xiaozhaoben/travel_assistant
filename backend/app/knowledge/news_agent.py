from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlparse

from app.knowledge.vector_store import PostgresTravelVectorStore, normalize_text

logger = logging.getLogger(__name__)


travel_feeds = [
    "https://www.tuniu.com/rss",
    "https://rsshub.app/mafengwo/note",
    "https://rsshub.app/zhihu/collection/xxxxx",
]


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

        urls = [url for url in (feed_urls or travel_feeds) if url]
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
                feed = feedparser.parse(feed_url)
                entries = list(getattr(feed, "entries", []) or [])
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


if __name__ == "__main__":
    fetch_travel_feeds(travel_feeds, vector_store=None)

