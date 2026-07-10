# Production Hardening Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Secure anonymous and authenticated resource access, add Redis-backed rate limiting and knowledge-job state, block SSRF in URL ingestion, and restore a reliable test/container/CI release baseline.

**Architecture:** A unified signed `Principal` is the only trusted identity source. Storage methods enforce owner scope, Redis provides cross-process control-plane state, and a dedicated URL fetcher isolates outbound-request security. Frontend bootstrap obtains an anonymous Bearer token before any protected API call.

**Tech Stack:** FastAPI, Pydantic 2, python-jose, psycopg 3, redis-py, httpx, pytest, Vue 3, TypeScript, Axios, Vite, Docker, GitHub Actions.

---

## File map

- Create `backend/app/auth/principal.py`: principal model, JWT creation/decoding, FastAPI dependencies.
- Create `backend/app/core/api_errors.py`: stable API errors and request correlation middleware.
- Create `backend/app/core/redis_client.py`: Redis connection creation and health boundary.
- Create `backend/app/core/rate_limit.py`: Redis fixed-window limiter and FastAPI enforcement helper.
- Create `backend/app/knowledge/job_store.py`: Redis-backed knowledge ingestion job state.
- Create `backend/app/security/url_fetcher.py`: URL validation, DNS/IP checks, bounded streaming fetch.
- Modify `backend/app/auth/service.py`: user JWTs use the unified principal claims.
- Modify `backend/app/core/config.py`: anonymous JWT, Redis, rate-limit and URL-fetch settings.
- Modify `backend/app/domain/models.py`: principal token response and removal of client-trusted QA identity.
- Modify `backend/app/storage/qa_store.py`: owner-scoped conversation reads and writes.
- Modify `backend/app/storage/report_store.py`: owner columns and owner-scoped report queries.
- Modify `backend/app/main.py`: dependencies, protected endpoints, Redis state, safe URL ingestion.
- Modify `backend/requirements.txt` and `backend/constraints.txt`: redis-py dependency.
- Create focused backend tests under `backend/tests/` instead of extending the 4,000-line legacy test module.
- Modify `frontend/src/services/api.ts`, `frontend/src/services/auth.ts`, `frontend/src/main.ts`, `frontend/src/views/QA.vue`, and `frontend/src/types/index.ts`: anonymous-token bootstrap and unified Bearer flow.
- Create `frontend/public/config.js` and `frontend/docker-entrypoint.sh`; modify Docker/Vite configuration.
- Modify `.github/workflows/deploy-frontend-pages.yml`: verification jobs gate deployment.

## Task 1: Restore a portable baseline and resolve the existing Prompt contract failure

**Files:**
- Modify: `backend/tests/test_trip_planner.py:830-860`
- Modify: `backend/tests/test_trip_planner.py:949-959`
- Modify: `backend/app/knowledge/qa_agent.py:380-392`

- [ ] **Step 1: Make the Prompt contract test describe behavior instead of an internal tool phrase**

Replace the failing assertion with:

```python
assert "用户明确要求联网或问题涉及时效信息" in prompt
assert "必须先获取联网搜索结果" in prompt
assert "不要在最终回答中暴露工具名" in prompt
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_trip_planner.py::test_travel_qa_agent_prompts_web_search_when_user_explicitly_requests_it -q
```

Expected: FAIL because the runtime instruction does not yet contain the two explicit constraints.

- [ ] **Step 3: Separate execution and presentation constraints in `qa_context_for_prompt`**

Use this instruction text:

```python
instruction = (
    "【联网要求】用户明确要求联网或问题涉及时效信息，必须先获取联网搜索结果，"
    "并优先采用官方/高可信资料；如果仍无可靠结果，明确说明不确定。"
    "不要在最终回答中暴露工具名、函数名或内部调用过程。"
)
```

- [ ] **Step 4: Remove the checkout-directory-name assertion**

Replace the hard-coded parent-name assertion with repository invariants:

```python
assert BACKEND_DIR.name == "backend"
assert (BACKEND_DIR / "app").is_dir()
assert ENV_PATH == BACKEND_DIR / ".env"
assert DestinationResearchService().cache_path.parent == BACKEND_DIR / "runtime"
```

