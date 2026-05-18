# 景点图片搜索 Provider

后端现在会通过 `UnsplashMCPClient` 这个兼容旧命名的适配器搜索景点图片。实际顺序是：

1. Wikimedia Commons
2. Openverse
3. Pexels
4. Pixabay
5. Unsplash
6. `placehold.co` 占位图

当配置了 Pexels 或 Pixabay Key 时，会优先尝试 Wikimedia Commons 和 Openverse 这类开放授权来源，再进入图库来源；如果没有任何图片 API Key，也会尝试 Wikimedia Commons 和 Openverse。

## 注册和 Key 地址

| Provider | 是否需要 Key | 获取地址 |
| --- | --- | --- |
| Wikimedia Commons | 不需要 | https://commons.wikimedia.org/wiki/Commons:API |
| Openverse | 基础搜索不需要；高额度可注册 OAuth 应用 | https://api.openverse.org/v1/#tag/auth |
| Pexels | 需要 | https://www.pexels.com/api/ |
| Pixabay | 需要 | https://pixabay.com/api/docs/ |
| Unsplash | 需要 | https://unsplash.com/developers |
| Google Places Photos | 需要 Google Cloud API Key 和计费配置 | https://developers.google.com/maps/documentation/places/web-service/photos |
| Foursquare Places Photos | 需要开发者 Key，照片能力可能受套餐限制 | https://location.foursquare.com/developer/ |

## backend/.env 配置

```env
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
