# 旅游规划助手

Vue3 + FastAPI + LangChain 的旅游规划助手。用户输入一句自然语言需求，例如“我想去北京玩 3 天，喜欢历史文化，预算中等”，系统会生成包含景点、酒店、餐饮、天气、地图点位和预算的可编辑行程。首页还提供旅行智能问答：RSS 旅行资讯会由采集 Agent 入库到 PostgreSQL/pgvector，并作为问答 Agent 的 RAG 资料来源。

## 项目结构

```text
backend/   FastAPI、四个业务 Agent、MCP 风格外部 API 适配层
frontend/  Vue3 + TypeScript + Vite 前端工作台
```

## 后端

先复制并填写环境变量：

```bash
copy backend\.env.example backend\.env
```

当前代码读取 `backend/.env`，关键配置如下：

```env
LLM_MODEL_ID=qwen3.6-plus
LLM_API_KEY=你的大模型Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_TIMEOUT=60
LLM_ENABLE_THINKING=false
PLANNER_MODE=fast
RESEARCH_CACHE_ENABLED=true
RESEARCH_CACHE_TTL_SECONDS=86400
AMAP_API_KEY=你的高德Web服务Key
UNSPLASH_ACCESS_KEY=你的Unsplash Access Key
```

`PlannerAgent` 会通过 LangChain 的 OpenAI-compatible `ChatOpenAI` 调用大模型；如果没有配置 `LLM_API_KEY`，会自动使用本地 fallback 行程，保证开发环境仍可运行。
DashScope/Qwen 模型默认建议保持 `LLM_ENABLE_THINKING=false`，这样更适合快速返回结构化 JSON 行程。
默认 `PLANNER_MODE=fast` 会跳过最终 Planner 大模型整合以提升响应速度；如需更强的文本质量和路线解释，可设为 `quality`。

高德地图后端调用走 MCP stdio，不再直接请求高德 REST API。后端会通过以下 MCP server 启动方式调用工具：

```json
["uvx", "amap-mcp-server"]
```

MCP server 使用 `AMAP_MAPS_API_KEY` 环境变量。当前配置兼容 `AMAP_API_KEY` 和 `AMAP_MAPS_API_KEY`，代码会把读取到的高德 Web 服务 Key 传给 MCP server。

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8010
```

如果 8000 被占用，可以换端口，例如 `8010`。

如果本机已经配置了大模型或地图图片 Key，但希望强制使用本地 fallback 数据，可以在启动后端前设置：

```bash
set DISABLE_LLM=true
set DISABLE_EXTERNAL_API=true
```

## 前端

```bash
cd frontend
npm ci
npm run dev
```

如果后端不是 8000 端口，启动前设置：

```bash
set VITE_BACKEND_URL=http://127.0.0.1:8010
set VITE_API_BASE_URL=http://127.0.0.1:8010
npm run dev
```

本地开发通常使用 `VITE_BACKEND_URL` 让 Vite 代理 `/api` 请求；如果前端构建后由其他静态服务托管，则使用 `VITE_API_BASE_URL` 直接指定后端地址。

如果需要显示真实高德 JS 地图，在前端启动前补充：

```bash
set VITE_AMAP_WEB_JS_KEY=你的高德Web端JSKey
set VITE_AMAP_SECURITY_JS_CODE=你的高德Web端安全密钥
```

不配置时结果页会显示内置模拟地图，仍然支持删除景点、调整顺序并重新计算预算。

## 旅行知识库与智能问答

知识库复用当前项目的 PostgreSQL 配置，并要求数据库安装 `pgvector` 扩展。配置 `DATABASE_URL` 或 `POSTGRES_HOST` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` 后，首次调用资讯入库会自动创建 `travel_knowledge_documents` 表。

RSS 源配置在 `backend/app/knowledge/news_agent.py` 的 `travel_feeds` 变量中：

```python
travel_feeds = [
    "https://www.tuniu.com/rss",
    "https://rsshub.app/mafengwo/note",
    "https://rsshub.app/zhihu/collection/xxxxx",
]
```

问答流程：

1. `POST /api/news/ingest` 抓取 RSS，使用 `feedparser` 解析并清洗 HTML。
2. 新闻内容切片后写入 PostgreSQL `vector(384)` 字段。
3. `POST /api/qa/ask` 先向量召回，再按参考项目的 RAG prompt 风格生成回答。
4. 如果未配置大模型，会返回基于召回资料的本地摘要；如果未配置数据库，会明确提示先配置知识库。

## API

- `GET /api/health`：查看大模型、高德地图、Unsplash 配置状态。
- `POST /api/trip/plan`：根据自然语言需求生成完整行程。
- `POST /api/trip/recalculate`：删除景点或调整顺序后重新计算路线点和预算。
- `POST /api/news/ingest`：使用旅行资讯采集 Agent 抓取 RSS 并写入 PostgreSQL 向量库。
- `GET /api/news/status`：查看旅行知识库与默认 RSS 源配置。
- `POST /api/qa/ask`：基于 PostgreSQL 向量库召回资料并回答旅行问题。
- `GET /api/map/poi`：通过 `uvx amap-mcp-server` 的 `maps_text_search` / `maps_search_detail` 工具搜索 POI。
- `GET /api/map/weather`：通过 `uvx amap-mcp-server` 的 `maps_weather` 工具查询天气。
- `GET /api/poi/photo`：通过 Unsplash MCP 风格适配层获取景点图片。

## 日志

后端启动时会初始化统一日志系统，日志同时输出到控制台和 `backend/logs/travel_assistant.log`。

每条日志都会带有时间；四个 Agent 的调用日志是 JSON 格式，包含：

- `timestamp`：日志产生时间。
- `agent`：Agent 名称，例如 `AttractionSearchAgent`。
- `event`：`input` 或 `output`。
- `payload`：该 Agent 的输入或输出数据。

## 编码与验证

Windows PowerShell 如遇中文日志或 README 显示乱码，先执行：

```powershell
chcp 65001
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
```

首次验证建议先安装依赖：

```bash
python -m venv backend\.venv
backend\.venv\Scripts\activate
pip install -r backend\requirements.txt
python -m pytest backend\tests\test_trip_planner.py -q

cd frontend
npm ci
npm run build
```

## 景点图片 API Key 注册地址

多源图片搜索说明见 `docs/image-providers.md`。常用注册入口：

- Wikimedia Commons：无需 Key，文档 https://commons.wikimedia.org/wiki/Commons:API
- Openverse：基础搜索无需 Key，高额度 OAuth 文档 https://api.openverse.org/v1/#tag/auth
- Pexels：https://www.pexels.com/api/
- Pixabay：https://pixabay.com/api/docs/
- Unsplash：https://unsplash.com/developers
- Google Places Photos：https://developers.google.com/maps/documentation/places/web-service/photos
- Foursquare Places：https://location.foursquare.com/developer/