- [ ] **Step 5: Verify both regressions and commit**

Run:

```powershell
python -m pytest backend/tests/test_trip_planner.py::test_travel_qa_agent_prompts_web_search_when_user_explicitly_requests_it backend/tests/test_trip_planner.py::test_backend_paths_stay_at_backend_root_after_package_split -q
```

Expected: `2 passed`.

Commit:

```powershell
git add backend/app/knowledge/qa_agent.py backend/tests/test_trip_planner.py
git commit -m "fix: 修复联网提示词契约和工作树测试"
```

## Task 2: Add the unified Principal and anonymous token endpoint

**Files:**
- Create: `backend/app/auth/principal.py`
- Create: `backend/app/core/api_errors.py`
- Create: `backend/tests/test_auth_principal.py`
- Modify: `backend/app/auth/service.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/domain/models.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing principal tests**

Create tests for anonymous issuance, user issuance, tampering, expiry, missing/invalid Bearer headers, and legacy user-token compatibility. Core assertions:

```python
def test_anonymous_token_round_trip():
    token = create_principal_token(
        Principal(subject="00000000-0000-0000-0000-000000000001", principal_type="anonymous"),
        secret="test-secret",
        algorithm="HS256",
        expire_minutes=30,
    )
    principal = decode_principal_token(token, "test-secret", "HS256")
    assert principal.principal_type == "anonymous"
    assert principal.subject.endswith("0001")


def test_invalid_optional_bearer_is_not_treated_as_anonymous(client):
    response = client.get("/api/qa/conversations", headers={"Authorization": "Bearer broken"})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"
```

- [ ] **Step 2: Run tests and verify RED**

Run `python -m pytest backend/tests/test_auth_principal.py -q`.

Expected: import/endpoint failures because Principal support does not exist.

- [ ] **Step 3: Implement `Principal` and JWT helpers**

Create the immutable model and helpers:

```python
@dataclass(frozen=True)
class Principal:
    subject: str
    principal_type: Literal["anonymous", "user"]
    username: str = ""

    @property
    def user_id(self) -> str | None:
        return self.subject if self.principal_type == "user" else None

    @property
    def anonymous_id(self) -> str | None:
        return self.subject if self.principal_type == "anonymous" else None
```

`create_principal_token()` must write `sub`, `principal_type`, `preferred_username`, `iat`, and `exp`. `decode_principal_token()` must validate the configured algorithm and reject unknown principal types. A verified legacy token with no `principal_type` but a `preferred_username` is treated as `user` during the migration window.

- [ ] **Step 4: Add stable errors and request IDs**

Define:

```python
@dataclass(frozen=True)
class ApiError:
    code: str
    message: str


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
```

Register an `HTTPException` handler that returns `{"success": False, "code": ..., "message": ..., "request_id": ...}` and middleware that accepts a valid `X-Request-ID` or generates a UUID, then echoes it in the response header.

- [ ] **Step 5: Add configuration and endpoint models**

Add `anonymous_jwt_expire_minutes` with default `43200`. Add:

```python
class PrincipalTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    principal_type: Literal["anonymous", "user"]
    subject: str
    expires_in: int
```

Remove `user_id` and `anonymous_id` from `TravelQARequest`; Pydantic's default extra-field behavior preserves backward-compatible request parsing without trusting the values.

- [ ] **Step 6: Add `POST /api/auth/anonymous` and migrate user tokens**

The endpoint generates `uuid.uuid4()`, signs an anonymous principal, and returns `PrincipalTokenResponse`. Registration/login call `create_principal_token(Principal(..., "user"), ...)`. Replace the old optional-user dependency on QA endpoints with `get_current_principal`.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
python -m pytest backend/tests/test_auth_principal.py backend/tests/test_trip_planner.py -q
```

Expected: all selected tests pass, with only the explicitly marked LangSmith test skipped.

Commit: `feat: 增加统一匿名和用户身份令牌`.

## Task 3: Enforce QA conversation ownership and secure anonymous merging

**Files:**
- Create: `backend/tests/test_qa_authorization.py`
- Modify: `backend/app/storage/qa_store.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/domain/models.py`
- Modify: `backend/app/auth/service.py`

