# Admin RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add database-authoritative `user/admin` RBAC so every knowledge-management endpoint and page is restricted to current administrators, with local promote/demote/show commands and persistent role-change audit.

**Architecture:** Extend the existing auth schema and service instead of introducing a general policy engine. JWT remains the identity proof, while `require_admin_principal` resolves the user ID against PostgreSQL for every admin request. The frontend role is presentation state refreshed from `/api/auth/me`; it is never a server authorization source.

**Tech Stack:** FastAPI, Pydantic 2, psycopg 3, python-jose, pytest, Vue 3, TypeScript, Axios, Vite.

---

## File map

- Modify `backend/app/auth/service.py`: auth-schema migration, database role reads, role mutation transaction, audit reads, current-user and admin dependencies.
- Create `backend/app/auth/admin_cli.py`: local `promote`, `demote`, and `show` command entrypoint.
- Modify `backend/app/domain/models.py`: shared `UserRole`, role-bearing auth response.
- Modify `backend/app/main.py`: role-aware auth payloads and admin-only knowledge dependencies.
- Create `backend/tests/test_admin_rbac.py`: schema, service, dependency, auth response, CLI and fail-closed tests.
- Modify `backend/tests/test_knowledge_authorization.py`: complete knowledge/news endpoint admin matrix.
- Modify `frontend/src/types/index.ts`: role-bearing auth types.
- Modify `frontend/src/services/authSession.ts`: secure old-cache role normalization.
- Modify `frontend/src/services/auth.ts`: login/register role persistence and `/auth/me` refresh.
- Modify `frontend/src/main.ts`: `requiresAdmin` live route guard.
- Modify `frontend/src/App.vue`: admin-only knowledge navigation and non-blocking identity refresh.
- Modify `backend/tests/test_frontend_auth_contract.py`: frontend RBAC source-contract coverage.
- Modify `README.md`: local role-management and deployment verification instructions.

## Task 1: Add role schema, role storage and persistent audit

**Files:**
- Modify: `backend/app/auth/service.py`
- Modify: `backend/app/domain/models.py`
- Create: `backend/tests/test_admin_rbac.py`

- [ ] **Step 1: Write failing schema and role-store tests**

Create fake cursor/connection/manager helpers that record SQL and return configured rows. Add tests proving:

```python
def test_auth_schema_migration_adds_default_role_constraint_and_audit_table():
    manager = RecordingConnectionManager()
    migrate_auth_schema(manager)
    sql = "\n".join(manager.cursor.executed_sql)
    assert "ADD COLUMN IF NOT EXISTS role" in sql
    assert "DEFAULT 'user'" in sql
    assert "CHECK (role IN ('user', 'admin'))" in sql
    assert "CREATE TABLE IF NOT EXISTS user_role_audit" in sql


def test_create_user_always_returns_default_user_role():
    manager = RowConnectionManager({"id": "user-1", "username": "alice", "role": "user"})
    user = create_user(manager, "alice", "hash")
    assert user["role"] == "user"


def test_change_user_role_updates_and_audits_in_one_transaction():
    manager = RoleChangeConnectionManager(current_role="user")
    result = change_user_role(manager, "alice", "admin", changed_by="deploy")
    assert result.previous_role == "user"
    assert result.new_role == "admin"
    assert result.changed is True
    assert manager.connection.committed is True
    assert manager.cursor.audit_values == ("user-1", "alice", "user", "admin", "deploy")


def test_repeating_same_role_is_idempotent_and_does_not_audit():
    manager = RoleChangeConnectionManager(current_role="admin")
    result = change_user_role(manager, "alice", "admin", changed_by="deploy")
    assert result.changed is False
    assert manager.cursor.audit_values is None
```

Also cover invalid role rejection, unknown username, rollback on audit failure, role-bearing `get_user_by_username/get_user_by_id`, and newest-first bounded audit reads.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py -q
```

Expected: collection/import failures for `migrate_auth_schema`, `change_user_role`, `RoleChangeResult`, and role-aware query behavior because the RBAC store does not exist.

- [ ] **Step 3: Implement the minimal schema and role store**

In `domain/models.py` add:

```python
UserRole = Literal["user", "admin"]


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    role: UserRole
```

In `auth/service.py`:

- make `USERS_TABLE_SQL` include `role text NOT NULL DEFAULT 'user'`;
- add idempotent `USERS_ROLE_MIGRATION_SQL` with a named `users_role_check` constraint;
- add `USER_ROLE_AUDIT_TABLE_SQL` with the approved fields and role checks;
- extract `migrate_auth_schema(connections)` that raises on failure;
- keep `ensure_users_table(connections)` as the lifespan fail-soft wrapper around `migrate_auth_schema`;
- include `role` in all user SELECT/INSERT RETURNING queries;
- define:

```python
@dataclass(frozen=True)
class RoleChangeResult:
    user_id: str
    username: str
    previous_role: str
    new_role: str
    changed: bool


