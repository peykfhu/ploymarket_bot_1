#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════╗"
echo "║   🤖 Polymarket Bot - 一键部署            ║"
echo "╚═══════════════════════════════════════════╝"
echo -e "${NC}"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ===== 安装 Docker =====
if ! command -v docker &>/dev/null; then
    echo -e "${YELLOW}[!] 安装 Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    echo -e "${GREEN}[✓] Docker 安装完成${NC}"
fi

# ===== 判断 compose 命令 =====
if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo -e "${YELLOW}[!] 安装 Docker Compose...${NC}"
    sudo curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
        -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    DC="docker-compose"
fi

echo -e "${GREEN}[✓] Docker 就绪${NC}"

# ===== 创建 .env =====
if [ ! -f .env ]; then
    cat > .env << 'ENVFILE'
POLYMARKET_API_KEY=
POLYMARKET_SECRET=
POLYMARKET_WALLET_ADDRESS=
POLYMARKET_PRIVATE_KEY=
NOAA_API_TOKEN=
BINANCE_API_KEY=
BINANCE_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
MAX_POSITION_SIZE=50
MAX_DAILY_LOSS=200
INITIAL_BALANCE=150
DRY_RUN=true
ENVFILE
    echo -e "${GREEN}[✓] .env 已创建${NC}"
fi

# ===== 创建数据目录 =====
mkdir -p data

# ===== 停旧容器 =====
$DC down 2>/dev/null || true

# ===== 构建启动 =====
echo -e "${CYAN}[*] 构建中（首次约2-3分钟）...${NC}"
$DC build --no-cache 2>&1 | grep -E "^(Step|Successfully|#)" || true

echo -e "${CYAN}[*] 启动服务...${NC}"
$DC up -d

# ===== 等待就绪 =====
echo -n "等待后端启动"
for i in $(seq 1 30); do
    echo -n "."
    sleep 1
    if curl -s http://localhost:8000/api/health >/dev/null 2>&1; then
        echo ""
        echo -e "${GREEN}[✓] 后端就绪${NC}"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo ""
        echo -e "${RED}[!] 启动超时，查看日志: $DC logs backend${NC}"
    fi
done

# ===== 开放端口 =====
if command -v ufw &>/dev/null; then
    sudo ufw allow 3000/tcp >/dev/null 2>&1 || true
    sudo ufw allow 8000/tcp >/dev/null 2>&1 || true
fi

# ===== 获取IP =====
IP=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

# ===== 完成 =====
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅ 部署成功!                                 ║${NC}"
echo -e "${GREEN}║                                               ║${NC}"
echo -e "${GREEN}║  🖥  面板:  ${BOLD}http://${IP}:3000${NC}${GREEN}          ║${NC}"
echo -e "${GREEN}║  🔌 API:   ${BOLD}http://${IP}:8000${NC}${GREEN}          ║${NC}"
echo -e "${GREEN}║                                               ║${NC}"
echo -e "${GREEN}║  📋 日志:  ${DC} logs -f backend${NC}${GREEN}       ║${NC}"
echo -e "${GREEN}║  🛑 停止:  ${DC} down${NC}${GREEN}                  ║${NC}"
echo -e "${GREEN}║  🔄 重启:  ${DC} restart${NC}${GREEN}               ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════╝${NC}"
echo ""
$DC ps