- [ ] **Step 1: Write failing cross-owner tests**

Cover PostgreSQL SQL parameters through the existing fake cursor and cover the in-memory store behavior:

```python
def test_conversation_detail_is_hidden_from_other_principal(client, anonymous_headers):
    first = client.post("/api/qa/ask", json={"question": "南京怎么玩？"}, headers=anonymous_headers[0])
    conversation_id = first.json()["data"]["conversation_id"]
    response = client.get(f"/api/qa/conversations/{conversation_id}", headers=anonymous_headers[1])
    assert response.status_code == 404


def test_client_supplied_anonymous_id_is_ignored(client, anonymous_headers):
    response = client.post(
        "/api/qa/ask",
        json={"question": "北京怎么玩？", "anonymous_id": "attacker-choice"},
        headers=anonymous_headers[0],
    )
    assert response.status_code == 200
    assert response.json()["data"]["conversation_id"]
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest backend/tests/test_qa_authorization.py -q`.

Expected: another principal can currently access the conversation or identity remains client-controlled.

- [ ] **Step 3: Make store methods owner-scoped**

Use explicit owner arguments on every method:

```python
def get_or_create_conversation(self, *, conversation_id, user_id, anonymous_id, title): ...
def get_recent_messages(self, conversation_id, *, user_id, anonymous_id, limit=8): ...
def append_message(self, conversation_id, role, content, *, user_id, anonymous_id, **message_data): ...
def list_conversations(self, *, user_id, anonymous_id, limit=50): ...
def get_conversation(self, conversation_id, *, user_id, anonymous_id): ...
```

All SQL reads use one of these mutually exclusive predicates:

```sql
WHERE id = %s AND user_id = %s AND anonymous_id IS NULL
WHERE id = %s AND anonymous_id = %s AND user_id IS NULL
```

`append_message` first locks and verifies the owned conversation in the same transaction. Apply identical ownership behavior to `InMemoryQAConversationStore`.

- [ ] **Step 4: Drive API identity only from Principal**

`_prepare_qa_memory` and `_persist_qa_exchange` receive a Principal and pass `principal.user_id` / `principal.anonymous_id`. Conversation list has no `user_id` or `anonymous_id` query parameters. Detail lookup returns `404` on an ownership mismatch.

- [ ] **Step 5: Secure merge with two verified tokens**

Keep the user Bearer token in `Authorization`. Read the raw anonymous token from required `X-Anonymous-Token`, decode it with the same JWT configuration, require `principal_type=anonymous`, and merge only its `sub`. Remove `MergeAnonymousRequest`.

- [ ] **Step 6: Verify and commit**

Run `python -m pytest backend/tests/test_qa_authorization.py backend/tests/test_trip_planner.py -q`.

Commit: `fix: 强制校验问答会话资源归属`.

## Task 4: Bind trip reports to their creator

**Files:**
- Create: `backend/tests/test_report_authorization.py`
- Modify: `backend/app/storage/report_store.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing owner tests**

Test save/list/detail/recalculate/photo-update behavior for two principals and verify legacy ownerless reports are not returned.

- [ ] **Step 2: Verify RED**

Run `python -m pytest backend/tests/test_report_authorization.py -q`.

- [ ] **Step 3: Migrate the report schema**

Add nullable columns for existing installations:

```sql
ALTER TABLE trip_reports ADD COLUMN IF NOT EXISTS owner_type text;
ALTER TABLE trip_reports ADD COLUMN IF NOT EXISTS owner_id text;
CREATE INDEX IF NOT EXISTS idx_trip_reports_owner_created
    ON trip_reports (owner_type, owner_id, created_at DESC);
