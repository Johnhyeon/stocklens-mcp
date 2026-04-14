@echo off
setlocal EnableDelayedExpansion

echo ==============================================
echo   StockLens Installer (Windows)
echo   신규 설치 · 업데이트 · 마이그레이션 통합
echo ==============================================
echo.

REM [0/5] Windows 다운로드 차단 자동 해제 (Zone.Identifier)
REM    웹에서 다운받은 .bat이 차단돼 실행 즉시 꺼지는 문제 예방
powershell -NoProfile -Command "Unblock-File -Path '%~f0' -ErrorAction SilentlyContinue" >nul 2>&1
echo.

REM [1/4] Find Python
echo [1/4] Checking Python...

set "PYTHON_CMD="

REM `py -3` Python Launcher 우선 — 여러 Python 설치된 경우 **최신 버전** 자동 선택
REM (사용자 PATH에 3.9가 먼저 있어도 Launcher는 3.11+ 를 고름)
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto :found_python
)

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

REM Python 버전 체크 — 3.11 이상 필수
for /f "tokens=2 delims=. " %%a in ('!PYTHON_CMD! --version 2^>^&1') do set PY_MAJOR=%%a
for /f "tokens=3 delims=. " %%a in ('!PYTHON_CMD! --version 2^>^&1') do set PY_MINOR=%%a
if !PY_MAJOR! LSS 3 goto :py_too_old
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 11 goto :py_too_old
echo       [OK] Python version check passed (3.11+)
echo.
goto :py_version_ok

:py_too_old
echo.
echo       [FAIL] Python !PY_MAJOR!.!PY_MINOR! is too old. Python 3.11 or newer required.
echo.
echo       현재 Python: %PYVER%
echo       필요 Python: 3.11 이상
echo.
echo       1. https://www.python.org/downloads/ 에서 Python 3.12 다운로드
echo       2. 설치 시 "Add Python to PATH" 체크 필수
echo       3. PowerShell/cmd 완전히 닫고 새로 열기
echo       4. 이 install.bat 다시 실행
echo.
pause
exit /b 1

:py_version_ok

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

REM [5/5] 설치·설정 검증 (stocklens-doctor)
echo [5/5] Verifying installation...
!PYTHON_CMD! -m stock_mcp_server.doctor
if errorlevel 1 (
    echo.
    echo       [FAIL] Doctor reported critical issues. See above for fix commands.
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
echo 문제 발생 시:
echo   stocklens-doctor     (진단 다시 실행)
echo.
pause
