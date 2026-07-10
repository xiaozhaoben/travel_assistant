# 生产化改造第一阶段设计

## 目标

第一阶段解决当前项目最紧迫的安全与发布基线问题：服务端签发匿名身份、统一身份解析、会话与报告资源归属、知识管理接口保护、Redis 分布式限流与任务状态、URL 入库 SSRF 防护，以及后端测试和前端容器构建恢复。

本阶段保持旅行规划和智能问答对匿名用户开放，不引入完整 RBAC、RAG 索引重构、模型网关、Trace 平台或 Prompt 管理平台。这些内容按后续独立阶段实施。

## 身份模型

系统使用统一 Bearer JWT 表示调用者身份。JWT 包含：

- `sub`：服务端生成的 UUID；
- `principal_type`：`anonymous` 或 `user`；
- `iat`、`exp`：签发和过期时间；
- 用户令牌继续包含 `preferred_username`。

新增 `POST /api/auth/anonymous`。当客户端没有可用令牌时调用该接口，服务端生成匿名 UUID 并签发默认有效期 30 天的匿名 JWT。客户端不得再自行声明可信的 `user_id` 或 `anonymous_id`。

后端新增统一 `Principal` 类型和依赖：

- `get_current_principal`：要求有效 Bearer Token；
- `get_current_principal_optional`：仅用于明确允许无身份访问的公共接口；如果请求携带了无效 Bearer Token，仍返回 `401`，不能静默降级成匿名访问；
- `require_user_principal`：要求 `principal_type=user`。

前端启动时从本地存储读取令牌；不存在或匿名令牌过期时获取新匿名令牌。Axios 与 SSE 使用同一个 `Authorization` 头。用户登录成功后切换为用户令牌。

## 资源归属与接口边界

问答会话继续使用 `user_id` 与 `anonymous_id` 两列，但所有创建、读取和追加消息操作都从当前 Principal 推导身份：

- 匿名身份写入 `anonymous_id=principal.sub`；
- 登录身份写入 `user_id=principal.sub`；
- 获取会话列表和详情时，SQL 必须同时校验会话所有者；
- 请求中的 `conversation_id` 只能复用属于当前 Principal 的会话，否则返回 `404`，避免泄露资源存在性；
- `TravelQARequest.user_id` 与 `anonymous_id` 从外部 API 契约移除或完全忽略。

`POST /api/auth/merge-anonymous` 接受匿名 Bearer Token，而不是客户端提供的任意匿名 ID。登录用户通过自己的用户 Bearer Token发起合并，并在单独请求头中提交待合并的匿名令牌。服务端验证其类型、签名和有效期后执行合并。

报告记录增加创建者类型与创建者 ID。匿名和登录用户只能列出、读取及更新自己的报告；历史无所有者记录不向普通调用者暴露。

接口权限按以下规则收紧：

- 匿名或登录用户：旅行规划、问答、自己的会话、自己的报告、地图和图片查询；
- 仅登录用户：知识文档入库、URL 入库、自动入库、知识搜索、入库任务创建及查询、资讯入库；
- 无需身份：健康检查、匿名令牌签发、用户注册和登录。

本阶段不新增管理员角色。知识管理先要求登录，后续 RBAC 阶段再细分管理员权限。

## Redis 设计

第一阶段直接使用 Redis，不保留进程内限流或任务状态作为正常运行路径。Redis 地址和凭据仅通过未跟踪的 `backend/.env` 或部署 Secret 提供，代码与文档中不写入真实密码。

新增配置：

- `REDIS_HOST`、`REDIS_PORT`、`REDIS_PASSWORD`、`REDIS_DB`；
- `REDIS_CONNECT_TIMEOUT_SECONDS`、`REDIS_SOCKET_TIMEOUT_SECONDS`；
- 可选 `REDIS_URL`，存在时优先使用。

当前 Redis 暂不支持 TLS，连接使用 `redis://`。Redis 仅保存限流计数、幂等键和知识入库任务状态，不保存 JWT、用户问题、回答或文档正文。部署侧应通过安全组仅允许后端出口 IP 访问 6379。

限流采用 Redis 原子脚本或事务实现固定窗口/滑动窗口策略，键至少包含 Principal、场景和时间窗口。首批场景：

- 匿名令牌签发；
- 注册和登录；
- 智能问答与流式问答；
- 旅行规划；
- 地图与图片外部查询；
- 知识入库和资讯入库。

Redis 不可用时采用按风险分类的策略：

- 知识写入、注册和登录：fail-closed，返回稳定的 `503`；
- 普通问答、规划、地图和图片查询：允许受控 fail-open，并记录结构化告警；
- 已创建的知识任务状态不可从 Redis 读取时返回 `503`，不能伪造为任务不存在。

