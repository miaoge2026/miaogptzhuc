#!/usr/bin/env bash
# ChatGPT 批量注册工具 — 一键启动脚本
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════╗"
echo "║     ChatGPT 批量注册工具  · Docker 版        ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# 确认 Docker 可用
if ! command -v docker &>/dev/null; then
  echo "❌ 未检测到 Docker，请先安装 Docker: https://docs.docker.com/get-docker/"
  exit 1
fi

if ! docker compose version &>/dev/null 2>&1; then
  echo "❌ 未检测到 docker compose 插件，请升级 Docker 或安装 docker-compose-plugin"
  exit 1
fi

# 若不存在 .env，从示例复制
if [ ! -f .env ]; then
  cp .env.example .env
  echo -e "${YELLOW}⚠️  已从 .env.example 创建 .env，请根据需要编辑后重新运行${NC}"
  echo "   快速编辑: nano .env"
  echo ""
fi

# 创建数据目录
mkdir -p data/codex_tokens

echo -e "${GREEN}🔨 构建镜像...${NC}"
docker compose build

echo -e "${GREEN}🚀 启动服务...${NC}"
docker compose up -d

PORT=${PORT:-5000}
echo ""
echo -e "${GREEN}✅ 启动成功！${NC}"
echo -e "   面板地址: ${CYAN}http://localhost:${PORT}${NC}"
echo ""
echo "   查看日志: docker compose logs -f"
echo "   停止服务: docker compose stop"
echo "   重启服务: docker compose restart"
echo ""
