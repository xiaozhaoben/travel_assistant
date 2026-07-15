# 管理员 RBAC 设计

## 目标

为旅行助手增加最小但完整的 `user/admin` 角色控制。知识库管理页面及其全部管理接口仅允许数据库中当前角色为 `admin` 的登录用户访问；旅行规划、智能问答、报告和匿名身份流程保持现状。

本阶段同时提供仅限服务器本地执行的角色管理命令，并持久记录角色变更审计。不新增公网角色管理 API，不实现多角色、多权限或组织级授权模型。

## 方案选择

采用数据库权威角色方案：`users.role` 是管理员授权的唯一事实来源。登录响应和前端缓存中的角色只用于界面展示，不能作为服务端放行依据。每次管理员请求都根据 JWT 的用户 ID 查询数据库，因此提升和降权立即生效，无需等待旧 JWT 过期。

未采用以下方案：

- 仅信任 JWT 角色：请求开销较低，但降权在令牌过期前无法生效。
- 配置 `ADMIN_USERNAMES` 自动提升：公开注册时可能产生用户名抢占和配置漂移风险。
- 完整角色、权限、用户角色关联表：当前只有两个固定角色，复杂度超过实际需要。

## 数据模型与迁移

`users` 表增加 `role text NOT NULL DEFAULT 'user'`，并限制取值为 `user` 或 `admin`。迁移必须幂等：先保证 `users` 表存在，再使用 `ADD COLUMN IF NOT EXISTS`，最后仅在约束不存在时增加角色检查约束。已有用户通过默认值自动成为普通用户；注册接口始终创建 `user`，不接收客户端角色。

新增 `user_role_audit` 表：

- `id`：UUID 主键；
- `user_id`：目标用户 ID，引用 `users.id`；
- `username`：变更时的用户名快照；
- `previous_role`、`new_role`：变更前后角色，均限制为 `user/admin`；
- `changed_by`：执行本地命令的操作系统账号；
- `changed_at`：数据库生成的变更时间。

角色更新与审计插入必须在同一数据库事务内完成。重复提升管理员或重复降权普通用户属于幂等无变化操作，不写审计记录。

## 身份与授权边界

后端增加 `UserRole = Literal["user", "admin"]`。登录、注册和 `/api/auth/me` 响应返回角色，前端据此展示界面。JWT 可以继续只承载用户 ID 和用户名；服务端管理员授权不依赖 JWT 角色，避免角色信息陈旧。

新增 `require_admin_principal` FastAPI 依赖：

1. 通过现有 Bearer 认证解析有效 `user` Principal；
2. 使用 Principal 的用户 ID 查询 `users` 表；
3. 仅当数据库当前角色为 `admin` 时返回 Principal；
4. 普通用户、已降权用户或数据库中不存在的用户返回 `403 AUTH_ADMIN_REQUIRED`；
5. 角色查询基础设施不可用时返回 `503 AUTH_ROLE_CHECK_UNAVAILABLE`，不得降级为放行。

`/api/auth/me` 每次查询数据库并返回当前用户名和角色。角色降权后旧 JWT 仍可用于普通用户功能，但立即失去管理员权限。

## 管理员接口范围

以下接口统一使用管理员依赖，同时保留现有 Redis 限流策略：

- `POST /api/news/ingest`
- `GET /api/news/status`
- `POST /api/knowledge/documents`
- `POST /api/knowledge/documents/from-url`
- `POST /api/knowledge/documents/auto`
- `POST /api/knowledge/documents/from-url/jobs`
- `POST /api/knowledge/documents/auto/jobs`
- `GET /api/knowledge/documents/jobs/{job_id}`
- `POST /api/knowledge/search`

管理员校验应位于 HTTP 入口依赖中。后台入库任务由已通过校验的任务创建接口触发，内部执行函数不重新模拟 HTTP 鉴权。智能问答 Agent 直接使用向量库的内部检索路径，不受管理接口 RBAC 影响，匿名问答继续可用。

## 本地角色管理命令

新增模块 `app.auth.admin_cli`，从后端运行目录使用：

```bash
python -m app.auth.admin_cli promote <username>
python -m app.auth.admin_cli demote <username>
python -m app.auth.admin_cli show <username>
```

