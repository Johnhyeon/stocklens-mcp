@echo off
setlocal EnableDelayedExpansion

echo ==============================================
echo   StockLens Installer (Windows)
echo   신규 설치 · 업데이트 · 마이그레이션 통합
echo ==============================================
echo.

REM [1/4] Find Python
echo [1/4] Checking Python...

set "PYTHON_CMD="

python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto :found_python
)

python3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python3"
    goto :found_python
)

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto :found_python
)

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python315\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Python315\python.exe"
    "C:\Python314\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P (
        set "PYTHON_CMD=%%~P"
        goto :found_python
    )
)

echo       [FAIL] Python is not installed.
echo.
echo       Please install Python 3.11+ first:
echo       https://www.python.org/downloads/
echo.
echo       IMPORTANT: Check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%v in ('!PYTHON_CMD! --version 2^>^&1') do set PYVER=%%v
echo       [OK] %PYVER% found
echo       Using: !PYTHON_CMD!
echo.

REM [2/4] Remove legacy package (naver-stock-mcp) if present
echo [2/4] Checking legacy package (naver-stock-mcp)...
!PYTHON_CMD! -m pip show naver-stock-mcp >nul 2>&1
if not errorlevel 1 (
    echo       [INFO] Legacy naver-stock-mcp detected. Removing...
    !PYTHON_CMD! -m pip uninstall -y naver-stock-mcp
    echo       [OK] naver-stock-mcp removed
) else (
    echo       [SKIP] No legacy package to remove
)
echo.

REM [3/4] Install / upgrade stocklens-mcp
echo [3/4] Installing / upgrading stocklens-mcp...
!PYTHON_CMD! -m pip install --upgrade stocklens-mcp
if errorlevel 1 (
    echo       [FAIL] Package installation failed.
    echo       Try: %PYTHON_CMD% -m pip install --upgrade stocklens-mcp
    echo.
    pause
    exit /b 1
)
echo       [OK] stocklens-mcp installed / upgraded
echo.

REM [4/4] Configure Claude Desktop
echo [4/4] Configuring Claude Desktop...
!PYTHON_CMD! -m stock_mcp_server.setup_claude stocklens
if errorlevel 1 (
    echo       [FAIL] Claude Desktop configuration failed.
    pause
    exit /b 1
)
echo.

echo ==============================================
echo   완료! (Installation complete)
echo ==============================================
echo.
echo 다음 단계:
echo   1. Claude Desktop 완전히 종료 (트레이 아이콘 우클릭 → Quit)
echo   2. Claude Desktop 재시작
echo   3. 테스트: "삼성전자 현재가"
echo.
pause
