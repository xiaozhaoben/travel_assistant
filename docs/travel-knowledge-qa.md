# 旅行知识库与智能问答

## 架构

智能问答由四个部分组成：

- `TravelNewsIngestionAgent`：使用 `feedparser` 抓取 `TRAVEL_FEEDS` 或请求体中的 RSS 源，清洗标题、摘要、正文并写入知识库。RSS 只作为长期知识库补充。
- `PostgresTravelVectorStore`：使用当前项目 PostgreSQL 作为向量数据库，表为 `travel_knowledge_documents`，向量字段默认使用百炼 `tongyi-embedding-vision-plus-2026-03-06` 的 `vector(512)`。
- `WebSearchMCPClient`：可选实时搜索 MCP，遇到预约、开放时间、闭馆、限流、节假日、交通公告等问题时实时补充资料。
- `TravelQuestionAnsweringAgent`：保持 `/api/qa/ask` 的兼容入口，内部委托给 LangGraph 问答流程。
- `TravelQAGraphRunner`：用 LangGraph 编排问题分类、实时搜索、向量召回、资料合并排序、LLM 回答和本地兜底摘要。资料按“官方/高可信 > 地图资料 > 开放旅行指南 > 社区经验 > RSS”排序，再参考 RAG 总结约束生成中文回答。

## 数据库要求

PostgreSQL 需要启用 pgvector：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

应用会自动创建业务表。若数据库账号没有创建扩展权限，请先由管理员执行上面的 SQL。

## Embedding 与切片

知识库切片参考 `E:\project\langchain-agent\rag\vector_store.py`，使用 LangChain `RecursiveCharacterTextSplitter`：

```env
EMBEDDING_PROVIDER=dashscope
EMBEDDING_MODEL_ID=tongyi-embedding-vision-plus-2026-03-06
EMBEDDING_DIMENSIONS=512
EMBEDDING_API_KEY=
```

未单独配置 `EMBEDDING_API_KEY` 时会复用 `LLM_API_KEY`。如果已有表还是 `vector(384)`，后端启动并初始化 schema 时会清空旧知识库行并把 `embedding` 列改为 `vector(512)`，然后需要重新执行资讯入库。

## API

抓取默认 RSS：

```bash
curl -X POST http://127.0.0.1:8010/api/news/ingest ^
  -H "Content-Type: application/json" ^
  -d "{\"feed_urls\":[]}"
```

提问：

```bash
curl -X POST http://127.0.0.1:8010/api/qa/ask ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"端午去南京三天有哪些预约和错峰建议？\",\"top_k\":5}"
```

## 配置实时搜索 MCP

推荐用实时搜索处理预约、开放时间、节假日公告和交通变化，而不是完全依赖固定 RSS。后端读取：

```env
WEB_SEARCH_MCP_COMMAND=["npx","-y","tavily-mcp"]
WEB_SEARCH_MCP_TOOL=web_search
```

也可以换成 Brave Search 等兼容 `query -> results` 的 MCP。问答 Agent 会自动构造偏官方的查询，例如“目的地 + 官方 + 预约 + 开放时间 + 文旅 + 景区 + 博物馆”。

## 配置 RSS

默认 RSS 源位于 `backend/app/knowledge/news_agent.py`，也可通过环境变量覆盖：

```env
RSSHUB_BASE_URL=https://rsshub.rssforever.com
TRAVEL_FEEDS=
```

当 `TRAVEL_FEEDS` 为空时，会使用内置推荐源：

- 马蜂窝热门/最新游记：`/mafengwo/note/hot`、`/mafengwo/note/latest`
- iMuseum 北京/上海展览：`/imuseum/beijing/all`、`/imuseum/shanghai/all`
- 国博资讯：`/chnmuseum/zx/xingnew`、`/chnmuseum/zx/xwzt`
- 中国美术馆：`/namoc/news`、`/namoc/exhibition`、`/namoc/announcement`
- 交通公告：`/12306/zxdt`、`/airchina/announcement`、`/guangzhoumetro/news`、`/fzmtr/announcements`
- 城市活动：`/huodongxing/explore`

建议优先选择官方文旅、景区、博物馆、交通公告或稳定旅行指南源。社区攻略源可以保留，但只作为体验参考，不作为预约、开放时间和政策依据。接口也支持在请求体中临时传入 `feed_urls`。
