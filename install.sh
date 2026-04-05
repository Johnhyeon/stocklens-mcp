#!/bin/bash
# naver-stock-mcp 자동 설치 (macOS / Linux)

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=============================================="
echo "  naver-stock-mcp 자동 설치"
echo "=============================================="
echo ""

if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macOS"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="Linux"
else
    OS="Unknown"
fi
echo "  감지된 OS: $OS"
echo ""

# [1/3] Python 설치 확인
echo "[1/3] Python 설치 확인..."
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo -e "      ${RED}✗ Python이 설치되어 있지 않습니다.${NC}"
    echo ""
    echo "      먼저 Python 3.11 이상을 설치해주세요:"
    if [[ "$OS" == "macOS" ]]; then
        echo "        brew install python"
        echo "        또는 https://www.python.org/downloads/"
    else
        echo "        sudo apt install python3 python3-pip  (Ubuntu/Debian)"
        echo "        sudo dnf install python3 python3-pip  (Fedora)"
    fi
    exit 1
fi

PYVER=$($PYTHON_CMD --version 2>&1 | awk '{print $2}')
echo -e "      ${GREEN}✓ Python $PYVER 감지됨${NC}"

PYMAJOR=$(echo $PYVER | cut -d. -f1)
PYMINOR=$(echo $PYVER | cut -d. -f2)
if [ "$PYMAJOR" -lt 3 ] || ([ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]); then
    echo -e "      ${YELLOW}⚠ Python 3.11 이상이 필요합니다 (현재: $PYVER)${NC}"
    exit 1
fi
echo ""

# [2/3] PyPI에서 설치
echo "[2/3] naver-stock-mcp 설치 중..."
if $PYTHON_CMD -m pip install --upgrade naver-stock-mcp; then
    echo -e "      ${GREEN}✓ naver-stock-mcp 설치 완료${NC}"
else
    echo -e "      ${RED}✗ 패키지 설치 실패${NC}"
    exit 1
fi
echo ""

# [3/3] Claude Desktop 설정
echo "[3/3] Claude Desktop 설정 중..."
if $PYTHON_CMD -m stock_mcp_server.setup_claude stock-mcp-server; then
    true
else
    echo -e "      ${RED}✗ Claude Desktop 설정 실패${NC}"
    exit 1
fi
echo ""

echo "=============================================="
echo "  설치가 완료되었습니다!"
echo "=============================================="
echo ""
echo "다음 단계:"
echo "  1. Claude Desktop을 완전히 종료하세요."
if [[ "$OS" == "macOS" ]]; then
    echo "     (Cmd + Q 또는 메뉴바에서 Quit)"
fi
echo "  2. Claude Desktop을 다시 실행하세요."
echo "  3. \"삼성전자 현재가 알려줘\" 라고 질문해보세요."
echo ""
