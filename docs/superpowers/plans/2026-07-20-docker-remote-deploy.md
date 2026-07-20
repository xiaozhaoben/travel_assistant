# Docker Remote Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows PowerShell command that updates the server checkout from Git, uploads the local frontend/backend environment files, and rebuilds the existing Docker Compose services.

**Architecture:** A repository-level PowerShell script owns local validation, temporary environment archive creation, SCP transfer, and two SSH phases: Git bootstrap/update, then remote deployment and health checks. Docker Compose keeps current local defaults while accepting host-port overrides used by the deployment script. A Python contract test reads the real files and prevents credentials, update safety, port mapping, or cleanup behavior from regressing.

**Tech Stack:** PowerShell 5+, Windows OpenSSH (`ssh`, `scp`), GNU/Linux shell, Git, Docker Compose, Python/pytest.

---

## File structure

- Create `scripts/deploy-docker.ps1`: one-command local deployment entrypoint; SSH address and port live in the top configuration block.
- Create `backend/tests/test_deploy_docker_script.py`: static contract tests for deployment behavior without connecting to the server.
- Modify `docker-compose.yml`: parameterize only published host ports while retaining existing defaults.
- Modify `README.md`: document the deployment command, prerequisites, and interactive SSH authentication.

### Task 1: Lock the deployment contract with failing tests

**Files:**
- Create: `backend/tests/test_deploy_docker_script.py`
- Test: `backend/tests/test_deploy_docker_script.py`

- [ ] **Step 1: Write tests for Compose and script contracts**

Create tests that read `docker-compose.yml` and `scripts/deploy-docker.ps1`, asserting:

```python
def test_compose_ports_can_be_overridden_without_changing_local_defaults():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '${BACKEND_PORT:-8010}:8000' in compose
    assert '${FRONTEND_PORT:-8080}:80' in compose


def test_deploy_script_has_safe_update_and_environment_transfer_contract():
    script = (ROOT / "scripts" / "deploy-docker.ps1").read_text(encoding="utf-8")
    required = [
        '$ServerHost =', '$SshPort =', '/usr/travel/app',
        'git pull --ff-only origin master', 'backend/.env', 'frontend/.env',
        'chmod 600', 'docker compose -p travel', '--remove-orphans',
        'BACKEND_PORT=8000', 'FRONTEND_PORT=8080', '/api/health',
    ]
    for fragment in required:
        assert fragment in script
    assert '$Password =' not in script
```

Also assert that the script uses `try`/`finally`, removes the local temporary archive, and registers remote cleanup with a shell `trap`.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
python -m pytest backend/tests/test_deploy_docker_script.py -q
```

Expected: FAIL because `scripts/deploy-docker.ps1` does not exist and Compose ports are still hardcoded.

- [ ] **Step 3: Commit the failing contract test**

```powershell
git add backend/tests/test_deploy_docker_script.py
git commit -m "test: 添加 Docker 部署脚本契约测试"
```

### Task 2: Parameterize Compose host ports

**Files:**
- Modify: `docker-compose.yml`
- Test: `backend/tests/test_deploy_docker_script.py`

- [ ] **Step 1: Replace only the host-side Compose ports**

Use:

```yaml
backend:
  ports:
    - "${BACKEND_PORT:-8010}:8000"

frontend:
  ports:
    - "${FRONTEND_PORT:-8080}:80"
```

- [ ] **Step 2: Run the focused Compose contract test**

```powershell
python -m pytest backend/tests/test_deploy_docker_script.py -q
```

Expected: script-related tests remain FAIL; Compose assertion passes.

### Task 3: Implement the PowerShell deploy command

**Files:**
- Create: `scripts/deploy-docker.ps1`
- Test: `backend/tests/test_deploy_docker_script.py`

- [ ] **Step 1: Add the top-level configuration and validation**

Define the SSH host and port only once at the top. Keep repository, branch, remote directory, Compose project, and service ports as explicit deployment constants. Enable terminating errors and validate `ssh`, `scp`, `tar`, plus both local `.env` files before any remote mutation.

- [ ] **Step 2: Add temporary environment packaging and upload**

Resolve the repository root from `$PSScriptRoot`, create a uniquely named `.tar.gz` under the system temp directory, and package exactly `backend/.env` and `frontend/.env`. Upload it with `scp -P $SshPort`; never print archive contents.

- [ ] **Step 3: Add idempotent Git bootstrap/update**

The first SSH command must:

```bash
if [ -d "$REMOTE_DIR/.git" ]; then
  git -C "$REMOTE_DIR" pull --ff-only origin master
elif [ -e "$REMOTE_DIR" ]; then
  fail because the non-empty target is not the expected checkout
else
  git clone --branch master --single-branch "$GIT_REPOSITORY" "$REMOTE_DIR"
fi
```

Do not use `reset --hard`, `clean`, or overwrite server-side Git changes.

- [ ] **Step 4: Add remote environment install, Compose update, and checks**

The second SSH command must register remote archive cleanup with `trap`, extract under the checkout, run `chmod 600` for both environment files, and execute:

```bash
BACKEND_PORT=8000 FRONTEND_PORT=8080 docker compose -p travel up -d --build --remove-orphans
BACKEND_PORT=8000 FRONTEND_PORT=8080 docker compose -p travel ps
curl --fail --silent --show-error http://127.0.0.1:8000/api/health >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/ >/dev/null
```

Only print a generic success message after both HTTP checks pass.

- [ ] **Step 5: Guarantee local cleanup**

Wrap packaging, upload, and deployment in `try`/`finally`, with `Remove-Item -LiteralPath $ArchivePath -Force` in `finally` when the archive exists.

- [ ] **Step 6: Run the focused test and confirm GREEN**

```powershell
python -m pytest backend/tests/test_deploy_docker_script.py -q
```

Expected: all tests PASS.

### Task 4: Document and verify the command

**Files:**
- Modify: `README.md`
- Test: `backend/tests/test_deploy_docker_script.py`

- [ ] **Step 1: Add concise usage documentation**

Document:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy-docker.ps1
```

State that Windows OpenSSH, local environment files, server Git/Docker/curl, and server GitHub access are required. State that SSH asks interactively for authentication and the password is not stored.

- [ ] **Step 2: Parse-check the PowerShell script**

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path .\scripts\deploy-docker.ps1),
  [ref]$null,
  [ref]$errors
) | Out-Null
if ($errors.Count) { $errors | Format-List; exit 1 }
```

Expected: exit code 0 and no parser errors.

- [ ] **Step 3: Run focused tests and repository hygiene checks**

```powershell
python -m pytest backend/tests/test_deploy_docker_script.py -q
git diff --check
git status --short
```

Expected: tests PASS, `git diff --check` exits 0, and status lists only task files plus the pre-existing unrelated untracked plan.

- [ ] **Step 4: Review the exact diff without contacting production**

```powershell
git diff -- docker-compose.yml scripts/deploy-docker.ps1 backend/tests/test_deploy_docker_script.py README.md
```

Confirm that no password or `.env` contents appear. Do not execute the deployment script during local verification because that would change the production server.

- [ ] **Step 5: Commit the implementation**

```powershell
git add docker-compose.yml scripts/deploy-docker.ps1 backend/tests/test_deploy_docker_script.py README.md
git commit -m "feat: 添加 Docker 远程更新脚本"
```
