"""StockLens 설치·설정 진단 도구.

실행: `stocklens-doctor` 또는 `python -m stock_mcp_server.doctor`

체크 항목:
- Python 버전
- stocklens-mcp 패키지 설치 여부
- stocklens 실행 명령 탐색 (PATH / sysconfig)
- Claude Desktop config 파일
- config 내 stocklens entry 유효성 (command resolvable)
- Legacy 키 잔존 여부
"""

import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path


# setup_claude와 일관성 유지
try:
    from stock_mcp_server.setup_claude import (
        get_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
    )
except ImportError:
    # 패키지 설치 전 직접 실행한 경우 대비
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from stock_mcp_server.setup_claude import (
        get_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
    )


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = None  # "ok" / "warn" / "fail"
        self.lines: list[str] = []
        self.fix: str | None = None

    def ok(self, msg: str):
        self.status = "ok"
        self.lines.append(msg)
        return self

    def warn(self, msg: str, fix: str | None = None):
        if self.status != "fail":
            self.status = "warn"
        self.lines.append(msg)
        if fix:
            self.fix = fix
        return self

    def fail(self, msg: str, fix: str | None = None):
        self.status = "fail"
        self.lines.append(msg)
        if fix:
            self.fix = fix
        return self

    def info(self, msg: str):
        self.lines.append(msg)
        return self


def check_python() -> Check:
    c = Check("Python")
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    c.info(f"Version:    {ver}")
    c.info(f"Executable: {sys.executable}")
    if sys.version_info < (3, 11):
        c.fail(
            "Python 3.11+ required",
            fix="Install Python 3.11+ from https://www.python.org/downloads/",
        )
    else:
        c.ok("Python 3.11+ available")
    return c


def check_package() -> Check:
    c = Check("Package")
    try:
        import stock_mcp_server  # noqa: F401
        c.ok("stocklens-mcp is installed")
        c.info(f"Location:   {Path(stock_mcp_server.__file__).parent}")
    except ImportError:
        c.fail(
            "stocklens-mcp NOT installed",
            fix=f'"{sys.executable}" -m pip install stocklens-mcp',
        )
    return c


def check_stocklens_command() -> Check:
    c = Check("Command")
    # 1) PATH 탐색
    exe = shutil.which("stocklens")
    if exe:
        c.ok(f"'stocklens' command found in PATH")
        c.info(f"Path:       {exe}")
        return c

    # 2) sysconfig scripts 디렉토리 직접 확인
    scripts_dir = Path(sysconfig.get_paths()["scripts"])
    for name in ("stocklens.exe", "stocklens"):
        candidate = scripts_dir / name
        if candidate.exists():
            c.warn(
                f"'stocklens' exists but NOT in PATH",
                fix=(
                    f'Add to PATH: "{scripts_dir}"\n'
                    f'(or proceed — setup_claude will use absolute path)'
                ),
            )
            c.info(f"Path:       {candidate}")
            return c

    # 3) 어디에도 없음
    c.fail(
        "'stocklens' command NOT found anywhere",
        fix=(
            f'"{sys.executable}" -m pip install --force-reinstall stocklens-mcp'
        ),
    )
    c.info(f"Checked:    {scripts_dir}")
    return c


def check_config() -> Check:
    c = Check("Claude Desktop Config")
    config_path = get_config_path()

    # Store 버전 감지 알림
    if "Packages" in str(config_path) and "LocalCache" in str(config_path):
        c.info("Detected: Microsoft Store version (sandboxed path)")
    c.info(f"Path:       {config_path}")

    # 두 경로 모두 존재하는 비정상 케이스 경고
    from stock_mcp_server.setup_claude import _find_store_config_path
    import os as _os
    store = _find_store_config_path()
    std_appdata = _os.environ.get("APPDATA")
    std_path = Path(std_appdata) / "Claude" / "claude_desktop_config.json" if std_appdata else None
    if store and std_path and store.exists() and std_path.exists() and store != std_path:
        c.warn(
            f"Both Store and standard config files exist. Active: {config_path}",
            fix=f"Remove unused: {std_path if config_path == store else store}",
        )

    if not config_path.exists():
        c.fail(
            "Config file does not exist",
            fix=f'"{sys.executable}" -m stock_mcp_server.setup_claude',
        )
        return c

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        c.fail(
            f"Config is not valid JSON: {e}",
            fix="Back up and re-run stocklens-setup",
        )
        return c
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcpServers", {}) or {}
    entry = servers.get(SERVER_KEY)

    # Legacy 체크
    legacy_found = [k for k in LEGACY_KEYS if k in servers]
    if legacy_found:
        c.warn(
            f"Legacy entries present: {legacy_found}",
            fix=f'"{sys.executable}" -m stock_mcp_server.setup_claude (auto-removes)',
        )

    if not entry:
        c.fail(
            "'stocklens' entry missing in mcpServers",
            fix=f'"{sys.executable}" -m stock_mcp_server.setup_claude',
        )
        return c

    cmd = entry.get("command")
    args = entry.get("args", [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    # Command resolvability
    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.fail(
                f"Command file missing: {cmd}",
                fix=f'"{sys.executable}" -m stock_mcp_server.setup_claude',
            )
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — Claude Desktop will fail to launch",
                fix=f'"{sys.executable}" -m stock_mcp_server.setup_claude',
            )

    return c


STATUS_ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", None: "[ ?  ]"}


def print_check(c: Check):
    icon = STATUS_ICON.get(c.status, "[ ?  ]")
    print(f"{icon} {c.name}")
    for line in c.lines:
        print(f"       {line}")
    if c.fix:
        print(f"       Fix: {c.fix}")
    print()


def main():
    # Windows cp949 터미널 호환을 위해 stdout UTF-8 시도
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("  StockLens Doctor - Installation Diagnosis")
    print("=" * 60)
    print()

    checks = [
        check_python(),
        check_package(),
        check_stocklens_command(),
        check_config(),
    ]

    for c in checks:
        print_check(c)

    # 종합
    any_fail = any(c.status == "fail" for c in checks)
    any_warn = any(c.status == "warn" for c in checks)

    print("=" * 60)
    if any_fail:
        print("  [FAIL] One or more critical issues found.")
        print("  Apply the 'Fix:' commands above, then re-run stocklens-doctor.")
        sys.exit(1)
    elif any_warn:
        print("  [WARN] Installation works but some warnings exist.")
        print("  If MCP appears in Claude Desktop, you're fine.")
    else:
        print("  [ OK ] All checks passed!")
        print("  If MCP still doesn't appear, FULLY QUIT Claude Desktop")
        print("  (tray icon → Quit) and restart.")
    print("=" * 60)


if __name__ == "__main__":
    main()
