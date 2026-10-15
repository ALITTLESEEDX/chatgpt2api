# 部署与升级

状态：当前

本项目的发布镜像默认是 `ghcr.io/yukkcat/chatgpt2api:latest`。标准 Compose 将服务暴露在 `3000` 端口，使用 `chatgpt2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地 `data/` 和 `config.json`。运行时配置和数据不应提交到 Git。

## Docker 部署

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
cp .env.example .env
# 将 .env 中的 CHATGPT2API_AUTH_KEY=your_secret_key_here 替换为私有密钥。
test -f config.json || printf '{}\n' > config.json
docker compose up -d
```

镜像中的 `/opt/chatgpt2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，再按锁文件同步 Python 依赖；同一镜像正常重启时会保留控制台在线更新后的运行版本。业务数据始终留在独立的 `data/` 挂载中。

### Sentinel 求解服务

网页链路（生图与文本对话）的 Sentinel turnstile 挑战是随机化 opcode 的 VM，纯 Python 复刻无法通过；主服务需要调用 `sentinel-solver` 这个常驻 Chromium 的求解服务，否则 `/backend-api/f/conversation` 会返回 404 `Model not found`。

- `docker-compose.yml` 里的 `sentinel-solver` 从 `docker/solver.Dockerfile` 本地构建，首次 `docker compose up -d` 会自动构建镜像。
- 主服务与 solver 共用 `SENTINEL_SOLVER_KEY`（在 `.env` 中设置，长随机串）。`.env.example` 与安装脚本都会生成该值；solver 未设置密钥时拒绝启动。
- solver 只接收挑战参数（prepare 响应 + 令牌种子 `p`），不接收账号 token、Cookie 或 prompt，且默认只监听容器内网 `8788`，不映射到宿主机。
- 浏览器自身出网需要代理时，在 `.env` 中设置 `SENTINEL_SOLVER_PROXY=http://host:port`（留空直连）。
- 主服务侧可覆盖的变量：`CHATGPT2API_SENTINEL_SOLVER_ENABLED`、`CHATGPT2API_SENTINEL_SOLVER_URL`、`CHATGPT2API_SENTINEL_SOLVER_KEY`、`CHATGPT2API_SENTINEL_SOLVER_TIMEOUT_SECONDS`。

非容器方式（`MODE=python` 安装或本地开发）需要自行启动求解服务，再让主服务指向它：

```bash
SENTINEL_SOLVER_KEY=<长随机串> python scripts/sentinel_solver.py --port 8788 --chrome /usr/bin/chromium
```

```bash
CHATGPT2API_SENTINEL_SOLVER_ENABLED=true \
CHATGPT2API_SENTINEL_SOLVER_URL=http://127.0.0.1:8788 \
CHATGPT2API_SENTINEL_SOLVER_KEY=<同一个长随机串> \
uv run main.py
```

排查：`curl -H "X-Solver-Key: <key>" http://127.0.0.1:8788/health`，返回 `{"ok": true, ...}` 表示求解器已就绪；主服务日志里 `sentinel_solver_failed` / `sentinel_solver_rejected` 表示求解失败并已回退到内置实现。

#### 当前限制（2026-10-15 实测）

求解服务已经接入并可用（`bootstrap`、`chat-requirements/prepare`、`/finalize`、图片 `f/conversation/prepare` 全部 200），但**网页生图仍未恢复**：求解服务返回的 turnstile token 与传入的令牌种子 `p` 逐字节相同（`gAAAAAC…`，643 字节；真实浏览器约 7.5KB），上游据此把客户端判为不可信，`/backend-api/f/conversation` 返回 404 `Model not found`。

原因来自上游当前的 turnstile 模块（`/unauth-mweb/assets/turnstile-*.js`）：

- `getEnforcementToken(reqs)` 有三条路径：命中 `tokenCache` 直接返回缓存值 → `reqs.turnstile.dx` 存在时用混淆 VM 解 `dx` → 否则调真实 Cloudflare widget（按 `persona` 选 sitekey）。
- 求解服务为了让 VM「成功」，把全局 `WeakMap.prototype.get` 打补丁，使 `tokenCache.get(reqs)` 返回 `p`，于是第一条路径直接返回 `p`——这不是可用 token。
- 真正走 VM 路径时 VM 的第 0 号 opcode 就失败：`0: SyntaxError: Unexpected token '<'`，即 `dx` 用当前页面上下文解不开（`dx` 由账号/会话级密钥加密，匿名页面拿不到）。
- 匿名页面走 widget 路径则被 Cloudflare 拒绝：`Turnstile challenge failed: 600010`（freeaccount / paid 的 sitekey 不适用于未登录页面）。

因此要恢复生图，需要另外一条能拿到真实 turnstile token 的路径（例如让求解服务持有账号会话，或直接由浏览器完成 `f/conversation` 请求），这超出本次移植范围。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后再启动应用，并将数据持久化到 `chatgpt2api-postgres-data` 命名卷。数据库端口默认不暴露到宿主机。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
```

`.env` 中的 `CHATGPT2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

也可以使用仓库安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/main/deploy/install.sh | sudo bash
```

安装脚本会明确询问 Application Database：

- SQLite 本地文件（默认）
- PostgreSQL 18 本地容器（Docker 模式）
- 已有 PostgreSQL URL

选择本地 PostgreSQL 时，脚本会自动生成并保存数据库密码，下载 `docker-compose.postgres.yml`，再与主 Compose 一起启动。重复运行安装脚本会复用已有密码。`CHATGPT2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量，只要求正整数且不设置人为最高值；账号、代理和上游服务仍分别执行自己的并发限制。

## 本地开发

后端：

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/chatgpt2api.db`。支持 SQLite 与 PostgreSQL 18，不再通过
`STORAGE_BACKEND` 选择 JSON、Git 或账号专用数据库。

选择数据库不是旧数据迁移操作，不会自动导入 JSON、JSONL、Git 或旧账号
SQLite 文件。图片文件及其相关索引仍按图片存储边界管理。完整边界见
[`storage-architecture.md`](storage-architecture.md)。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`。图片任务记录、PPT / PSD 文件和图片目录
按备份设置选择；外部 WebDAV 仍需独立备份。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/chatgpt2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

镜像部署固定或回退版本时，在 `.env` 设置 `CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。
