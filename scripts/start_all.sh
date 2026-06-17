#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# 一键启动：example_agent + eval 后端 + 对话界面 + 看板 + Agent Runtime
#
# 用法:
#   bash scripts/start_all.sh                     # 默认端口
#   bash scripts/start_all.sh --stop              # 停止所有服务
#   bash scripts/start_all.sh --status            # 查看运行状态
# ═══════════════════════════════════════════════════════════════════════════

set -e

# ── 颜色 ──────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── 路径 ──────────────────────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_DIR="/tmp/agent-eval"
LOG_DIR="$PROJECT_ROOT/logs"
ENV_FILE="$PROJECT_ROOT/backend/.env"
AGENT_RUNTIME_READY_FILE="$PID_DIR/agent_runtime.ready"

# ── 端口 ──────────────────────────────────────────────────────────────
AGENT_PORT="${AGENT_PORT:-8800}"       # 对话界面 + 看板
EVAL_PORT="${EVAL_PORT:-18000}"        # 评测后端 API
DB_PORT="${DB_PORT:-5433}"             # PostgreSQL（docker-compose 映射）

# ── 日志函数 ──────────────────────────────────────────────────────────
log()    { echo -e "${BLUE}[$(date +'%H:%M:%S')]${NC} $1"; }
ok()     { echo -e "       ${GREEN}✅${NC} $1"; }
warn()   { echo -e "       ${YELLOW}⚠️${NC}  $1"; }
fail()   { echo -e "       ${RED}❌${NC} $1"; }
info()   { echo -e "       ${CYAN}ℹ${NC}  $1"; }

load_env_file() {
    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
        ok "已加载环境配置: $ENV_FILE"
    else
        warn "未找到环境配置文件: $ENV_FILE"
    fi
}

# ── 确保 PID 目录 ────────────────────────────────────────────────────
mkdir -p "$PID_DIR"
mkdir -p "$LOG_DIR"

# ═══════════════════════════════════════════════════════════════════════════
# 停止
# ═══════════════════════════════════════════════════════════════════════════
stop_all() {
    echo -e "${YELLOW}正在停止所有服务...${NC}"

    for svc in agent_server eval_api agent_runtime; do
        pid_file="$PID_DIR/${svc}.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
                # 等待退出
                for i in $(seq 1 10); do
                    kill -0 "$pid" 2>/dev/null || break
                    sleep 0.3
                done
                # 强制 kill
                kill -9 "$pid" 2>/dev/null || true
                ok "已停止 $svc (PID=$pid)"
            else
                info "$svc 已不在运行"
            fi
            rm -f "$pid_file"
        fi
    done

    # 清理残留进程
    pkill -f "agent_server.py" 2>/dev/null || true
    pkill -f "uvicorn backend.api:app" 2>/dev/null || true
    pkill -f "python -m backend.agent.runtime" 2>/dev/null || true
    rm -f "$AGENT_RUNTIME_READY_FILE"

    echo -e "${GREEN}所有服务已停止。${NC}"
}

# ═══════════════════════════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════════════════════════
status_all() {
    echo -e "${CYAN}══════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  服务状态${NC}"
    echo -e "${CYAN}══════════════════════════════════════════════${NC}"

    check_svc() {
        local name="$1" pid_file="$PID_DIR/${1}.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "  ${GREEN}●${NC} $name  (PID=$pid)"
            else
                echo -e "  ${RED}○${NC} $name  (PID 文件存在但进程已死)"
            fi
        else
            echo -e "  ${RED}○${NC} $name  (未运行)"
        fi
    }

    check_svc "agent_server"
    check_svc "eval_api"
    check_svc "agent_runtime"
    echo ""

    # 端口检查
    echo -e "  端口监听:"
    for port in $AGENT_PORT $EVAL_PORT; do
        if lsof -i ":$port" -sTCP:LISTEN >/dev/null 2>&1; then
            echo -e "    ${GREEN}●${NC} :$port"
        else
            echo -e "    ${RED}○${NC} :$port"
        fi
    done
    echo -e "${CYAN}══════════════════════════════════════════════${NC}"
}

