@echo off
REM [DEPRECATED] upgrade.bat은 install.bat으로 통합됐습니다.
REM install.bat이 이전 naver-stock-mcp 감지 시 자동 제거 + 새 패키지 설치까지 처리합니다.

echo ==============================================
echo   [안내] upgrade.bat은 더 이상 별도 유지되지 않습니다.
echo   install.bat이 신규 설치·업데이트·마이그레이션 모두 처리합니다.
echo   install.bat을 실행합니다...
echo ==============================================
echo.

call "%~dp0install.bat"