```

New reports always provide `owner_type` and `owner_id`. Do not expose rows where either is null.

- [ ] **Step 4: Scope store methods and endpoints**

Use signatures:

```python
def save_report(self, request, result, *, owner_type: str, owner_id: str) -> dict: ...
def list_reports(self, *, owner_type: str, owner_id: str, limit: int = 50): ...
def get_report(self, report_id: str, *, owner_type: str, owner_id: str): ...
def save_revision(self, report_id: str, ..., *, owner_type: str, owner_id: str): ...
```

Planning, report list/detail, recalculation, and `report_id` image writes require a Principal. Before image mutation, fetch the owned report; return `404` if unavailable.

- [ ] **Step 5: Verify and commit**

Run `python -m pytest backend/tests/test_report_authorization.py backend/tests/test_trip_planner.py -q`.

Commit: `fix: 限制旅行报告仅由创建者访问`.

## Task 5: Add Redis configuration, health and distributed rate limiting

**Files:**
- Create: `backend/app/core/redis_client.py`
- Create: `backend/app/core/rate_limit.py`
- Create: `backend/tests/test_rate_limit.py`
- Create: `backend/.env.example`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/requirements.txt`
- Modify: `backend/constraints.txt`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add dependency declarations without credentials**

Add `redis>=7.4.0,<8.0.0` to requirements and `redis==7.4.0` to constraints. This matches the currently installed, Python 3.11-compatible client and avoids the redis-py 8 default-protocol migration during the hardening change. Document only placeholders in `.env.example`; never write the supplied real password.

- [ ] **Step 2: Write failing limiter tests**

Use a deterministic fake implementing `eval`, `ping`, and `close`. Verify per-principal isolation, limit exhaustion, TTL-derived `Retry-After`, fail-open QA behavior, and fail-closed knowledge/auth behavior.

- [ ] **Step 3: Verify RED**

Run `python -m pytest backend/tests/test_rate_limit.py -q`.

- [ ] **Step 4: Implement Redis client creation**

`create_redis_client(settings)` uses `redis.Redis.from_url` when `REDIS_URL` is present, otherwise supplies host, port, password and db separately. Set `decode_responses=True`, `socket_connect_timeout`, `socket_timeout`, `health_check_interval=30`, and a bounded `ConnectionPool(max_connections=20)`.

- [ ] **Step 5: Implement the atomic fixed-window limiter**

Use this Lua operation:

```lua
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('TTL', KEYS[1])}
```

Hash the raw subject/IP before using it in the Redis key. `enforce()` returns remaining count or raises `429 RATE_LIMITED` with `Retry-After`. On Redis exceptions, return only when the policy is fail-open; otherwise raise `503 REDIS_UNAVAILABLE`.

- [ ] **Step 6: Apply policies**

Define explicit policies in code: anonymous issue `20/min/IP`, register/login `10/min/IP` fail-closed, QA `20/min/principal`, planning `5/min/principal`, map/photo `60/min/principal`, knowledge reads `30/min/user` fail-closed, knowledge writes `5/min/user` fail-closed. Make values configurable through environment variables.

- [ ] **Step 7: Add Redis health and lifecycle**

Add the Redis client/limiter to `AppResources`, close it during lifespan shutdown, and expose only `enabled` and `ok` in `/api/health`—never host, password or exception details.

- [ ] **Step 8: Verify and commit**

Run `python -m pytest backend/tests/test_rate_limit.py backend/tests/test_auth_principal.py -q`.

Commit: `feat: 使用Redis实现分布式接口限流`.

## Task 6: Move knowledge ingestion job state to Redis

**Files:**
- Create: `backend/app/knowledge/job_store.py`
- Create: `backend/tests/test_knowledge_job_store.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`

- [ ] **Step 1: Write failing job-store tests**

Verify create/get/update, TTL refresh, JSON-safe result storage, no request content key, and Redis failure mapping.

- [ ] **Step 2: Verify RED**

Run `python -m pytest backend/tests/test_knowledge_job_store.py -q`.

- [ ] **Step 3: Implement Redis hashes**

Use key `travel-assistant:knowledge-job:{job_id}` and fields `job_id`, `status`, `message`, `source_type`, `result_json`, `error_code`, `created_at`, `updated_at`. Set a configurable default TTL of seven days. Never serialize the ingestion request or document content.

- [ ] **Step 4: Replace global dictionary state**

Remove `knowledge_ingest_jobs` and its thread lock. `create_knowledge_ingest_job`, `update_knowledge_ingest_job`, and the status endpoint delegate to `RedisKnowledgeJobStore`. Keep actual execution in `BackgroundTasks` for this phase.