class UserRoleNotFound(LookupError):
    pass


def change_user_role(
    connections: DatabaseConnectionManager,
    username: str,
    new_role: str,
    *,
    changed_by: str,
) -> RoleChangeResult:
    if new_role not in {"user", "admin"}:
        raise ValueError("unsupported user role")
    actor = changed_by.strip() or "unknown"
    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            user = cur.execute(
                "SELECT id::text, username, role FROM users WHERE username = %s FOR UPDATE",
                (username,),
            ).fetchone()
            if user is None:
                raise UserRoleNotFound(username)
            previous_role = str(user["role"])
            if previous_role == new_role:
                return RoleChangeResult(str(user["id"]), str(user["username"]), previous_role, new_role, False)
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user["id"]))
            cur.execute(
                """INSERT INTO user_role_audit
                   (user_id, username, previous_role, new_role, changed_by)
                   VALUES (%s, %s, %s, %s, %s)""",
                (user["id"], user["username"], previous_role, new_role, actor),
            )
            return RoleChangeResult(str(user["id"]), str(user["username"]), previous_role, new_role, True)


def list_user_role_audit(
    connections: DatabaseConnectionManager,
    user_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 100))
    with connections.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """SELECT previous_role, new_role, changed_by, changed_at
                   FROM user_role_audit WHERE user_id = %s
                   ORDER BY changed_at DESC LIMIT %s""",
                (user_id, bounded_limit),
            ).fetchall()
    return [dict(row) for row in rows]
```

`change_user_role` must validate the role before SQL, lock the user with `FOR UPDATE`, return unchanged without INSERT when roles match, and execute UPDATE plus audit INSERT within the same connection transaction.

- [ ] **Step 4: Run focused and existing auth tests**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py backend/tests/test_auth_principal.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add backend/app/auth/service.py backend/app/domain/models.py backend/tests/test_admin_rbac.py
git commit -m "feat: 增加用户角色和变更审计"
```

## Task 2: Add database-authoritative admin dependency and auth responses

**Files:**
- Modify: `backend/app/auth/service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_admin_rbac.py`

- [ ] **Step 1: Write failing dependency and auth-response tests**

Add a small FastAPI dependency app and tests for:

```python
def test_admin_dependency_allows_database_admin_with_existing_jwt():
    lookup = FakeRoleLookup({"user-1": {"id": "user-1", "username": "alice", "role": "admin"}})
    response = admin_client(lookup).get("/admin", headers=user_headers("user-1"))
    assert response.status_code == 200


def test_admin_dependency_rejects_user_and_old_token_after_demotion():
    lookup = FakeRoleLookup({"user-1": {"id": "user-1", "username": "alice", "role": "admin"}})
    token_headers = user_headers("user-1")
    assert admin_client(lookup).get("/admin", headers=token_headers).status_code == 200
    lookup.users["user-1"]["role"] = "user"
    response = admin_client(lookup).get("/admin", headers=token_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ADMIN_REQUIRED"


def test_admin_dependency_fails_closed_when_role_store_is_unavailable():
    response = admin_client(FailingRoleLookup()).get("/admin", headers=user_headers("user-1"))
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_ROLE_CHECK_UNAVAILABLE"
```

Also test anonymous rejection remains `AUTH_USER_REQUIRED`, missing database user is `AUTH_ADMIN_REQUIRED`, `/api/auth/register` returns role `user`, login returns the stored role, `/api/auth/me` returns the database-current role, and a deleted user token is invalidated rather than trusted from JWT claims.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py -q
```

Expected: failures because `require_admin_principal`, database-current `/auth/me`, and role response fields are not wired.

- [ ] **Step 3: Implement admin and current-user dependencies**

In `auth/service.py` add:

```python
def get_current_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    principal = require_user_principal(get_current_principal(authorization))
    try:
        user = get_user_by_id(get_auth_connections(), principal.subject)
    except Exception as exc:
        raise api_error(503, "AUTH_ROLE_CHECK_UNAVAILABLE", "无法确认当前用户权限") from exc
    if user is None:
        raise api_error(401, "AUTH_TOKEN_INVALID", "认证令牌已过期或无效")
    return {"user_id": user["id"], "username": user["username"], "role": user["role"]}