知识任务状态迁移到 Redis Hash，设置合理 TTL。任务中只保存状态、时间、错误码和最终文档标识，不保存文档正文。第一阶段仍可由 FastAPI BackgroundTasks 执行实际任务；任务执行队列和独立 Worker 在下一阶段完成。

## URL 入库与输入安全

URL 入库仅接受 `http` 和 `https`。请求前解析主机并拒绝：

- 环回、私网、链路本地、组播、保留和未指定 IPv4/IPv6；
- `localhost` 和明显的本地主机别名；
- URL 内嵌用户名或密码；
- 非标准或不允许的协议。

HTTP 客户端默认禁用自动重定向。如果后续允许有限跳转，则每一跳都必须重新进行协议、DNS 和 IP 校验。响应设置连接、读取和总超时，并以流式方式读取；超过配置的最大字节数立即终止。仅接受文本、HTML 和 XHTML 内容类型。

外部异常映射为稳定错误码，例如 `KNOWLEDGE_FETCH_BLOCKED`、`KNOWLEDGE_FETCH_TIMEOUT`、`KNOWLEDGE_FETCH_TOO_LARGE`。HTTP、数据库、Redis 和供应商原始异常仅进入脱敏日志，不直接返回客户端。

## 发布基线修复

修复显式联网问答的 Prompt 契约漂移：统一系统行为、运行时强制联网指令和测试断言，确保“调用联网能力”与“禁止在最终回答暴露工具名”是两个独立约束。

修复测试对仓库目录名的硬编码，使测试在普通 checkout 和 git worktree 中都可运行。

补齐前端容器运行时配置：创建 `docker-entrypoint.sh`，启动时从环境变量生成 `/usr/share/nginx/html/config.js`，并保证脚本正确转义 JavaScript 字符串。普通 Vite/Pages 构建提供无副作用的默认 `public/config.js`，消除缺失脚本警告。

调整 Vite 分包，移除会生成空 chunk 的固定 `maps` 分组；Ant Design 体积优化作为第二阶段前端专项，不在本阶段做大规模组件改造。

新增 GitHub Actions 验证工作流，至少执行：

- 后端依赖安装和完整 pytest；
- 前端 `npm ci` 与 `npm run build`；
- 前后端 Docker build smoke test。

现有 Pages 部署继续保留，但部署必须依赖验证工作流成功。

## 错误处理与日志

API 使用稳定的错误响应结构，包含机器可读 `code`、用户可读 `message` 和请求关联 ID。服务端日志记录关联 ID、Principal 类型、场景、耗时和错误分类，不记录完整 JWT、Redis 密码或认证头。

无效或过期令牌统一返回 `401`；身份有效但权限不足返回 `403`；资源不属于当前 Principal 时返回 `404`；Redis 或数据库等基础设施不可用返回 `503`；限流返回 `429` 并携带 `Retry-After`。

## 测试策略

测试遵循先失败、后最小实现、再重构：

1. 匿名 JWT：签发、类型、篡改、过期和错误算法。
2. 身份依赖：无令牌、有效匿名、有效用户、无效 Bearer 头和过期令牌。
3. 会话归属：不能列出、读取、追加或复用其他 Principal 的会话。
4. 报告归属：列表、详情和图片回写均限制为当前 Principal。
5. 匿名合并：只允许登录用户合并一个有效匿名令牌对应的会话。
6. Redis：限流原子性、不同 Principal 隔离、TTL、Redis 故障时的 fail-open/fail-closed 行为。
7. SSRF：拒绝环回、私网、IPv6 本地地址、内嵌凭据、非法协议、重定向和超大响应；允许合规公网文本页面。
8. 回归：显式联网 Prompt 契约、worktree 路径兼容、完整 pytest。
9. 前端：匿名令牌启动、Axios 和 SSE 认证头、登录后令牌切换、运行时配置生成。
10. 构建：前端生产构建、后端和前端 Docker build。

## 完成标准

第一阶段完成必须同时满足：

- 所有受保护资源都从服务端 Principal 推导所有者；
- 匿名用户保留问答、规划和个人历史能力；
- URL 入库通过 SSRF 与响应大小防护；
- Redis 支撑跨进程限流和任务状态，且不保存敏感正文；
- 后端完整测试零失败，前端构建零错误；
- 两个 Docker 镜像均能构建；
- CI 在拉取请求和主分支推送时执行上述验证；
- 真实 Redis 密码没有进入 Git diff 或提交历史。

## 后续阶段

第二阶段处理独立 Worker、持久队列、幂等重试与任务恢复；第三阶段处理 HNSW、全文索引、RRF、reranker 和上下文预算；第四阶段处理 LangSmith Trace、Prompt 版本和评测门禁；第五阶段再拆分大文件、统一数据库连接池和优化前端包体积。
