#!/bin/bash
# [DEPRECATED] upgrade.sh는 install.sh로 통합됐습니다.
# install.sh가 이전 naver-stock-mcp 감지 시 자동 제거 + 새 패키지 설치까지 처리합니다.

echo "=============================================="
echo "  [안내] upgrade.sh는 더 이상 별도 유지되지 않습니다."
echo "  install.sh가 신규 설치·업데이트·마이그레이션 모두 처리합니다."
echo "  install.sh를 실행합니다..."
echo "=============================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install.sh"
