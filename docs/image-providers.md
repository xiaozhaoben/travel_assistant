# 景点图片搜索 Provider

后端现在会通过 `UnsplashMCPClient` 这个兼容旧命名的适配器搜索景点图片。实际顺序是：

1. Web Search MCP + 大模型筛选（如果配置了 `WEB_SEARCH_MCP_COMMAND` 和大模型 Key，先从搜索结果的 `image`、`image_url`、`thumbnail` 等字段整理候选图片，再由大模型选择最贴近景点实拍/官方高可信的 URL）
2. Wikimedia Commons
3. Openverse
4. Pexels
5. Pixabay
6. Unsplash
7. 无结果时返回空字符串，由前端使用本地生成图片兜底，避免额外网络占位图

当配置了实时搜索 MCP 和大模型 Key 时，会先尝试用网页搜索找到候选图片，并让大模型只从候选 URL 中选择最合适的一张；大模型未选出有效图片时，再尝试 Wikimedia Commons 和 Openverse 这类开放授权来源，然后进入图库来源。如果没有大模型 Key 但配置了实时搜索 MCP，会使用搜索结果中的首个图片 URL 作为轻量兜底。

## 注册和 Key 地址

| Provider | 是否需要 Key | 获取地址 |
| --- | --- | --- |
| Web Search MCP | 取决于 MCP server，例如 Tavily 需要对应 Key | https://github.com/tavily-ai/tavily-mcp |
| Wikimedia Commons | 不需要 | https://commons.wikimedia.org/wiki/Commons:API |
| Openverse | 基础搜索不需要；高额度可注册 OAuth 应用 | https://api.openverse.org/v1/#tag/auth |
| Pexels | 需要 | https://www.pexels.com/api/ |
| Pixabay | 需要 | https://pixabay.com/api/docs/ |
| Unsplash | 需要 | https://unsplash.com/developers |
| Google Places Photos | 需要 Google Cloud API Key 和计费配置 | https://developers.google.com/maps/documentation/places/web-service/photos |
| Foursquare Places Photos | 需要开发者 Key，照片能力可能受套餐限制 | https://location.foursquare.com/developer/ |

## backend/.env 配置

```env
LLM_MODEL_ID=
LLM_API_KEY=
LLM_BASE_URL=
WEB_SEARCH_MCP_COMMAND=["npx","-y","tavily-mcp"]
WEB_SEARCH_MCP_TOOL=web_search
PEXELS_API_KEY=
PIXABAY_API_KEY=
UNSPLASH_ACCESS_KEY=
UNSPLASH_SECRET_KEY=
OPENVERSE_CLIENT_ID=
OPENVERSE_CLIENT_SECRET=
WIKIMEDIA_USER_AGENT=travel-assistant/1.0 (contact: your-email@example.com)
```

WIKIMEDIA_USER_AGENT 建议改成带项目名和联系方式的描述性值，否则 Wikimedia 可能返回 403。

当前代码未接入 Google Places Photos 和 Foursquare Photos，因为它们更偏 POI 详情付费能力，建议确认成本后再作为精确 POI 图片源加入。

## 授权注意

Wikimedia Commons 和 Openverse 返回的图片通常需要保留作者、来源链接和协议；Pexels、Pixabay、Unsplash 也有各自的展示、缓存、归因或下载事件要求。上线前建议在图片对象里扩展 `source`、`author`、`license`、`attribution_url` 字段。
