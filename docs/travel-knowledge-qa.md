# 旅行知识库与智能问答

## 架构

智能问答由四个部分组成：

- `TravelNewsIngestionAgent`：使用 `feedparser` 抓取 `TRAVEL_FEEDS` 或请求体中的 RSS 源，清洗标题、摘要、正文并写入知识库。RSS 只作为长期知识库补充。
- `PostgresTravelVectorStore`：使用当前项目 PostgreSQL 作为向量数据库，表为 `documents` 与 `document_chunks`，向量字段默认使用百炼 `tongyi-embedding-vision-plus-2026-03-06` 的 `vector(512)`。
- `WebSearchMCPClient`：可选实时搜索 MCP，遇到预约、开放时间、闭馆、限流、节假日、交通公告等问题时实时补充资料。
- `TravelQuestionAnsweringAgent`：保持 `/api/qa/ask` 的兼容入口，内部使用 LangGraph `create_react_agent` 创建回答智能体。
- `TravelQAGraphRunner`：用 LangGraph 编排问题分类、实时搜索、向量召回、资料合并排序、LLM 回答和本地兜底摘要。资料按“官方/高可信 > 地图资料 > 开放旅行指南 > 社区经验 > RSS”排序，再参考 RAG 总结约束生成中文回答。
- `PostgresQAConversationStore`：使用 PostgreSQL 保存问答会话和消息历史。登录用户通过 `user_id` 读取个人历史，未登录用户通过前端生成的 `anonymous_id` 也可以保存访客历史。
- `PostgresSaver`：使用同一个 `conversation_id` 作为 LangGraph `thread_id`，在 PostgreSQL checkpoint 表中保存 ReAct agent 的短期会话状态；`SummarizationNode` 作为 `pre_model_hook` 在模型调用前压缩长上下文。

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

## Updatable Metadata

Knowledge ingestion now uses a stable document identity and versioned chunks:

- `doc_id` identifies the logical document. It prefers `metadata.doc_id`, then `source_type + source_url`, and finally `source_type + source_name + title`.
- `content_hash` stores the SHA-256 hash of the normalized document or chunk content.
- `version_id` increments when the same logical document is ingested with changed content.
- `chunk_strategy`, `chunk_size`, `chunk_overlap`, `chunk_index`, and `chunk_count` record how chunks were produced.
- `embedding_model` and `embedding_dimension` record the vector contract used for each chunk.
- `is_deleted` is used for soft deletion. Searches only return rows where both the document and chunk are active.

This lets repeated ingestion skip unchanged content, replace changed content without stale retrieval, and keep old chunk versions available for audit.

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
  -d "{\"question\":\"端午去南京三天有哪些预约和错峰建议？\",\"top_k\":5,\"anonymous_id\":\"anon-browser-1\"}"
```

继续同一会话时，把上一次响应中的 `conversation_id` 带回：

```bash
curl -X POST http://127.0.0.1:8010/api/qa/ask ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"那博物馆怎么预约？\",\"conversation_id\":\"上一次返回的 conversation_id\",\"anonymous_id\":\"anon-browser-1\"}"
```

前端独立问答页使用流式接口：

```bash
curl -N -X POST http://127.0.0.1:8010/api/qa/ask/stream ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"那博物馆怎么预约？\",\"conversation_id\":\"上一次返回的 conversation_id\",\"anonymous_id\":\"anon-browser-1\"}"
```

流式响应采用 SSE 格式，主要事件包括：

- `start`：返回当前 `conversation_id`。
- `answer_delta`：返回本次回答的增量文本片段。
- `done`：返回完整 `TravelQAResponse`，包含来源、生成模式和消息 ID。

查询历史会话：

```bash
curl "http://127.0.0.1:8010/api/qa/conversations?anonymous_id=anon-browser-1"
```

## 配置实时搜索 MCP

推荐用实时搜索处理预约、开放时间、节假日公告和交通变化，而不是完全依赖固定 RSS。后端读取：

```env
WEB_SEARCH_MCP_COMMAND=["npx","-y","tavily-mcp"]
WEB_SEARCH_MCP_TOOL=web_search
```

也可以换成 Brave Search 等兼容 `query -> results` 的 MCP。问答 Agent 会自动构造偏官方的查询，例如“目的地 + 官方 + 预约 + 开放时间 + 文旅 + 景区 + 博物馆”。

## 配置问答联网兜底

智能问答使用 `langchain_tavily.TavilySearch` 作为 ReAct agent 的联网搜索工具。当向量库和实时资料都没有召回内容时，问答提示词会要求 agent 先调用联网搜索，再基于官方/高可信结果回答。后端 `.env` 可配置：

```env
TAVILY_API_KEY=your-tavily-api-key
TAVILY_MAX_RESULTS=5
TAVILY_SEARCH_DEPTH=basic
```

`TAVILY_MAX_RESULTS` 会限制在 1 到 10 之间；未配置 `TAVILY_API_KEY` 或设置 `DISABLE_EXTERNAL_API=true` 时，不会注册 Tavily 工具，问答会退回本地资料或兜底回答。

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
