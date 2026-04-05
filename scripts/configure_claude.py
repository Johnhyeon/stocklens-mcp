"""Claude Desktop 설정 파일에 stock-data MCP 서버를 등록합니다.

install.bat / install.sh에서 호출되는 헬퍼 스크립트입니다.
기존 설정을 보존하면서 stock-data 항목만 추가/업데이트합니다.
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
    else:  # linux 및 기타
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def configure(command: str = "stock-mcp-server") -> None:
    config_path = get_config_path()
    config_dir = config_path.parent

    # 디렉토리 생성
    config_dir.mkdir(parents=True, exist_ok=True)

    # 기존 설정 읽기
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 백업 생성
            backup_path = config_path.with_suffix(".json.backup")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print(f"  기존 설정 백업: {backup_path}")
        except json.JSONDecodeError:
            print("  [WARN] 기존 설정 파일이 손상되어 있습니다. 새로 만듭니다.")
            config = {}
    else:
        config = {}

    # mcpServers 섹션 보장
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    # stock-data 등록/업데이트
    config["mcpServers"]["stock-data"] = {
        "command": command,
    }

    # 저장
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"  [OK] 설정 파일 업데이트 완료")
    print(f"  경로: {config_path}")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "stock-mcp-server"
    try:
        configure(command)
    except Exception as e:
        print(f"  [ERROR] 오류: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