# ═══════════════════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════════════════
start_all() {
    cd "$PROJECT_ROOT"

    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Agent Eval 一键启动${NC}"
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo ""

    # ── 1. 检查 & 启动 Docker 基础设施 ──────────────────────────────
    log "1/7 检查基础设施 (PostgreSQL + Redis)..."

    # 检查 docker 是否可用
    if ! command -v docker &>/dev/null; then
        fail "Docker 未安装，请先安装 Docker Desktop"
        exit 1
    fi

    # 检查 postgres 是否已在监听
    if lsof -i ":$DB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        ok "PostgreSQL 已在端口 $DB_PORT 运行"
    else
        log "   启动 docker-compose 服务..."
        docker compose up -d redis postgres 2>/dev/null \
            || docker-compose up -d redis postgres
        ok "PostgreSQL + Redis 容器已启动"
    fi

    # 检查 Redis
    if lsof -i ":6379" -sTCP:LISTEN >/dev/null 2>&1; then
        ok "Redis 已在端口 6379 运行"
    else
        warn "Redis 可能未就绪，等待中..."
    fi

    # ── 2. 设置环境变量 ──────────────────────────────────────────────
    log "2/7 配置环境变量..."
    load_env_file

    # 优先使用已有的 DATABASE_URL，否则根据 docker-compose 设置默认值
    if [ -z "$DATABASE_URL" ]; then
        export DATABASE_URL="postgresql+asyncpg://aura:aura@localhost:${DB_PORT}/agent_eval"
    fi
    if [ -z "$REDIS_URL" ]; then
        export REDIS_URL="redis://localhost:6379/0"
    fi
    info "DATABASE_URL = $DATABASE_URL"
    info "REDIS_URL    = $REDIS_URL"

    # ── 3. Agent Runtime 配置预检 ─────────────────────────────────
    log "3/7 检查 Agent Runtime 配置..."
    if python -m backend.agent.runtime --check-config > "$LOG_DIR/agent_runtime.log" 2>&1; then
        ok "Agent Runtime 配置检查通过"
    else
        fail "Agent Runtime 配置检查失败，查看日志: tail $LOG_DIR/agent_runtime.log"
        exit 1
    fi

    # ── 4. 终止旧服务 ──────────────────────────────────────────────
    log "4/7 检查并终止旧服务..."

    kill_old_on_port() {
        local port="$1" name="$2"
        local pids=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)
        if [ -n "$pids" ]; then
            warn "端口 $port 被占用 ($name)，正在终止..."
            echo "$pids" | xargs kill 2>/dev/null
            sleep 0.5
            # 顽固进程强制 kill
            local remain=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)
            if [ -n "$remain" ]; then
                echo "$remain" | xargs kill -9 2>/dev/null
            fi
            ok "已释放端口 $port"
        fi
    }

    # 先通过 PID 文件尝试精确停止
    for svc in agent_server eval_api agent_runtime; do
        pid_file="$PID_DIR/${svc}.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                warn "发现残留 $svc (PID=$pid)，正在终止..."
                kill "$pid" 2>/dev/null
                sleep 0.5
                kill -9 "$pid" 2>/dev/null || true
            fi
            rm -f "$pid_file"
        fi
    done

    # 再通过端口兜底清理
    kill_old_on_port "$EVAL_PORT"  "eval_api"
    kill_old_on_port "$AGENT_PORT" "agent_server"
    pkill -f "python -m backend.agent.runtime" 2>/dev/null || true
    rm -f "$AGENT_RUNTIME_READY_FILE"

    # ── 5. 启动评测后端 API ──────────────────────────────────────────
    log "5/7 启动评测后端 API (端口 $EVAL_PORT)..."

    nohup python -m uvicorn backend.api:app \
        --host 0.0.0.0 \
        --port "$EVAL_PORT" \
        --log-level info \
        --no-access-log \
        --reload \
        > "$LOG_DIR/eval_api.log" 2>&1 &
    EVAL_PID=$!
    echo $EVAL_PID > "$PID_DIR/eval_api.pid"

    # 等待就绪
    for i in $(seq 1 15); do
        if curl -s "http://localhost:$EVAL_PORT/health" >/dev/null 2>&1; then
            ok "评测后端 API 就绪 (PID=$EVAL_PID, 端口 $EVAL_PORT)"
            break
        fi
        sleep 0.5
    done
    if ! kill -0 "$EVAL_PID" 2>/dev/null; then
        fail "评测后端启动失败，查看日志: tail $LOG_DIR/eval_api.log"
        exit 1
    fi

    # ── 6. 启动 Agent Server（对话界面 + 看板）──────────────────────
    log "6/7 启动 Agent Server - 对话界面 + 看板 (端口 $AGENT_PORT)..."

    rm -f "$PID_DIR/agent_server.pid"

    nohup python -m uvicorn examples.agent_server:app \
        --host 0.0.0.0 \
        --port "$AGENT_PORT" \
        --log-level info \
        --no-access-log \
        --reload \
        > "$LOG_DIR/agent_server.log" 2>&1 &
    AGENT_PID=$!
    echo $AGENT_PID > "$PID_DIR/agent_server.pid"

    # 等待就绪
    for i in $(seq 1 15); do
        if curl -s "http://localhost:$AGENT_PORT/" >/dev/null 2>&1; then
            ok "Agent Server 就绪 (PID=$AGENT_PID, 端口 $AGENT_PORT)"
            break
        fi
        sleep 0.5
    done
    if ! kill -0 "$AGENT_PID" 2>/dev/null; then
        fail "Agent Server 启动失败，查看日志: tail $LOG_DIR/agent_server.log"
        exit 1
    fi

    # ── 7. 启动 Agent Runtime（Gateway + Scheduler）────────────────
    log "7/7 启动 Agent Runtime - Gateway + Scheduler..."

    rm -f "$PID_DIR/agent_runtime.pid" "$AGENT_RUNTIME_READY_FILE"

    AGENT_RUNTIME_READY_FILE="$AGENT_RUNTIME_READY_FILE" \
    nohup python -m backend.agent.runtime \
        > "$LOG_DIR/agent_runtime.log" 2>&1 &
    RUNTIME_PID=$!
    echo $RUNTIME_PID > "$PID_DIR/agent_runtime.pid"

    # 等待 Gateway + Scheduler 完成初始化
    for i in $(seq 1 30); do
        if [ -f "$AGENT_RUNTIME_READY_FILE" ]; then
            ok "Agent Runtime 就绪 (PID=$RUNTIME_PID)"
            break
        fi
        if ! kill -0 "$RUNTIME_PID" 2>/dev/null; then
            fail "Agent Runtime 启动失败，查看日志: tail $LOG_DIR/agent_runtime.log"
            exit 1
        fi
        sleep 0.5
    done
    if [ ! -f "$AGENT_RUNTIME_READY_FILE" ]; then
        fail "Agent Runtime 未在预期时间内就绪，查看日志: tail $LOG_DIR/agent_runtime.log"
        exit 1
    fi

    # ── 汇总 ──────────────────────────────────────────────────────────
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  🚀 所有服务已启动！${NC}"
    echo ""
    echo -e "  ${YELLOW}💬 对话界面 + 📊 看板:${NC}"
    echo -e "     http://localhost:${AGENT_PORT}"
    echo ""
    echo -e "  ${YELLOW}🔧 评测后端 API:${NC}"
    echo -e "     http://localhost:${EVAL_PORT}/docs"
    echo -e "     http://localhost:${EVAL_PORT}/health"
    echo ""
    echo -e "  ${CYAN}📋 日志文件:${NC}"
    echo -e "     Agent Server:   tail -f $LOG_DIR/agent_server.log"
    echo -e "     Eval API:       tail -f $LOG_DIR/eval_api.log"
    echo -e "     Agent Runtime:  tail -f $LOG_DIR/agent_runtime.log"
    echo ""
    echo -e "  ${CYAN}🔧 管理命令:${NC}"
    echo -e "     停止服务:      bash scripts/start_all.sh --stop"
    echo -e "     查看状态:      bash scripts/start_all.sh --status"
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

case "${1:-}" in
    --stop|-s)
        stop_all
        ;;
    --status|status)
        status_all
        ;;
    --restart|-r)
        stop_all
        sleep 1
        start_all
        ;;
    *)
        start_all
        ;;
esac