- [ ] **Step 5: Verify and commit**

Run `python -m pytest backend/tests/test_knowledge_job_store.py backend/tests/test_travel_vector_store.py -q`.

Commit: `feat: 使用Redis持久化知识入库任务状态`.

## Task 7: Protect knowledge administration and block SSRF

**Files:**
- Create: `backend/app/security/__init__.py`
- Create: `backend/app/security/url_fetcher.py`
- Create: `backend/tests/test_safe_url_fetcher.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`

- [ ] **Step 1: Write failing URL-policy tests**

Parametrize rejected inputs: `file://`, embedded credentials, `localhost`, `127.0.0.1`, RFC1918 IPv4, link-local IPv4/IPv6, multicast, reserved addresses, redirect responses, binary content and bodies beyond the configured limit. Add one allowed public HTTPS text response.

- [ ] **Step 2: Verify RED**

Run `python -m pytest backend/tests/test_safe_url_fetcher.py -q`.

- [ ] **Step 3: Implement URL validation**

`validate_public_http_url(url, resolver=socket.getaddrinfo)` must require HTTP/HTTPS, hostname, no username/password, and every resolved address must satisfy `ipaddress.ip_address(address).is_global`. Reject the complete request if any address is non-global.

- [ ] **Step 4: Implement bounded streaming fetch**

Use `httpx.Client(follow_redirects=False, timeout=...)` and `client.stream("GET", ...)`. Reject all 3xx, permit only `text/html`, `text/plain`, and `application/xhtml+xml`, stop once accumulated bytes exceed `URL_FETCH_MAX_BYTES` (default 2 MiB), then decode using the declared encoding or UTF-8 replacement.

- [ ] **Step 5: Require a user principal on knowledge endpoints**

Apply `require_user_principal` and fail-closed rate-limit policies to news ingestion, all knowledge ingestion variants, job creation/status and raw knowledge search. Ordinary QA retrieval remains available to anonymous principals through `/api/qa/*`.

Add integration assertions that anonymous tokens receive `403 AUTH_USER_REQUIRED`, valid user tokens reach the injected knowledge service, and missing tokens receive `401 AUTH_REQUIRED`.

- [ ] **Step 6: Replace direct `httpx.get` and stabilize errors**

Map policy, timeout, size, content-type and upstream-status errors to stable codes without returning raw exception strings.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
python -m pytest backend/tests/test_safe_url_fetcher.py backend/tests/test_travel_vector_store.py backend/tests/test_qa_authorization.py -q
```

Commit: `fix: 加固知识接口权限和URL抓取安全`.

## Task 8: Bootstrap anonymous identity in the frontend

**Files:**
- Modify: `frontend/src/services/api.ts`
- Modify: `frontend/src/services/auth.ts`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/views/QA.vue`
- Modify: `frontend/src/types/index.ts`

- [ ] **Step 1: Add frontend token types and API calls**

Add `PrincipalTokenResponse` with `principal_type`, `subject`, `access_token`, `token_type`, `expires_in`. Add `issueAnonymousToken()` and change `mergeAnonymousSessions()` to accept the previous anonymous token and send it in `X-Anonymous-Token`.

- [ ] **Step 2: Implement bootstrap before app mount**

Export `ensurePrincipalToken()` from `auth.ts`. It returns the existing token or calls `/api/auth/anonymous`, persists the result, and keeps `user=null` for anonymous principals. In `main.ts`, call it before `app.mount`.

- [ ] **Step 3: Preserve the anonymous token across login/register**

Capture the current token before persisting the user token. After successful login/register, call merge with the captured anonymous token. Remove `travel_qa_anonymous_id` generation and storage.

- [ ] **Step 4: Fix UI authentication semantics**

`isAuthenticated` remains `user !== null`; router guest redirects check stored user data rather than mere token presence. QA requests and conversation list calls stop sending client-chosen user/anonymous IDs. SSE continues to read the shared Bearer token.

- [ ] **Step 5: Verify and commit**

Do not introduce a JavaScript test runner in this phase. Run `npm run build` for static verification and run `python -m pytest backend/tests/test_auth_principal.py backend/tests/test_qa_authorization.py -q` for the API contract exercised by the frontend.

