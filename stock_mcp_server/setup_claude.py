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
    2. uv tool bin 디렉토리 (`~/.local/bin` 등) — Manager 가 갱신하는 대상
    3. PATH 탐색 (shutil.which)
    4. sysconfig scripts 디렉토리 직접 탐색
    5. 최후 fallback: sys.executable + `-m stock_mcp_server.server`

    반환된 entry는 Claude Desktop이 PATH 환경변수와 무관하게 실행 가능.
    """
    # 1) 사용자가 명시적으로 절대 경로를 줬으면 그대로 사용
    if os.path.isabs(preferred_command) and Path(preferred_command).exists():
        return {"command": preferred_command}

    # 2) uv tool bin 디렉토리를 **PATH 보다 먼저** 본다.
    #
    # 실기기에서 확인한 사고: 옛 `pip install` 잔재가 시스템 Python 의 Scripts\ 에 남아
    # 있으면 PATH 순서상 그게 먼저 잡혀서, 설정 파일에 옛 실행 파일 경로가 박힌다. 그러면
    # Manager 로 최신 버전을 올려도 호스트 앱(Claude·ChatGPT)은 계속 옛 버전을 띄운다 —
    # "업데이트했는데 그대로다" 가 되고, 원인이 설정 파일 안에 있어서 찾기도 어렵다.
    # uv 가 관리하는 쪽이 Manager 가 실제로 갱신하는 대상이므로 그쪽을 먼저 쓴다.
    # (uv 없이 pip 로만 설치한 환경은 이 디렉토리가 없어 아래 PATH 탐색으로 내려간다.)
    for bin_dir in _uv_tool_bin_dirs():
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = bin_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}

    # 3) PATH 탐색
    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

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


def get_codex_config_path() -> Path:
    """Codex CLI의 MCP 서버 설정 — `~/.codex/config.toml`, `[mcp_servers.<name>]` 섹션.

    Windows에서 실제 설치본으로 이 경로를 확인했다. macOS/Linux도 관례상 같은 경로일
    가능성이 높으나 이 환경에서 직접 검증하지는 못했다.
    """
    return Path.home() / ".codex" / "config.toml"


# 하위 호환 — 기존 코드/외부 import 보존
def get_config_path() -> Path:
    return get_claude_desktop_config_path()


# (target name, 경로 함수, 사람이 읽는 라벨)
TARGETS: dict[str, tuple] = {
    "claude-desktop": (get_claude_desktop_config_path, "Claude Desktop"),
    "claude-code": (get_claude_code_config_path, "Claude Code CLI"),
    "codex": (get_codex_config_path, "Codex CLI"),
}


def _has_codex() -> bool:
    """codex 타겟을 쓸 수 있는 환경인지 — Codex CLI 또는 ChatGPT 데스크탑 앱.
    통합 이후 둘은 같은 `~/.codex/config.toml` 을 읽으므로 하나로 본다.

    ChatGPT 앱을 깔았지만 MCP 설정을 한 번도 안 건드린 사람은 이 폴더가 없을 수 있다 —
    그때는 아래 auto 판정이 결국 claude-desktop 으로 떨어진다(앱 자체를 찾아내는 일은
    OS별 설치 경로 탐색이 필요해서 LeetKit Manager 쪽이 담당한다)."""
    if shutil.which("codex"):
        return True
    return get_codex_config_path().parent.exists()


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
        if has_desktop:
            return ["claude-desktop"]
        # Claude 가 하나도 없으면 codex(ChatGPT 앱·Codex CLI)를 본다. 예전에는 무조건
        # claude-desktop 으로 떨어져서, ChatGPT 만 쓰는 사람에게 **없는 앱의 설정 파일**을
        # 만들고 "등록 완료"라고 말했다 — 시킨 대로 다 했는데 도구가 안 보이는 자리다.
        if _has_codex():
            return ["codex"]
        return ["claude-desktop"]
    raise ValueError(f"Invalid target: {arg}")


def _configure_one_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """단일 config 파일에 mcpServers.stocklens 등록. 변경 결과를 dict로 반환한다.

    quiet=True 면 사람용 진행 로그를 찍지 않는다(Manager의 --json/--non-interactive
    호출에서 stdout을 구조화 결과 하나로만 유지하기 위함). 파일 변경 로직 자체는
    quiet 여부와 무관하게 동일하다.

    Codex 등 TOML 기반 클라이언트는 config_path 확장자가 `.toml`이라 여기서
    `_configure_toml_target`으로 분기한다 — TARGETS 딕셔너리 구조를 바꾸지 않고도
    JSON/TOML 두 포맷을 같은 인터페이스로 지원.
    """
    if config_path.suffix == ".toml":
        return _configure_toml_target(config_path, label, command=command, quiet=quiet)

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


def _configure_toml_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """Codex처럼 TOML(`[mcp_servers.<name>]`)로 MCP 서버를 등록하는 클라이언트용.

    tomlkit으로 파싱·재작성해서 다른 mcp_servers.* 항목·주석은 그대로 두고 우리
    섹션만 추가/갱신한다(전체를 다시 문자열로 쓰는 JSON 경로와 달리, 파일에 이미
    다른 도구가 등록한 서버들이 섞여 있을 수 있어 round-trip 보존이 필요).
    """
    import tomlkit

    if not quiet:
        print()
        print(f"  → {label}")

    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if config_path.exists():
        backup_path = config_path.with_suffix(".toml.backup")
        backup_path.write_bytes(config_path.read_bytes())
        if not quiet:
            print(f"  [OK] Backup saved: {backup_path}")
        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            if not quiet:
                print("  [WARN] Existing config is corrupted. Creating new one.")
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table()

    entry = resolve_server_entry(command)
    server_table = tomlkit.table()
    server_table["command"] = entry["command"]
    if "args" in entry:
        server_table["args"] = entry["args"]
    doc["mcp_servers"][SERVER_KEY] = server_table

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

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
        "legacy_removed": [],
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


def _remove_one_target(config_path, label: str) -> dict:
    """한 설정 파일에서 우리 MCP 항목만 지운다. 다른 서버 항목·주석은 안 건드린다.

    파일이 없거나 항목이 없으면 "이미 없음"으로 성공 처리한다 — 해제를 두 번 눌러도
    실패로 보이면 안 된다. 지우기 전에 항상 백업을 남긴다(등록 때와 같은 규칙).
    """
    from pathlib import Path as _Path

    config_path = _Path(config_path)
    result = {"target_label": label, "config_path": str(config_path), "removed": False}
    if not config_path.exists():
        return result

    if config_path.suffix == ".toml":
        import tomlkit

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            return result
        config_path.with_suffix(".toml.backup").write_bytes(config_path.read_bytes())
        servers = doc.get("mcp_servers")
        if servers is not None:
            for key in [SERVER_KEY, *LEGACY_KEYS]:
                if servers.pop(key, None) is not None:
                    result["removed"] = True
        if result["removed"]:
            config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return result

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return result
    with open(config_path.with_suffix(".json.backup"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    servers = config.get("mcpServers") or {}
    for key in [SERVER_KEY, *LEGACY_KEYS]:
        if servers.pop(key, None) is not None:
            result["removed"] = True
    if result["removed"]:
        config["mcpServers"] = servers
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    return result


def remove(targets: list[str]) -> list[dict]:
    """선택한 타겟들에서 MCP 등록을 해제한다.

    Manager의 "MCP 등록" 모달에서 체크를 풀면 여기로 온다. 예전엔 해제 수단이 아예
    없어서, 체크박스가 토글처럼 보이는데 실제로는 "추가만" 됐다 — 체크를 풀고 등록을
    눌러도 그 설정이 그대로 남아 사용자 눈에는 먹통으로 보였다.
    """
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")
    results = []
    for target in targets:
        path_func, label = TARGETS[target]
        entry = _remove_one_target(path_func(), label)
        entry["target"] = target
        results.append(entry)
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
        choices=["claude-desktop", "claude-code", "both", "auto", "codex"],
        default="auto",
        help=(
            "MCP 등록 대상. claude-desktop=Claude Desktop 앱, "
            "claude-code=Claude Code CLI, both=둘 다, auto=환경 자동 감지 "
            "(기본: auto), codex=ChatGPT 앱·Codex CLI(같은 ~/.codex/config.toml 을 읽는다. "
            "auto 는 Claude 가 하나도 없을 때 이쪽을 고른다). "
            "STOCKLENS_TARGET 환경변수로도 지정 가능."
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
    p.add_argument(
        "--remove",
        action="store_true",
        help="등록을 해제한다(설정 파일에서 우리 항목만 삭제). Manager의 체크 해제가 이걸 쓴다.",
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

    # 해제는 등록과 완전히 다른 일이라 여기서 바로 갈라진다 — 키 검증·설치 확인 같은
    # 등록 절차를 탈 이유가 없다.
    if args.remove:
        try:
            removed = remove(targets)
        except Exception as e:  # noqa: BLE001 — 실패도 JSON 계약으로 알려야 한다
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            else:
                print(f"  [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))
        elif not quiet:
            for entry in removed:
                state = "해제됨" if entry["removed"] else "원래 없음"
                print(f"  [OK] {entry['target_label']}: {state}")
        sys.exit(0)

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
