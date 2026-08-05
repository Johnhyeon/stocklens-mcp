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


def _uv_tool_bin_dirs() -> list[Path]:
    """`uv tool install`이 entry point를 배치하는 경로 후보.

    uv는 `~/.local/bin` (Unix·Windows 공통)을 표준으로 쓰지만, 사용자가
    `UV_TOOL_BIN_DIR` / `XDG_BIN_HOME`로 재정의할 수 있다. 두 경우 다 커버.
    """
    candidates: list[Path] = []
    env = os.environ.get("UV_TOOL_BIN_DIR")
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_BIN_HOME")
    if xdg:
        candidates.append(Path(xdg))
    candidates.append(Path.home() / ".local" / "bin")
    return [p for p in candidates if p.exists()]


def resolve_server_entry(preferred_command: str = "stocklens") -> dict:
    """PATH 의존 없이 확실히 실행되는 MCP server config entry를 생성.

    우선순위:
    1. 절대 경로가 명시되면 그대로 사용
    2. PATH 탐색 (shutil.which)
    3. uv tool bin 디렉토리 직접 탐색 (`~/.local/bin` 등)
    4. sysconfig scripts 디렉토리 직접 탐색
    5. 최후 fallback: sys.executable + `-m stock_mcp_server.server`

    반환된 entry는 Claude Desktop이 PATH 환경변수와 무관하게 실행 가능.
    """
    # 1) 사용자가 명시적으로 절대 경로를 줬으면 그대로 사용
    if os.path.isabs(preferred_command) and Path(preferred_command).exists():
        return {"command": preferred_command}

    # 2) PATH 탐색
    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

    # 3) uv tool bin 디렉토리 — `uv tool install` 직후 PATH 미반영 상태에서도 잡힘
    for bin_dir in _uv_tool_bin_dirs():
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = bin_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}

    # 4) sysconfig scripts 디렉토리 직접 탐색 (pip 호환)
    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = scripts_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}
    except Exception:
        pass

    # 5) 최후 fallback: python -m 형태
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


