from __future__ import annotations

import json
import logging
import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.core.config import BACKEND_DIR, get_settings
from app.domain.models import ResearchSnippet

logger = logging.getLogger(__name__)


class WebSearchMCPClient:
    """Configurable MCP adapter for web search style tools."""

    def __init__(
        self,
        command: str | None = None,
        tool_name: str | None = None,
        mcp_caller=None,
    ):
        settings = get_settings()
        self.command = command if command is not None else settings.web_search_mcp_command
        self.tool_name = tool_name or settings.web_search_mcp_tool
        self.mcp_caller = mcp_caller

    @property
    def available(self) -> bool:
        return bool(self.mcp_caller or self.command)

    def search(self, query: str) -> list[dict[str, Any]]:
        if self.mcp_caller:
            return self._normalize_result(self.mcp_caller.call_tool(self.tool_name, {"query": query}))
        if not self.command:
            return []
        return self._call_stdio(query)

    def _call_stdio(self, query: str) -> list[dict[str, Any]]:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = _parse_command(self.command or "")

        async def _call() -> list[dict[str, Any]]:
            server = StdioServerParameters(
                command=command[0],
                args=command[1:],
                env=os.environ.copy(),
            )
            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(self.tool_name, {"query": query})
                    text = "\n".join(
                        getattr(item, "text", "")
                        for item in result.content
                        if getattr(item, "text", "")
                    )
                    return self._normalize_result(text)

        try:
            return anyio.run(_call)
        except Exception as exc:
            logger.warning("Web search MCP failed, using local research fallback: %s", exc)
            return []

    def _normalize_result(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            if isinstance(raw.get("results"), list):
                return raw["results"]
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
                return self._normalize_result(data)
            except json.JSONDecodeError:
                return [{"title": "联网资料", "url": None, "content": raw}]
        return []


class DestinationResearchService:
    def __init__(
        self,
        web_client: WebSearchMCPClient | None = None,
        cache_path: Path | None = None,
    ):
        settings = get_settings()
        self.web_client = web_client or WebSearchMCPClient()
        self.cache_path = cache_path or BACKEND_DIR / "runtime" / "research_cache.json"
        self.cache_enabled = settings.research_cache_enabled
        self.cache_ttl = timedelta(seconds=max(0, settings.research_cache_ttl_seconds))
        self.cache_max_entries = max(1, settings.research_cache_max_entries)

    def research(self, city: str, preferences: Iterable[str], days: int) -> list[ResearchSnippet]:
        preference_list = [item for item in preferences if item]
        cache_key = self._cache_key(city, preference_list, days)
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        snippets = self._search_web(city, preference_list, days)
        if not snippets:
            snippets = self._fallback_snippets(city, preference_list, days)
        self._save_cache(cache_key, snippets)
        return snippets

    def _search_web(self, city: str, preferences: list[str], days: int) -> list[ResearchSnippet]:
        if not self.web_client.available:
            return []
        query = f"{city} {days}天 旅行 攻略 {' '.join(preferences)} 预约 交通 美食 避坑"
        snippets = []
        for item in self.web_client.search(query)[:6]:
            title = str(item.get("title") or item.get("name") or "目的地资料")
            summary = str(item.get("summary") or item.get("content") or item.get("snippet") or "")
            if not summary:
                continue
            snippets.append(
                ResearchSnippet(
                    source="web",
                    title=title,
                    url=item.get("url") or item.get("link"),
                    summary=summary[:240],
                    keywords=self._keywords(f"{title} {summary}", preferences),
                )
            )
        return snippets

    def _fallback_snippets(self, city: str, preferences: list[str], days: int) -> list[ResearchSnippet]:
        preference_text = "、".join(preferences) or "经典必游"
        return [
            ResearchSnippet(
                source="local",
                title=f"{city}{days}天{preference_text}行程提示",
                summary=f"{city}{days}天行程建议每天安排2-3个景点，热门场馆提前预约，雨天优先安排博物馆和室内展馆。",
                keywords=[city, "预约", "交通", "美食", *preferences],
            ),
            ResearchSnippet(
                source="local",
                title=f"{city}交通与体力安排",
                summary="核心城区建议公共交通加步行；远郊景点单独安排半天，低强度路线减少跨城折返。",
                keywords=[city, "交通", "低强度", "公共交通"],
            ),
        ]

    def _keywords(self, text: str, preferences: list[str]) -> list[str]:
        candidates = ["预约", "交通", "美食", "酒店", "避坑", "博物馆", "亲子", "老人", "低强度"]
        found = [word for word in candidates if word in text]
        return list(dict.fromkeys([*preferences, *found])) or preferences

    def _get_cached(self, key: str) -> list[ResearchSnippet] | None:
        if not self.cache_enabled:
            return None
        entry = self._load_cache().get(key)
        if not entry:
            return None
        if isinstance(entry, list):
            return [ResearchSnippet.model_validate(item) for item in entry]
        if not isinstance(entry, dict):
            return None
        cached_at = self._parse_cached_at(entry.get("cached_at"))
        if cached_at and self.cache_ttl.total_seconds() > 0:
            if datetime.now(timezone.utc) - cached_at > self.cache_ttl:
                return None
        items = entry.get("items")
        if not isinstance(items, list):
            return None
        return [ResearchSnippet.model_validate(item) for item in items]

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_enabled or not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self, key: str, snippets: list[ResearchSnippet]) -> None:
        if not self.cache_enabled:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache = self._load_cache()
            cache[key] = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "items": [snippet.model_dump(mode="json") for snippet in snippets],
            }
            cache = self._trim_cache(cache)
            temp_path = self.cache_path.with_suffix(f"{self.cache_path.suffix}.tmp")
            temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.cache_path)
        except Exception as exc:
            logger.debug("Research cache write skipped: %s", exc)

    def _cache_key(self, city: str, preferences: list[str], days: int) -> str:
        return "|".join([city, str(days), ",".join(sorted(preferences))])

    def _trim_cache(self, cache: dict[str, Any]) -> dict[str, Any]:
        if len(cache) <= self.cache_max_entries:
            return cache
        ordered = sorted(
            cache.items(),
            key=lambda item: self._parse_cached_at(item[1].get("cached_at") if isinstance(item[1], dict) else None)
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return dict(ordered[: self.cache_max_entries])

    def _parse_cached_at(self, value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _parse_command(command: str) -> list[str]:
    command = command.strip()
    if command.startswith("["):
        parsed = json.loads(command)
        return [str(item) for item in parsed]
    return shlex.split(command)