Commit: `feat: 前端启动时获取服务端匿名身份`.

## Task 9: Repair runtime configuration and Docker builds

**Files:**
- Create: `frontend/public/config.js`
- Create: `frontend/docker-entrypoint.sh`
- Modify: `frontend/Dockerfile`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/tests/test-entrypoint.sh`

- [ ] **Step 1: Add a failing entrypoint smoke test**

The test builds the frontend image, runs it with an API URL containing characters that require JSON escaping, fetches `/config.js`, and asserts it parses after removing the assignment prefix/suffix.

- [ ] **Step 2: Add default development configuration**

Create `public/config.js` containing exactly:

```javascript
window.__APP_CONFIG__ = {};
```

- [ ] **Step 3: Generate runtime config safely**

Install `jq` in the Nginx stage. `docker-entrypoint.sh` uses:

```sh
#!/bin/sh
set -eu
target=/usr/share/nginx/html/config.js
printf 'window.__APP_CONFIG__ = ' > "$target"
jq -cn \
  --arg apiBaseUrl "${VITE_API_BASE_URL:-}" \
  --arg apiTimeoutMs "${VITE_API_TIMEOUT_MS:-300000}" \
  '{API_BASE_URL:$apiBaseUrl,API_TIMEOUT_MS:$apiTimeoutMs}' >> "$target"
printf ';\n' >> "$target"
exec "$@"
```

- [ ] **Step 4: Remove the empty manual chunk**

Delete the fixed `maps` entry from `manualChunks` and remove duplicate `@ts-ignore` comments. Keep the Ant Design chunk optimization for a later phase.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
npm run build
docker build -t travel-assistant-frontend-phase1 ./frontend
```

Expected: no missing `config.js`, missing entrypoint or empty `maps` chunk warnings; Docker build exits 0.

Commit: `fix: 修复前端运行时配置和容器构建`.

## Task 10: Gate deployment on backend, frontend and Docker verification

**Files:**
- Modify: `.github/workflows/deploy-frontend-pages.yml`
- Modify: `README.md`

- [ ] **Step 1: Extend workflow triggers and verification jobs**

Add `pull_request` and jobs `backend-test`, `frontend-test`, and `docker-smoke`. Backend uses Python 3.11 and `pip install -r backend/requirements.txt`; frontend uses Node 22 and `npm ci`; Docker smoke builds both images. Tests do not connect to the supplied external Redis—unit fakes and `RATE_LIMIT_ENABLED=false` are used in test configuration.

- [ ] **Step 2: Gate deployment jobs**

The Pages `build` job runs only outside pull requests and declares:

```yaml
needs: [backend-test, frontend-test, docker-smoke]
if: github.event_name != 'pull_request'
```

The `deploy` job continues to depend on `build`.

- [ ] **Step 3: Document environment variables without real credentials**

Document Redis variable names, the current lack of TLS, security-group restriction, anonymous JWT lifetime, rate limits and URL-fetch bounds. Do not include the actual host password in README or examples.

- [ ] **Step 4: Run fresh full verification**

Run:

```powershell
python -m pytest backend/tests -q
npm run build
docker build -t travel-assistant-backend-phase1 ./backend
docker build -t travel-assistant-frontend-phase1 ./frontend
git diff --check
git status --short
```

Expected: backend zero failures with only intentional LangSmith skip; frontend and both Docker builds exit 0; no whitespace errors; only planned files modified.

- [ ] **Step 5: Review secret hygiene**

Run:

```powershell
git diff --cached
git grep -n "REDIS_PASSWORD\|redis://" -- ':!docs/superpowers/*'
```

Expected: no supplied Redis password and no credential-bearing Redis URL in tracked content.

- [ ] **Step 6: Commit**

Commit: `ci: 增加生产化改造验证门禁`.

## Plan self-review checklist

- Every approved specification requirement maps to Tasks 1–10.
- Identity and ownership changes precede rate limiting and job-state migration.
- No task stores Redis credentials or sensitive user/document content in Redis.
- Every behavior change starts with a failing test except pure build/config files, which use build smoke tests.
- Final verification includes backend, frontend, both Docker images, whitespace and secret hygiene.