def require_admin_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    principal = require_user_principal(principal)
    try:
        user = get_user_by_id(get_auth_connections(), principal.subject)
    except Exception as exc:
        raise api_error(503, "AUTH_ROLE_CHECK_UNAVAILABLE", "无法确认管理员权限") from exc
    if user is None or user.get("role") != "admin":
        raise api_error(403, "AUTH_ADMIN_REQUIRED", "该操作需要管理员权限")
    return principal
```

Update `get_current_user_optional` to use the same database-current user mapping when a token is present. In `main.py`, include `role=user["role"]` in register/login `AuthTokenResponse`, and return role from `/api/auth/me`.

- [ ] **Step 4: Run focused authorization tests**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py backend/tests/test_auth_principal.py backend/tests/test_qa_authorization.py -q
```

Expected: all selected tests pass and anonymous/user ownership behavior remains unchanged.

- [ ] **Step 5: Commit Task 2**

```powershell
git add backend/app/auth/service.py backend/app/main.py backend/tests/test_admin_rbac.py
git commit -m "feat: 增加数据库实时管理员鉴权"
```

## Task 3: Protect the complete knowledge-management surface

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_knowledge_authorization.py`

- [ ] **Step 1: Replace the existing user-success test with an admin matrix that fails**

Keep `MANAGED_ENDPOINTS` as the single inventory. Add an autouse role lookup fixture and parameterized assertions:

```python
@pytest.mark.parametrize(("method", "path", "payload"), MANAGED_ENDPOINTS)
def test_knowledge_management_endpoints_reject_regular_user(method, path, payload, regular_user_role):
    response = TestClient(main_module.app).request(method, path, json=payload, headers=auth_headers("user", "user-1"))
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_ADMIN_REQUIRED"


def test_admin_principal_reaches_knowledge_business_layer(monkeypatch, admin_role):
    calls = []

    class Store:
        def ingest_document(self, **kwargs):
            calls.append(kwargs)
            return {"doc_id": "doc-1", "chunks_added": 1}

    resources = main_module.get_app_resources()
    monkeypatch.setattr(main_module, "travel_vector_store", Store())
    monkeypatch.setattr(resources, "travel_vector_store", main_module.travel_vector_store)
    response = TestClient(main_module.app).post(
        "/api/knowledge/documents",
        headers=auth_headers("user", "admin-1"),
        json={"title": "Guide", "content": "Managed guide"},
    )
    assert response.status_code == 200
    assert calls[0]["title"] == "Guide"
```

Update the private-URL, limiter-subject and news-error tests to use an admin database role. Preserve unauthenticated `AUTH_REQUIRED` and anonymous `AUTH_USER_REQUIRED` coverage.

- [ ] **Step 2: Run the knowledge authorization file and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_knowledge_authorization.py -q
```

Expected: regular-user endpoint cases currently reach the business layer or fail with a non-RBAC response.

- [ ] **Step 3: Require administrators in both knowledge limiter dependencies**

Import `require_admin_principal` from `app.auth.service`. Change only:

```python
def _limit_knowledge_read(
    http_request: Request,
    principal: Principal = Depends(require_admin_principal),
):
    return _enforce_rate_limit("knowledge_read", http_request, principal)


def _limit_knowledge_write(
    http_request: Request,
    principal: Principal = Depends(require_admin_principal),
):
    return _enforce_rate_limit("knowledge_write", http_request, principal)
```

All listed endpoints already depend on one of these two boundaries, so do not scatter duplicate role checks into each endpoint.

- [ ] **Step 4: Run knowledge, rate-limit and SSRF regressions**

Run:

```powershell
python -m pytest backend/tests/test_knowledge_authorization.py backend/tests/test_rate_limit.py backend/tests/test_safe_url_fetcher.py backend/tests/test_news_fetch_security.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add backend/app/main.py backend/tests/test_knowledge_authorization.py
git commit -m "fix: 限制知识管理仅管理员访问"
```

## Task 4: Add the local administrator CLI

