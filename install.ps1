# StockLens Installer (Windows · PowerShell)
# Usage:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Johnhyeon/stocklens-mcp/main/install.ps1 | iex"
#
# 3 steps:
#   1) uv (Python package manager)  — auto-installs Python runtime if missing
#   2) stocklens-mcp via `uv tool install`
#   3) Claude Desktop config via `stocklens-setup`

$ErrorActionPreference = 'Stop'

$ESC = [char]27
function Info($msg)  { Write-Host "$ESC[36m$msg$ESC[0m" }
function OK($msg)    { Write-Host "$ESC[32m$msg$ESC[0m" }
function Warn($msg)  { Write-Host "$ESC[33m$msg$ESC[0m" }
function Err($msg)   { Write-Host "$ESC[31m$msg$ESC[0m" }

Write-Host ""
Write-Host "=============================================="
Write-Host "  StockLens Installer (Windows)"
Write-Host "=============================================="
Write-Host ""

$LocalBin = Join-Path $env:USERPROFILE ".local\bin"

# ── [1/3] uv ─────────────────────────────────────────────
Info "[1/4] Checking uv..."
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Info "      uv not found. Installing from astral.sh..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Err "      [FAIL] uv installation failed: $_"
        Err "      Manual install: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    }
    # uv installer adds %USERPROFILE%\.local\bin to user PATH for new shells.
    # Patch current session so the next steps see it immediately.
    if (Test-Path $LocalBin) {
        $env:Path = "$LocalBin;$env:Path"
    }
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Err "      [FAIL] uv installed but not on PATH. Open a new PowerShell and re-run."
        exit 1
    }
    OK "      uv installed: $($uvCmd.Source)"
} else {
    OK "      uv found: $($uvCmd.Source)"
}
Write-Host ""

# ── [2/3] stocklens-mcp ──────────────────────────────────
Info "[2/4] Installing stocklens-mcp..."

# Idempotent install: --force re-creates the tool environment if it already exists,
# so re-running the script reliably upgrades to the latest PyPI version.
# uv tool은 격리 환경이라 시스템 pip의 옛 naver-stock-mcp가 남아있어도 충돌하지 않음.
& uv tool install --force stocklens-mcp
if ($LASTEXITCODE -ne 0) {
    Err "      [FAIL] uv tool install failed."
    exit 1
}
OK "      stocklens-mcp installed via uv tool"
Write-Host ""

# Ensure .local\bin is on PATH for the rest of this session
if (Test-Path $LocalBin -PathType Container) {
    if (-not ($env:Path -split ';' -contains $LocalBin)) {
        $env:Path = "$LocalBin;$env:Path"
    }
}

# ── [3/3] MCP target config ──────────────────────────────
# $env:STOCKLENS_TARGET 으로 등록 대상 지정: claude-desktop / claude-code / both / auto
if (-not $env:STOCKLENS_TARGET) { $env:STOCKLENS_TARGET = "auto" }

Info "[3/4] Configuring MCP (target=$($env:STOCKLENS_TARGET))..."
$setupExe = Join-Path $LocalBin "stocklens-setup.exe"
if (Test-Path $setupExe) {
    & $setupExe stocklens
} else {
    & uv tool run --from stocklens-mcp stocklens-setup stocklens
}
if ($LASTEXITCODE -ne 0) {
    Err "      [FAIL] MCP configuration failed."
    exit 1
}
Write-Host ""

# ── [4/4] License activation ─────────────────────────────
Info "[4/4] License activation..."
Write-Host "      Enter the license key sent to your email after purchase."
$activateExe = Join-Path $LocalBin "stocklens-activate.exe"
$licKey = (Read-Host "      License key").Trim()
if ($licKey) {
    if (Test-Path $activateExe) {
        & $activateExe $licKey
    } else {
        & uv tool run --from stocklens-mcp stocklens-activate $licKey
    }
    if ($LASTEXITCODE -ne 0) {
        Warn "      Activation failed. Check the key and retry: stocklens-activate <LICENSE-KEY>"
    }
} else {
    Warn "      Skipped. Activate later: stocklens-activate <LICENSE-KEY>"
}
Write-Host ""

# ── Verify ───────────────────────────────────────────────
Info "Verifying installation..."
$doctorExe = Join-Path $LocalBin "stocklens-doctor.exe"
if (Test-Path $doctorExe) {
    & $doctorExe
} else {
    & uv tool run --from stocklens-mcp stocklens-doctor
}
if ($LASTEXITCODE -ne 0) {
    Err ""
    Err "[FAIL] Doctor reported critical issues. See above for fix commands."
    exit 1
}
Write-Host ""

Write-Host "=============================================="
OK     "  Installation complete"
Write-Host "=============================================="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. FULLY quit Claude Desktop (tray icon -> Quit)"
Write-Host "  2. Restart Claude Desktop"
Write-Host "  3. Try: '삼성전자 현재가'"
Write-Host ""
Write-Host "Activate:      stocklens-activate <LICENSE-KEY>"
Write-Host "Update later:  uv tool upgrade stocklens-mcp"
Write-Host "Diagnose:      stocklens-doctor"
Write-Host ""
