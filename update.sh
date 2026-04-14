#!/bin/bash
# [DEPRECATED] update.sh는 install.sh로 통합됐습니다.
# install.sh 하나로 신규 설치·업데이트·마이그레이션 모두 처리합니다.

echo "=============================================="
echo "  [안내] update.sh는 더 이상 별도 유지되지 않습니다."
echo "  install.sh가 신규 설치·업데이트·마이그레이션 모두 처리합니다."
echo "  install.sh를 실행합니다..."
echo "=============================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install.sh"
