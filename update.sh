#!/bin/bash
# naver-stock-mcp 업데이트 (macOS / Linux)

set -e

GREEN='\033[0;32m'
NC='\033[0m'

echo "=============================================="
echo "  naver-stock-mcp 업데이트"
echo "=============================================="
echo ""

if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    PYTHON_CMD="python"
fi

echo "최신 버전으로 업데이트 중..."
$PYTHON_CMD -m pip install --upgrade naver-stock-mcp
echo ""
echo -e "${GREEN}✓ 업데이트 완료!${NC}"
echo ""
echo "Claude Desktop을 완전히 종료했다가 다시 실행하세요."
echo ""
