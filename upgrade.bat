@echo off
setlocal EnableDelayedExpansion

echo ==============================================
echo   StockLens Upgrade (Windows)
echo   naver-stock-mcp -^> stocklens-mcp v0.2
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
pause
exit /b 1

:found_python
for /f "tokens=*" %%v in ('!PYTHON_CMD! --version 2^>^&1') do set PYVER=%%v
echo       [OK] %PYVER% found
echo       Using: !PYTHON_CMD!
echo.

REM [2/4] Uninstall old package (naver-stock-mcp) if present
echo [2/4] Removing old package (naver-stock-mcp)...
!PYTHON_CMD! -m pip show naver-stock-mcp >nul 2>&1
if not errorlevel 1 (
    !PYTHON_CMD! -m pip uninstall -y naver-stock-mcp
    echo       [OK] naver-stock-mcp removed
) else (
    echo       [SKIP] naver-stock-mcp not installed
)
echo.

REM [3/4] Install / upgrade stocklens-mcp
echo [3/4] Installing stocklens-mcp (latest)...
!PYTHON_CMD! -m pip install --upgrade stocklens-mcp
if errorlevel 1 (
    echo       [FAIL] Package installation failed.
    pause
    exit /b 1
)
echo       [OK] stocklens-mcp installed
echo.

REM [4/4] Configure Claude Desktop (auto-migrate legacy keys)
echo [4/4] Updating Claude Desktop config...
!PYTHON_CMD! -m stock_mcp_server.setup_claude stocklens
if errorlevel 1 (
    echo       [FAIL] Claude Desktop configuration failed.
    pause
    exit /b 1
)
echo.

echo ==============================================
echo   Upgrade complete!
echo ==============================================
echo.
echo Next steps:
echo   1. Quit Claude Desktop completely.
echo      (Right-click tray icon, then Quit)
echo   2. Restart Claude Desktop.
echo   3. Try asking: "Samsung Electronics current price"
echo.
pause
