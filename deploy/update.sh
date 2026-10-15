#!/usr/bin/env bash
# 源码构建 + 更新本地 Docker Compose 部署（主服务 + sentinel-solver）。
#
# 默认流程：git 同步（只允许快进）→ 构建本地镜像并写回 .env 的 CHATGPT2API_IMAGE
# → 重建运行时卷 → docker compose up -d --build → 打印状态。
#
# 用法：
#   sudo bash deploy/update.sh
#   sudo bash deploy/update.sh --reset              # 上游历史被重写、无法快进时
#   sudo bash deploy/update.sh --no-pull --no-build --keep-runtime
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

ENV_FILE="${REPO_DIR}/.env"
BRANCH="${BRANCH:-}"
TAG="${CHATGPT2API_LOCAL_IMAGE:-chatgpt2api:local}"
DO_PULL=1
DO_RESET=0
ALLOW_DIRTY=0
DO_BUILD=1
REBUILD_RUNTIME=1

usage() {
  cat <<'EOF'
构建并更新本地 Docker Compose 部署。

可选参数：
  --reset                  允许 `git reset --hard origin/<branch>`（上游历史被重写时使用）
  --allow-dirty            使用 --reset 时，允许丢弃工作区未提交的改动
  --no-pull                跳过 git 同步
  --no-build               不构建本地镜像，直接使用 .env 里的 CHATGPT2API_IMAGE
  --tag <name>             本地镜像 tag，默认 chatgpt2api:local
  --keep-runtime           不重建运行时卷（chatgpt2api-runtime）
  -h, --help               显示本帮助

说明：
  * 镜像 VERSION 不变时，入口脚本不会刷新容器内 /app，所以默认会重建运行时卷；
    /app 只是镜像种子与依赖的缓存，业务数据在 ./data 与 ./config.json。
  * .env 缺少 SENTINEL_SOLVER_KEY 时会自动生成（主服务与 sentinel-solver 共用）。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --reset) DO_RESET=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-pull) DO_PULL=0 ;;
    --no-build) DO_BUILD=0 ;;
    --keep-runtime) REBUILD_RUNTIME=0 ;;
    --tag) shift; TAG="${1:?--tag 需要值}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[update] %s\n' "$*"; }
die() { printf '[update] 错误：%s\n' "$*" >&2; exit 1; }

env_value() {
  local key="$1"
  [ -f "${ENV_FILE}" ] || return 0
  sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1
}

set_env_value() {
  local key="$1" value="$2"
  if [ -f "${ENV_FILE}" ] && grep -q "^${key}=" "${ENV_FILE}"; then
    awk -v k="${key}" -v v="${value}" 'BEGIN{FS="=";OFS="="} $1==k{print k"="v; next} {print}' \
      "${ENV_FILE}" >"${ENV_FILE}.tmp"
    mv "${ENV_FILE}.tmp" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

new_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
    return
  fi
  tr -d '-' </proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N
}

# ---------------------------------------------------------------------------
# 1. 前置检查
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件（Docker Compose v2）"
[ -f "${REPO_DIR}/docker-compose.yml" ] || die "当前目录不是 chatgpt2api 仓库：${REPO_DIR}"
[ -f "${REPO_DIR}/Dockerfile" ] || die "缺少 Dockerfile：${REPO_DIR}"

if [ "${DO_PULL}" = "1" ]; then
  command -v git >/dev/null 2>&1 || die "未找到 git"
  git rev-parse --git-dir >/dev/null 2>&1 || die "当前目录不是 Git 仓库，请改用 --no-pull"
  git remote get-url origin >/dev/null 2>&1 || die "没有 origin 远端，无法同步；请改用 --no-pull"
  BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
fi

