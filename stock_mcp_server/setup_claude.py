"""Claude Desktop 설정에 stock-data MCP 서버를 등록하는 CLI.

설치 후 `stock-mcp-setup` 명령어로 실행할 수 있습니다.
"""

import json
import os
import sys
from pathlib import Path


def get_config_path() -> Path:
    """OS별 Claude Desktop 설정 파일 경로를 반환합니다."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA 환경변수를 찾을 수 없습니다.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def configure(command: str = "stock-mcp-server") -> None:
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
            print(f"  [OK] 기존 설정 백업: {backup_path}")
        except json.JSONDecodeError:
            print("  [WARN] 기존 설정 파일이 손상되어 있습니다. 새로 만듭니다.")
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"]["stock-data"] = {"command": command}

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"  [OK] 설정 파일 업데이트 완료")
    print(f"  경로: {config_path}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "stock-mcp-server"
    print("==============================================")
    print("  naver-stock-mcp - Claude Desktop 설정")
    print("==============================================")
    print()
    try:
        configure(command)
        print()
        print("설정이 완료되었습니다.")
        print("Claude Desktop을 완전히 종료했다가 다시 실행하세요.")
    except Exception as e:
        print(f"  [ERROR] 오류: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
