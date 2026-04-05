@echo off
chcp 65001 >nul
setlocal

echo ==============================================
echo   naver-stock-mcp 업데이트
echo ==============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo Python이 설치되어 있지 않습니다.
    pause
    exit /b 1
)

echo 최신 버전으로 업데이트 중...
python -m pip install --upgrade naver-stock-mcp
if errorlevel 1 (
    echo 업데이트 실패
    pause
    exit /b 1
)

echo.
echo ==============================================
echo   업데이트 완료!
echo ==============================================
echo.
echo Claude Desktop을 완전히 종료했다가 다시 실행하세요.
echo.
pause
