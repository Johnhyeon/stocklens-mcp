"""Configure Claude Desktop to use the StockLens MCP server.

Run `stocklens-setup` after installing the package.
"""

import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path


# MCP 서버 키: 'stocklens'
# v0.1.x 호환용: 'stock-data'가 있으면 자동으로 제거 (마이그레이션)
SERVER_KEY = "stocklens"
LEGACY_KEYS = ["stock-data"]


def resolve_server_entry(preferred_command: str = "stocklens") -> dict:
    """PATH 의존 없이 확실히 실행되는 MCP server config entry를 생성.

    우선순위:
    1. shutil.which로 PATH에서 찾기 → 절대 경로
    2. sysconfig scripts 디렉토리 직접 탐색 → 절대 경로
    3. 최후 fallback: sys.executable + `-m stock_mcp_server.server`

    반환된 entry는 Claude Desktop이 PATH 환경변수와 무관하게 실행 가능.
    """
    # 1) 사용자가 명시적으로 절대 경로를 줬으면 그대로 사용
    if os.path.isabs(preferred_command) and Path(preferred_command).exists():
        return {"command": preferred_command}

    # 2) PATH 탐색
    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

    # 3) sysconfig scripts 디렉토리 직접 탐색
    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = scripts_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}
    except Exception:
        pass

    # 4) 최후 fallback: python -m 형태
    #    stock_mcp_server.server 모듈 직접 실행
    return {
        "command": sys.executable,
        "args": ["-m", "stock_mcp_server.server"],
    }


def _find_store_config_path() -> Path | None:
    """Microsoft Store 버전 Claude Desktop의 샌드박스 config 경로 탐색.

    Store 앱은 `%LOCALAPPDATA%\\Packages\\Claude_<hash>\\LocalCache\\Roaming\\Claude\\`
    안에 config를 보관. 해시가 사용자별로 달라서 glob으로 찾음.

    Returns:
        Path if Store version detected, else None.
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    packages_dir = Path(local_appdata) / "Packages"
    if not packages_dir.exists():
        return None
    # Claude_xxx 또는 AnthropicPBC.Claude_xxx 등 변종 대응
    for pattern in ("Claude_*", "*Claude*"):
        for pkg in packages_dir.glob(pattern):
            candidate = pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            # 부모 디렉토리 존재 = Claude가 최소 한 번 실행됨
            if candidate.parent.exists():
                return candidate
    return None


def get_config_path() -> Path:
    """Claude Desktop config 경로 탐지.

    Windows 우선순위:
    1. Microsoft Store 버전 (샌드박스 경로) — Packages\\Claude_*\\LocalCache\\...
    2. 표준 .exe 설치 버전 — %APPDATA%\\Claude\\...

    Store 버전이 감지되면 그쪽을 씀. 아니면 표준 경로.
    """
    if sys.platform == "win32":
        store = _find_store_config_path()
        if store is not None:
            return store
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not found.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def configure(command: str = "stocklens") -> None:
    config_path = get_config_path()
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            backup_path = config_path.with_suffix(".json.backup")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print(f"  [OK] Backup saved: {backup_path}")
        except json.JSONDecodeError:
            print("  [WARN] Existing config is corrupted. Creating new one.")
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    # 이전 키 자동 정리 (마이그레이션)
    removed_legacy = []
    for legacy in LEGACY_KEYS:
        if legacy in config["mcpServers"]:
            del config["mcpServers"][legacy]
            removed_legacy.append(legacy)
    if removed_legacy:
        print(f"  [OK] Removed legacy entries: {', '.join(removed_legacy)}")

    # 새 키 등록 — PATH 의존 없이 확실히 실행되는 entry 사용
    entry = resolve_server_entry(command)
    config["mcpServers"][SERVER_KEY] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"  [OK] Config updated (key: {SERVER_KEY})")
    print(f"  Path: {config_path}")
    print(f"  Command: {entry['command']}")
    if "args" in entry:
        print(f"  Args:    {' '.join(entry['args'])}")
    # 검증: 기록한 command가 실제 실행 가능한지
    cmd = entry["command"]
    if Path(cmd).is_absolute() and not Path(cmd).exists():
        print(f"  [WARN] Recorded command file does not exist: {cmd}")
    elif not Path(cmd).is_absolute() and not shutil.which(cmd):
        print(f"  [WARN] '{cmd}' not found in PATH. "
              f"Run 'stocklens-doctor' to diagnose.")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "stocklens"
    print("==============================================")
    print("  StockLens - Claude Desktop Setup")
    print("==============================================")
    print()
    try:
        configure(command)
        print()
        print("Done! Please fully quit and restart Claude Desktop.")
    except Exception as e:
        print(f"  [ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
