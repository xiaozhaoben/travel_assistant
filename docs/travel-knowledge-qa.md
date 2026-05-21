# 旅行知识库与智能问答

## 架构

智能问答由三个部分组成：

- `TravelNewsIngestionAgent`：使用 `feedparser` 抓取 `travel_feeds` 中的 RSS 源，清洗标题、摘要、正文并写入知识库。
- `PostgresTravelVectorStore`：使用当前项目 PostgreSQL 作为向量数据库，表为 `travel_knowledge_documents`，向量字段为 `vector(384)`。
- `TravelQuestionAnsweringAgent`：先向量召回旅行资讯，再参考 `E:\project\langchain-agent\prompts` 的 RAG 总结约束生成中文回答。

## 数据库要求

PostgreSQL 需要启用 pgvector：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

应用会自动创建业务表。若数据库账号没有创建扩展权限，请先由管理员执行上面的 SQL。

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

## 配置 RSS

默认 RSS 源位于 `backend/app/knowledge/news_agent.py`：

```python
travel_feeds = [
    "https://www.tuniu.com/rss",
    "https://rsshub.app/mafengwo/note",
    "https://rsshub.app/zhihu/collection/xxxxx",
]
```

可直接替换为实际可用的旅游资讯源。接口也支持在请求体中临时传入 `feed_urls`。