命令复用应用的数据库配置和连接管理器，只处理已存在用户，不创建账号、不接收密码、不输出数据库连接信息。`promote` 与 `demote` 在单个事务内锁定目标用户、更新角色并写入审计；`show` 返回用户名、当前角色及最近的角色变更摘要。未知用户返回非零退出码和稳定、无敏感信息的错误文本。`changed_by` 使用当前操作系统账号的规范化文本。

## 前端行为

`AuthUser` 增加 `role: 'user' | 'admin'`。读取旧版 localStorage 时，缺少或非法角色一律按 `user` 处理，不能因缓存损坏获得管理权限。

导航栏仅在当前用户角色为 `admin` 时显示知识库入口。`/knowledge` 路由使用 `requiresAdmin` 元数据；进入该路由前调用 `/api/auth/me` 刷新数据库当前身份：

- 管理员继续进入；
- 普通用户更新本地身份、跳回首页并显示“需要管理员权限”；
- 无效令牌沿用现有 401 失效和匿名身份恢复流程；
- `503 AUTH_ROLE_CHECK_UNAVAILABLE` 显示服务暂不可用，不错误地清除有效登录状态。

应用启动后对已登录用户执行非阻塞 `/api/auth/me` 刷新，使提升角色后刷新浏览器即可显示知识库入口。管理员接口返回 `403 AUTH_ADMIN_REQUIRED` 时，前端刷新当前用户资料并退出知识管理页，但不把普通用户登出。

## 错误与日志

RBAC 使用现有稳定 API 错误结构：

- `401 AUTH_REQUIRED` / `AUTH_TOKEN_INVALID`：缺少、无效或过期令牌；
- `403 AUTH_USER_REQUIRED`：匿名身份访问用户功能；
- `403 AUTH_ADMIN_REQUIRED`：有效用户没有管理员角色；
- `503 AUTH_ROLE_CHECK_UNAVAILABLE`：无法从数据库确认角色。

日志仅记录请求关联信息、用户 ID、权限结果和异常类型，不记录完整 JWT、Authorization、密码、数据库连接串或 Redis 凭据。CLI 审计写入数据库，不依赖易丢失的终端输出。

## 测试策略

所有行为变更遵循先失败、后最小实现：

1. 用户表迁移：新旧表结构、默认角色、合法角色约束和幂等执行；
2. 角色存储：查询、提升、降权、重复操作、事务审计和未知用户；
3. 管理员依赖：管理员放行，普通用户、匿名用户、已降权旧 JWT、缺失用户和数据库故障；
4. 认证响应：注册固定为 `user`，登录和 `/api/auth/me` 返回数据库当前角色；
5. 接口矩阵：所有知识与资讯管理接口对普通用户返回稳定 403，对管理员通过授权边界；
6. CLI：`promote`、`demote`、`show` 的退出码、输出和敏感信息边界；
7. 前端契约：管理员导航、`requiresAdmin` 路由、`/api/auth/me` 刷新、旧缓存降级和 403 行为；
8. 回归：完整后端测试、前端生产构建、Shell/差异检查和凭据扫描。

## 发布与回滚

发布顺序：先部署包含幂等迁移的后端，注册或确认目标普通用户，再在后端运行环境执行 `promote`。刷新浏览器后验证管理员可以进入知识库、普通用户返回 403，最后执行一次降权再提升测试，确认旧 JWT 立即受数据库角色控制且审计记录完整。

角色迁移或查询失败时采用 fail-closed：普通旅行功能继续可用，管理员功能返回 503。回滚应用版本不会删除新增列或审计表；旧版本会忽略这些字段，避免破坏已有用户数据。

## 非目标

- 公网角色管理 API 或管理后台用户列表；
- 超级管理员、组织、租户、动态权限或资源级 ACL；
- JWT 刷新令牌和会话撤销系统；
- 独立知识入库 Worker 与持久任务队列；
- 修改旅行规划、问答、报告的现有所有权规则。

## 完成标准

- 现有用户默认是普通用户，注册无法自选管理员；
- 本地 CLI 可安全提升、降权和查询角色，每次有效变更均有持久审计；
- 所有知识管理入口只接受数据库当前角色为管理员的用户；
- 降权对旧 JWT 立即生效，数据库故障不会误放行；
- 普通用户看不到知识库入口，直接访问会被实时校验并安全跳转；
- 匿名旅行规划和智能问答行为不变；
- 后端完整测试与前端生产构建通过，未引入真实凭据。
