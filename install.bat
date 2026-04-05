@echo off
chcp 65001 >nul
setlocal

echo ==============================================
echo   naver-stock-mcp 자동 설치 스크립트 (Windows)
echo ==============================================
echo.

REM 스크립트 위치로 이동
cd /d "%~dp0"

REM [1/3] Python 설치 확인
echo [1/3] Python 설치 확인...
python --version >nul 2>&1
if errorlevel 1 (
    echo       X Python이 설치되어 있지 않습니다.
    echo.
    echo       먼저 Python 3.11 이상을 설치해주세요:
    echo       https://www.python.org/downloads/
    echo.
    echo       설치 시 "Add Python to PATH" 체크박스를 반드시 체크하세요.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo       O Python %PYVER% 감지됨
echo.

REM [2/3] 패키지 설치
echo [2/3] 패키지 설치 중...
python -m pip install -e . >nul 2>&1
if errorlevel 1 (
    echo       X 패키지 설치 실패
    echo       상세 에러를 보려면 아래 명령어를 직접 실행해보세요:
    echo         python -m pip install -e .
    echo.
    pause
    exit /b 1
)
echo       O stock-mcp-server 설치 완료
echo.

REM [3/3] Claude Desktop 설정
echo [3/3] Claude Desktop 설정 중...
python scripts\configure_claude.py stock-mcp-server
if errorlevel 1 (
    echo       X Claude Desktop 설정 실패
    pause
    exit /b 1
)
echo.

echo ==============================================
echo   설치가 완료되었습니다!
echo ==============================================
echo.
echo 다음 단계:
echo   1. Claude Desktop을 완전히 종료하세요.
echo      (트레이 아이콘 우클릭 -^> Quit)
echo   2. Claude Desktop을 다시 실행하세요.
echo   3. "삼성전자 현재가 알려줘" 라고 질문해보세요.
echo.
pause