if [ ! -f "${ENV_FILE}" ]; then
  [ -f "${REPO_DIR}/.env.example" ] || die "缺少 .env 与 .env.example，先执行：cp .env.example .env"
  cp "${REPO_DIR}/.env.example" "${ENV_FILE}"
  log "已从 .env.example 创建 .env，请确认 CHATGPT2API_AUTH_KEY"
fi

# ---------------------------------------------------------------------------
# 2. 同步代码
# ---------------------------------------------------------------------------
if [ "${DO_PULL}" = "1" ]; then
  if [ -n "$(git status --porcelain)" ] && [ "${DO_RESET}" = "1" ] && [ "${ALLOW_DIRTY}" != "1" ]; then
    git status --short >&2
    die "工作区有未提交改动，--reset 会丢弃它们；确认后加 --allow-dirty"
  fi

  log "git fetch origin（分支 ${BRANCH}）"
  git fetch --prune origin

  if [ "${DO_RESET}" = "1" ]; then
    log "git reset --hard origin/${BRANCH}"
    git reset --hard "origin/${BRANCH}"
  elif ! git merge --ff-only "origin/${BRANCH}"; then
    die "无法快进到 origin/${BRANCH}（历史被重写或存在本地提交）。确认后使用：bash deploy/update.sh --reset"
  fi
  log "当前提交：$(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"
fi

# ---------------------------------------------------------------------------
# 3. .env 必需项
# ---------------------------------------------------------------------------
if [ -z "$(env_value SENTINEL_SOLVER_KEY)" ]; then
  set_env_value SENTINEL_SOLVER_KEY "$(new_secret)"
  log "已生成 SENTINEL_SOLVER_KEY 并写入 .env"
fi
if [ -z "$(env_value CHATGPT2API_AUTH_KEY)" ]; then
  log "警告：.env 里 CHATGPT2API_AUTH_KEY 为空，控制台与 API 无法登录"
fi

COMPOSE_ARGS=(-f docker-compose.yml)
if [ -f "${REPO_DIR}/docker-compose.postgres.yml" ] && [ "$(env_value DATABASE_MODE)" = "postgres-local" ]; then
  COMPOSE_ARGS+=(-f docker-compose.postgres.yml)
fi

# ---------------------------------------------------------------------------
# 4. 构建镜像
# ---------------------------------------------------------------------------
if [ "${DO_BUILD}" = "1" ]; then
  log "构建主服务镜像：${TAG}"
  docker build -t "${TAG}" "${REPO_DIR}"
  # 让 compose 使用这次构建的镜像，避免继续拉取 registry 上的旧版本
  set_env_value CHATGPT2API_IMAGE "${TAG}"
else
  log "跳过镜像构建，使用 CHATGPT2API_IMAGE=$(env_value CHATGPT2API_IMAGE)"
fi

# ---------------------------------------------------------------------------
# 5. 重建运行时卷并启动
# ---------------------------------------------------------------------------
if [ "${REBUILD_RUNTIME}" = "1" ]; then
  log "停止服务并重建运行时卷（镜像 VERSION 未变时入口脚本不会刷新 /app）"
  docker compose "${COMPOSE_ARGS[@]}" down --remove-orphans
  while IFS= read -r volume; do
    [ -n "${volume}" ] || continue
    log "删除卷 ${volume}"
    docker volume rm "${volume}" >/dev/null
  done < <(docker volume ls --quiet --filter label=com.docker.compose.volume=chatgpt2api-runtime)
fi

log "启动服务"
docker compose "${COMPOSE_ARGS[@]}" up -d --build

# ---------------------------------------------------------------------------
# 6. 结果
# ---------------------------------------------------------------------------
docker compose "${COMPOSE_ARGS[@]}" ps
log "查看日志：docker compose ${COMPOSE_ARGS[*]} logs -f app sentinel-solver"
log "求解器健康检查：docker compose ${COMPOSE_ARGS[*]} exec app python -c \"import urllib.request;print(urllib.request.urlopen('http://sentinel-solver:8788/health').read().decode())\""
