@echo off
REM [DEPRECATED] update.bat은 install.bat으로 통합됐습니다.
REM install.bat 하나로 신규 설치·업데이트·마이그레이션 모두 처리합니다.

echo ==============================================
echo   [안내] update.bat은 더 이상 별도 유지되지 않습니다.
echo   install.bat이 신규 설치·업데이트·마이그레이션 모두 처리합니다.
echo   install.bat을 실행합니다...
echo ==============================================
echo.

call "%~dp0install.bat"
