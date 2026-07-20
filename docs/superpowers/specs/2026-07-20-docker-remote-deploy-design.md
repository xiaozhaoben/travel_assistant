# 前后端 Docker 远程更新脚本设计

## 目标

在 Windows 本地通过一个 PowerShell 脚本，将 GitHub 上的最新项目代码部署到服务器，并同步本地前后端环境文件。部署沿用现有 Docker Compose 项目名 `travel`，更新 `travel-frontend-1` 和 `travel-backend-1` 两个容器。

## 部署目标

SSH 地址和 SSH 端口放在 PowerShell 脚本顶部配置区，方便直接修改。服务器项目目录固定为 `/usr/travel/app`，Git 分支固定为 `master`，Compose 项目名固定为 `travel`。前端使用宿主机端口 `8080`，后端使用宿主机端口 `8000`。

SSH 密码不写入脚本、配置、Git 或部署日志，由本机 OpenSSH 在执行时交互获取。长期使用时可改用 SSH Key。

## 文件变更

### 本地部署脚本

新增 `scripts/deploy-docker.ps1`，负责：

1. 读取脚本顶部的 SSH 地址和端口配置，并检查本地 `backend/.env`、`frontend/.env`、`ssh`、`scp` 和 `tar`。
2. 将两个 `.env` 打包到唯一的本地临时压缩包。
3. 通过 SSH 创建服务器部署目录；首次部署时克隆 `master` 分支，后续部署时使用 `git pull --ff-only origin master` 更新。
4. 将环境文件压缩包上传到服务器临时路径。
5. 解压环境文件到服务器项目的 `backend/.env` 和 `frontend/.env`，权限设为仅部署用户可读写。
6. 使用指定端口重建并启动 Compose 服务。
7. 输出容器状态，并检查前后端 HTTP 地址。
8. 无论成功或失败，清理本地和服务器临时压缩包。

脚本遇到缺少文件、Git 更新冲突、Docker 构建失败、容器启动失败或健康检查失败时立即退出，并返回非零状态。

### Docker Compose 端口参数

修改 `docker-compose.yml`，仅将宿主机端口参数化：

```yaml
backend:
  ports:
    - "${BACKEND_PORT:-8010}:8000"

frontend:
  ports:
    - "${FRONTEND_PORT:-8080}:80"
```

本地默认行为保持不变。服务器部署脚本显式传入 `BACKEND_PORT=8000` 和 `FRONTEND_PORT=8080`。

## 部署流程

首次部署：

1. 创建 `/usr/travel/app` 的父目录。
2. 将 `master` 分支克隆到 `/usr/travel/app`。
3. 上传并安装两个环境文件。
4. 执行 `docker compose -p travel up -d --build --remove-orphans`。

后续部署：

1. 确认 `/usr/travel/app` 是目标 Git 仓库。
2. 执行 fast-forward-only 更新；服务器工作区存在冲突时停止，不覆盖服务器文件。
3. 上传并替换两个环境文件。
4. 重建并更新现有 Compose 容器。

Compose 命令在服务器上以以下环境变量运行：

```bash
BACKEND_PORT=8000 FRONTEND_PORT=8080 docker compose -p travel ...
```

## 安全与失败处理

- `.env` 已被 `.gitignore` 忽略，不纳入提交。
- SSH 地址和端口集中放在脚本顶部配置区，不散落在命令中。
- 临时压缩包使用唯一名称，部署结束后两端均清理。
- 脚本不打印 `.env` 内容或 SSH 密码。
- Git 更新仅允许 fast-forward，不使用 `reset --hard` 或清理服务器工作区。
- 环境文件在服务器上设置为 `chmod 600`。
- 环境文件安装完成后才启动 Docker，防止容器在缺少配置时启动。
- 任一远程命令失败后不继续执行后续部署步骤。

## 验证

实施阶段增加针对脚本契约的静态测试，至少验证：

- 脚本没有硬编码密码。
- Git 更新使用 `pull --ff-only`。
- Compose 项目名固定为 `travel`。
- 服务器端口为前端 `8080`、后端 `8000`。
- 两个 `.env` 都会上传并设置权限。
- 部署包含前后端健康检查和临时文件清理。

部署完成后服务器执行：

```bash
docker compose -p travel ps
curl --fail "http://127.0.0.1:${BACKEND_PORT}/api/health"
curl --fail "http://127.0.0.1:${FRONTEND_PORT}/"
```

只有 Compose 命令成功、两个 HTTP 检查均通过时，脚本才报告部署成功。
