#!/bin/bash
# StockLens Installer (macOS / Linux)
# 신규 설치 · 업데이트 · 마이그레이션 통합

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=============================================="
echo "  StockLens Installer"
echo "  신규 설치 · 업데이트 · 마이그레이션 통합"
echo "=============================================="
echo ""

if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macOS"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="Linux"
else
    OS="Unknown"
fi
echo "  Detected OS: $OS"
echo ""

# [1/4] Python
echo "[1/4] Checking Python..."
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo -e "      ${RED}✗ Python not installed.${NC}"
    echo ""
    echo "      Install Python 3.11+ first:"
    if [[ "$OS" == "macOS" ]]; then
        echo "        brew install python"
    else
        echo "        sudo apt install python3 python3-pip"
    fi
    exit 1
fi

PYVER=$($PYTHON_CMD --version 2>&1 | awk '{print $2}')
echo -e "      ${GREEN}✓ Python $PYVER found${NC}"

PYMAJOR=$(echo $PYVER | cut -d. -f1)
PYMINOR=$(echo $PYVER | cut -d. -f2)
if [ "$PYMAJOR" -lt 3 ] || ([ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]); then
    echo -e "      ${YELLOW}⚠ Python 3.11+ required (current: $PYVER)${NC}"
    exit 1
fi
echo ""

# [2/4] Remove legacy package (naver-stock-mcp) if present
echo "[2/4] Checking legacy package (naver-stock-mcp)..."
if $PYTHON_CMD -m pip show naver-stock-mcp > /dev/null 2>&1; then
    echo "      (info) Legacy naver-stock-mcp detected. Removing..."
    $PYTHON_CMD -m pip uninstall -y naver-stock-mcp
    echo -e "      ${GREEN}✓ naver-stock-mcp removed${NC}"
else
    echo "      (skip) No legacy package to remove"
fi
echo ""

# [3/4] Install / upgrade stocklens-mcp
echo "[3/4] Installing / upgrading stocklens-mcp..."
if $PYTHON_CMD -m pip install --upgrade stocklens-mcp > /tmp/stocklens-install.log 2>&1; then
    echo -e "      ${GREEN}✓ stocklens-mcp installed / upgraded${NC}"
else
    echo -e "      ${RED}✗ Installation failed${NC}"
    echo "      Log: /tmp/stocklens-install.log"
    exit 1
fi
echo ""

# [4/4] Configure Claude Desktop
echo "[4/4] Configuring Claude Desktop..."
$PYTHON_CMD -m stock_mcp_server.setup_claude stocklens
echo ""

echo "=============================================="
echo "  완료! (Installation complete)"
echo "=============================================="
echo ""
echo "다음 단계:"
echo "  1. Claude Desktop 완전히 종료"
if [[ "$OS" == "macOS" ]]; then
    echo "     (Cmd+Q or Menu > Quit)"
fi
echo "  2. Claude Desktop 재시작"
echo "  3. 테스트: \"삼성전자 현재가\""
echo ""