def get_claude_desktop_config_path() -> Path:
    """Claude Desktop 앱의 mcpServers config 파일 경로.

    Windows 우선순위:
    1. Microsoft Store 버전 (샌드박스 경로) — Packages\\Claude_*\\LocalCache\\...
    2. 표준 .exe 설치 버전 — %APPDATA%\\Claude\\...
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


def get_claude_code_config_path() -> Path:
    """Claude Code CLI의 사용자 스코프 config (`~/.claude.json`).

    Claude Code 도 Claude Desktop 과 동일한 mcpServers 객체 스키마를 쓴다.
    파일에는 다른 사용자 설정/세션 키가 같이 들어있으므로 mcpServers 부분만
    patching 한다.
    """
    return Path.home() / ".claude.json"


# 하위 호환 — 기존 코드/외부 import 보존
def get_config_path() -> Path:
    return get_claude_desktop_config_path()


# (target name, 경로 함수, 사람이 읽는 라벨)
TARGETS: dict[str, tuple] = {
    "claude-desktop": (get_claude_desktop_config_path, "Claude Desktop"),
    "claude-code": (get_claude_code_config_path, "Claude Code CLI"),
}


def _resolve_targets(arg: str) -> list[str]:
    """`--target` 인자를 실제 타겟 리스트로 해석. `auto` 는 환경 감지.

    감지 규칙:
    - STOCKLENS_TARGET 환경변수가 명시 (auto 외의 값) → 그 값 사용
    - 그 외, `claude` CLI on PATH = Claude Code 환경
    - Claude Desktop config 디렉토리 존재 = Desktop 환경
    - 둘 다면 both, 아무것도 없으면 claude-desktop (가장 흔한 케이스)
    """
    if arg == "both":
        return ["claude-desktop", "claude-code"]
    if arg in TARGETS:
        return [arg]
    if arg == "auto":
        env_target = (os.environ.get("STOCKLENS_TARGET") or "").strip().lower()
        if env_target and env_target != "auto":
            return _resolve_targets(env_target)

        has_code = shutil.which("claude") is not None
        desktop_dir = get_claude_desktop_config_path().parent
        has_desktop = desktop_dir.exists()

        if has_code and has_desktop:
            return ["claude-desktop", "claude-code"]
        if has_code:
            return ["claude-code"]
        return ["claude-desktop"]
    raise ValueError(f"Invalid target: {arg}")


def _configure_one_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """단일 config 파일에 mcpServers.stocklens 등록. 변경 결과를 dict로 반환한다.

    quiet=True 면 사람용 진행 로그를 찍지 않는다(Manager의 --json/--non-interactive
    호출에서 stdout을 구조화 결과 하나로만 유지하기 위함). 파일 변경 로직 자체는
    quiet 여부와 무관하게 동일하다.
    """
    if not quiet:
        print()
        print(f"  → {label}")

    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            backup_path = config_path.with_suffix(".json.backup")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            if not quiet:
                print(f"  [OK] Backup saved: {backup_path}")
        except json.JSONDecodeError:
            if not quiet:
                print("  [WARN] Existing config is corrupted. Creating new one.")
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    removed_legacy = []
    for legacy in LEGACY_KEYS:
        if legacy in config["mcpServers"]:
            del config["mcpServers"][legacy]
            removed_legacy.append(legacy)
    if removed_legacy and not quiet:
        print(f"  [OK] Removed legacy entries: {', '.join(removed_legacy)}")

    # dict 키 대입이라 반복 실행해도 중복 항목이 생기지 않는다(항상 덮어씀).
    entry = resolve_server_entry(command)
    config["mcpServers"][SERVER_KEY] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    warnings: list[str] = []
    cmd = entry["command"]
    if Path(cmd).is_absolute() and not Path(cmd).exists():
        warnings.append(f"Recorded command file does not exist: {cmd}")
    elif not Path(cmd).is_absolute() and not shutil.which(cmd):
        warnings.append(f"'{cmd}' not found in PATH. Run 'stocklens-doctor' to diagnose.")

    if not quiet:
        print(f"  [OK] Config updated (key: {SERVER_KEY})")
        print(f"  Path:    {config_path}")
        print(f"  Command: {entry['command']}")
        if "args" in entry:
            print(f"  Args:    {' '.join(entry['args'])}")
        for w in warnings:
            print(f"  [WARN] {w}")

    return {
        "target_label": label,
        "config_path": str(config_path),
        "backup_path": str(backup_path) if backup_path else None,
        "command": entry["command"],
        "args": entry.get("args"),
        "legacy_removed": removed_legacy,
        "warnings": warnings,
    }


def configure(
    command: str = "stocklens", *, targets: list[str] | None = None, quiet: bool = False
) -> list[dict]:
    """선택된 모든 타겟에 stocklens MCP 등록. 타겟별 변경 결과 리스트를 반환한다.

    targets: ["claude-desktop"], ["claude-code"], 또는 ["claude-desktop", "claude-code"].
    None 이면 ["claude-desktop"] (하위 호환).
    """
    targets = targets or ["claude-desktop"]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")

    results = []
    for target in targets:
        path_func, label = TARGETS[target]
        results.append(_configure_one_target(path_func(), label, command=command, quiet=quiet))
    return results


def _build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="stocklens-setup",
        description="Register stocklens in Claude config (Desktop and/or Code CLI).",
    )
    p.add_argument(
        "command",
        nargs="?",
        default="stocklens",
        help="MCP 클라이언트가 실행할 커맨드 (기본: stocklens).",
    )
    p.add_argument(
        "--target",
        choices=["claude-desktop", "claude-code", "both", "auto"],
        default="auto",
        help=(
            "MCP 등록 대상. claude-desktop=Claude Desktop 앱, "
            "claude-code=Claude Code CLI, both=둘 다, auto=환경 자동 감지 "
            "(기본: auto). STOCKLENS_TARGET 환경변수로도 지정 가능."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="결과를 JSON으로 stdout에 출력 (Manager 연동용). 자동으로 --non-interactive를 겸한다.",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="배너/진행 로그를 찍지 않는다 (원래 프롬프트가 없으므로 동작 자체는 동일).",
    )
    return p


def main():
    args = _build_parser().parse_args()
    targets = _resolve_targets(args.target)
    quiet = args.json or args.non_interactive

    if not quiet:
        target_labels = ", ".join(TARGETS[t][1] for t in targets)
        print("==============================================")
        print("  StockLens - MCP Setup")
        print("==============================================")
        print(f"  Targets: {target_labels}")

    try:
        results = configure(args.command, targets=targets, quiet=quiet)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"  [ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"ok": True, "targets": results}, ensure_ascii=False))
        sys.exit(0)

    if not quiet:
        print()
        if "claude-desktop" in targets:
            print("Done! Claude Desktop 을 완전히 종료(트레이→Quit) 후 다시 실행하세요.")
        if "claude-code" in targets:
            print("Done! Claude Code 새 세션부터 stocklens 도구 사용 가능.")


if __name__ == "__main__":
    main()