**Files:**
- Create: `backend/app/auth/admin_cli.py`
- Modify: `backend/tests/test_admin_rbac.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI tests**

Call `main(["promote", "alice"], settings_loader=fake_settings, manager_factory=fake_manager, actor_loader=fake_actor)` directly so tests use real argument parsing and command behavior without a real database:

```python
def test_admin_cli_promote_uses_os_actor_and_reports_change(capsys):
    code = admin_cli.main(
        ["promote", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://configured"),
        manager_factory=lambda _url: manager,
        actor_loader=lambda: "deploy-user",
    )
    assert code == 0
    assert "username=alice role=admin changed=true" in capsys.readouterr().out


def test_admin_cli_show_outputs_current_role_and_recent_audit(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli, "get_user_by_username", lambda _manager, _name: {
        "id": "user-1", "username": "alice", "role": "admin"
    })
    monkeypatch.setattr(admin_cli, "list_user_role_audit", lambda _manager, _id: [{
        "previous_role": "user", "new_role": "admin", "changed_by": "deploy-user",
        "changed_at": "2026-07-15T00:00:00Z",
    }])
    code = admin_cli.main(
        ["show", "alice"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://configured"),
        manager_factory=lambda _url: manager,
    )
    assert code == 0
    assert "username=alice role=admin" in capsys.readouterr().out


def test_admin_cli_unknown_user_returns_two_without_secrets(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli, "change_user_role", Mock(side_effect=UserRoleNotFound("missing")))
    code = admin_cli.main(
        ["promote", "missing"],
        settings_loader=lambda: SimpleNamespace(database_url="postgresql://configured"),
        manager_factory=lambda _url: manager,
        actor_loader=lambda: "deploy-user",
    )
    assert code == 2
    assert "postgresql://" not in capsys.readouterr().err
```

Also test `demote`, idempotent change output, missing `DATABASE_URL`, database exceptions returning 1, manager close in all paths, and no password/JWT/connection details in output.

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py -k cli -q
```

Expected: import failure because `app.auth.admin_cli` does not exist.

- [ ] **Step 3: Implement the CLI with injected seams**

Use `argparse` subcommands with a required username. Define:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage travel-assistant administrator roles")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("promote", "demote", "show"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("username")
    return parser


def format_audit(audit: Mapping[str, Any]) -> str:
    return (
        f"changed_at={audit['changed_at']} previous_role={audit['previous_role']} "
        f"new_role={audit['new_role']} changed_by={audit['changed_by']}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_loader=get_settings,
    manager_factory=DatabaseConnectionManager,
    actor_loader=getpass.getuser,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings_loader()
    if not settings.database_url:
        print("database configuration is required", file=sys.stderr)
        return 2
    manager = manager_factory(settings.database_url)
    try:
        migrate_auth_schema(manager)
        if args.command == "show":
            user = get_user_by_username(manager, args.username)
            if user is None:
                raise UserRoleNotFound(args.username)
            print(f"username={user['username']} role={user['role']}")
            for audit in list_user_role_audit(manager, str(user["id"])):
                print(format_audit(audit))
            return 0
        target_role = "admin" if args.command == "promote" else "user"
        result = change_user_role(
            manager,
            args.username,
            target_role,
            changed_by=(actor_loader() or "unknown").strip() or "unknown",
        )
        print(f"username={result.username} role={result.new_role} changed={str(result.changed).lower()}")
        return 0
    except UserRoleNotFound:
        print("user was not found", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.error("Admin role command failed exception_type=%s", type(exc).__name__)
        print("administrator role command failed", file=sys.stderr)
        return 1
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

The command must call `migrate_auth_schema`, never echo the database URL, normalize an empty actor to `unknown`, close the manager in `finally`, and use exit codes 0 (success/no change), 2 (unknown user or missing database configuration), and 1 (database failure).

Document exact promote/demote/show commands, server-shell-only use, audit behavior, and post-change browser refresh in README. Do not document real connection values.

- [ ] **Step 4: Run CLI and auth tests**

Run:

```powershell
python -m pytest backend/tests/test_admin_rbac.py -q
python -m app.auth.admin_cli --help
```

Expected: tests pass and help lists `promote`, `demote`, `show` without opening a database connection.

- [ ] **Step 5: Commit Task 4**

```powershell
git add backend/app/auth/admin_cli.py backend/tests/test_admin_rbac.py README.md
git commit -m "feat: 增加本地管理员角色命令"
```

## Task 5: Make frontend administrator state fail closed and live-refreshed

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/services/authSession.ts`
- Modify: `frontend/src/services/auth.ts`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/App.vue`
- Modify: `backend/tests/test_frontend_auth_contract.py`

- [ ] **Step 1: Write failing frontend RBAC contract tests**

Add source-level contract assertions consistent with the existing no-JS-runner test strategy:

```python
def test_frontend_auth_user_role_defaults_fail_closed_for_legacy_cache():
    types_source = _read("types/index.ts")
    session_source = _read("services/authSession.ts")
    assert "role: 'user' | 'admin'" in types_source
    assert re.search(r"parsed\.role === 'admin' \? 'admin' : 'user'", session_source)


def test_knowledge_navigation_and_route_require_live_admin_role():
    app_source = _read("App.vue")
    main_source = _read("main.ts")
    auth_source = _read("services/auth.ts")
    assert "auth.isAdmin.value" in app_source
    assert re.search(r"/knowledge.*requiresAdmin", main_source)
    assert "refreshCurrentUser" in main_source
    assert "getAuthMe" in auth_source
```

Also assert login/register persist `result.role`, app bootstrap refresh is non-blocking, a 503 refresh failure does not clear the user, and an admin-required route redirects to the QA/home route with a visible permission message.

- [ ] **Step 2: Run the frontend contract tests and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_frontend_auth_contract.py -q
```

Expected: failures for missing role type, admin route metadata, refresh function and admin-only nav.

- [ ] **Step 3: Implement role-aware frontend state**

Add `role` to `AuthTokenResponse` and `AuthUser`. Normalize cached users with:

```typescript
const role: AuthUser['role'] = parsed.role === 'admin' ? 'admin' : 'user'
return { user_id: parsed.user_id, username: parsed.username, role }
```

Persist `result.role` after login/register. In `useAuth` add:

```typescript
const isAdmin = computed(() => isAuthenticated.value && user.value?.role === 'admin')

async function refreshCurrentUser(): Promise<AuthUser | null> {
  const accessToken = getStoredUserToken()
  if (!accessToken || !getStoredUser()) return null
  const current = await getAuthMe()
  persistUserPrincipal(accessToken, current)
  return current
}
```

Expose both values. In `main.ts`, make the guard async; for `requiresAdmin`, require a stored user, call `refreshCurrentUser`, allow only role `admin`, and otherwise show `message.warning('需要管理员权限')` and return `{ name: 'QA' }`. Let 401 use existing token invalidation; on non-401 errors show a service-unavailable message and cancel navigation without clearing login state.

Mark the knowledge route `meta: { requiresAdmin: true }`. In `App.vue`, render its link only for `auth.isAdmin.value`, and on mount call `void auth.refreshCurrentUser().catch(() => undefined)` for authenticated users before the existing merge retry.

- [ ] **Step 4: Run frontend contract and production build**

Run:

```powershell
python -m pytest backend/tests/test_frontend_auth_contract.py backend/tests/test_auth_principal.py -q
cd frontend
npm run build
```

Expected: contract tests pass and `vue-tsc && vite build` exits 0.

- [ ] **Step 5: Commit Task 5**

```powershell
git add frontend/src/types/index.ts frontend/src/services/authSession.ts frontend/src/services/auth.ts frontend/src/main.ts frontend/src/App.vue backend/tests/test_frontend_auth_contract.py
git commit -m "feat: 前端仅向管理员开放知识库"
```

## Task 6: Full verification and documentation parity

**Files:**
- Modify only if verification finds a specification mismatch.

- [ ] **Step 1: Run the full backend suite**

```powershell
$env:RATE_LIMIT_ENABLED='false'
python -m pytest backend/tests -q
Remove-Item Env:RATE_LIMIT_ENABLED
```

Expected: zero failures and only the intentional LangSmith skip.

- [ ] **Step 2: Run frontend and static verification**

```powershell
cd frontend
npm run build
cd ..
git diff --check
git status --short
```

Expected: build and whitespace checks pass; only intentional RBAC files are modified or the worktree is clean after commits.

- [ ] **Step 3: Review authorization and secret hygiene**

Verify every route in `MANAGED_ENDPOINTS` is transitively guarded by `require_admin_principal`. Search the branch diff for non-empty database/Redis passwords, credential-bearing URLs, complete JWTs, and Authorization headers. Synthetic test fixtures must use reserved example hosts and cannot contain supplied deployment credentials.

- [ ] **Step 4: Review the completed diff**

Inspect `git diff master...HEAD`, re-run focused RBAC tests after any review fix, and confirm the implementation matches every completion criterion in `docs/superpowers/specs/2026-07-15-admin-rbac-design.md`.

- [ ] **Step 5: Commit any verification-only fixes**

If review produced a real fix, commit only its files with a focused Chinese Conventional Commit message. If no files changed, do not create an empty commit.
