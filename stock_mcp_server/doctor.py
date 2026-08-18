"""StockLens 설치·설정 진단 도구.

실행: `stocklens-doctor` 또는 `python -m stock_mcp_server.doctor`

체크 항목:
- uv 설치 여부 (Python 런타임 관리자)
- stocklens-mcp 패키지 import 가능 여부
- stocklens 실행 명령 탐색 (PATH / uv tool bin / sysconfig)
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
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
        _uv_tool_bin_dirs,
        _find_store_config_path,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from stock_mcp_server.setup_claude import (
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
        _uv_tool_bin_dirs,
        _find_store_config_path,
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


def _find_uv() -> str | None:
    """uv 실행 파일 경로. PATH뿐 아니라 설치 스크립트가 실제로 두는 위치까지 본다.

    PATH만 보면 오탐이 난다 — LeetKit Manager가 uv를 자동 설치해주면 uv는
    `~/.local/bin`에 생기지만 설치 스크립트는 *영구* PATH(레지스트리)만 갱신하므로,
    이미 실행 중인 프로세스에는 반영되지 않는다. 그 상태로 PATH만 확인하면 uv를
    멀쩡히 갖고 있는 사람에게 "uv를 설치하세요"라고 계속 안내하게 된다(실제로
    그렇게 떴다). DartLens·TelegramLens는 이미 이렇게 찾고 있었는데 여기만 빠져 있었다.
    """
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for bin_dir in (home / ".local" / "bin", home / ".cargo" / "bin"):
        for name in ("uv.exe", "uv"):
            candidate = bin_dir / name
            if candidate.exists():
                return str(candidate)
    return None


def check_uv() -> Check:
    c = Check("uv (Python runtime manager)")
    uv = _find_uv()
    if uv:
        c.ok("uv is installed")
        c.info(f"Path:       {uv}")
    else:
        # uv 없이도 stocklens가 동작 가능 (pip 설치 등)이지만,
        # 권장 설치 경로는 uv이므로 warn으로 안내.
        c.warn(
            "uv not found in PATH",
            fix=(
                "Install uv (recommended):\n"
                "  Windows: irm https://astral.sh/uv/install.ps1 | iex\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
            ),
        )
    return c


def check_package() -> Check:
    c = Check("Package (stocklens-mcp)")
    try:
        import stock_mcp_server  # noqa: F401
        c.ok("stocklens-mcp is importable")
        c.info(f"Location:   {Path(stock_mcp_server.__file__).parent}")
        c.info(f"Python:     {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        c.info(f"Executable: {sys.executable}")
    except ImportError:
        c.fail(
            "stocklens-mcp NOT importable in current interpreter",
            fix="uv tool install --force stocklens-mcp",
        )
    return c


def check_stocklens_command() -> Check:
    c = Check("Command (stocklens)")
    # 1) PATH 탐색
    exe = shutil.which("stocklens")
    if exe:
        c.ok("'stocklens' found in PATH")
        c.info(f"Path:       {exe}")
        return c

    # 2) uv tool bin 디렉토리 직접 확인
    for bin_dir in _uv_tool_bin_dirs():
        for name in ("stocklens.exe", "stocklens"):
            candidate = bin_dir / name
            if candidate.exists():
                # PATH에 없는 것 자체는 문제가 아니다 — MCP 등록은 절대경로로 하므로
                # 그대로 동작한다. 예전엔 warn이라 카드가 영영 "주의"로 남았는데,
                # 정작 안내문에 "무시 가능"이라고 적혀 있는 경고였다.
                c.ok("'stocklens' is installed")
                c.info(f"Path:       {candidate}")
                c.info("Not on PATH — MCP registration uses this absolute path, so no action is needed.")
                c.info(f'Add "{bin_dir}" to PATH only if you want to type the command in a terminal.')
                return c

    # 3) sysconfig scripts 디렉토리 (pip 설치 호환)
    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in ("stocklens.exe", "stocklens"):
            candidate = scripts_dir / name
            if candidate.exists():
                c.warn(
                    "'stocklens' exists in sysconfig scripts but not on PATH",
                    fix=f'Add to PATH: "{scripts_dir}"',
                )
                c.info(f"Path:       {candidate}")
                return c
    except Exception:
        pass

    # 4) 어디에도 없음
    c.fail(
        "'stocklens' command NOT found anywhere",
        fix="uv tool install --force stocklens-mcp",
    )
    return c


def label_to_target(label: str) -> str:
    return "claude-code" if "Code" in label else "claude-desktop"


def _check_config_file(label: str, config_path: Path, *, required: bool) -> Check:
    """단일 config 파일 점검. required=False 면 부재 시 SKIP."""
    c = Check(f"Config — {label}")

    # Store 버전은 앱이 격리된 공간에서 돌아, 우리가 띄우는 프로세스도 그 안에 갇힌다.
    # 그 자체가 고장은 아니고 실제로 잘 쓰는 사람도 있어서 막지는 않는다 — 다만 문제가
    # 생겼을 때 원인 후보가 하나 더 붙는 환경이라, 진단에서는 눈에 띄게 알린다.
    # info 로 찍으면 화면을 스쳐 지나가서, 정작 막힌 사람이 이 줄을 못 보고 넘어갔다.
    if "Packages" in str(config_path) and "LocalCache" in str(config_path):
        c.warn(
            "Microsoft Store 버전 Claude Desktop 을 쓰고 계십니다 (격리된 경로). "
            "지금 잘 되신다면 그대로 쓰셔도 됩니다.",
            fix=(
                "문제가 있다면 일반 설치판을 권합니다: "
                "① 설정 > 설치된 앱에서 Claude 제거 "
                "② %LOCALAPPDATA%\\Packages 에서 Claude_* 폴더 삭제 "
                "③ https://claude.ai/download 에서 설치 파일로 재설치 "
                "(스토어 페이지 말고 이 주소에서 받으세요)"
            ),
        )
    c.info(f"Path:       {config_path}")

    # 두 Desktop config 파일이 동시에 존재하는 비정상 케이스 경고
    if "Claude" in label and "Code" not in label:
        store = _find_store_config_path()
        std_appdata = os.environ.get("APPDATA")
        std_path = Path(std_appdata) / "Claude" / "claude_desktop_config.json" if std_appdata else None
        if store and std_path and store.exists() and std_path.exists() and store != std_path:
            c.warn(
                f"Both Store and standard config files exist. Active: {config_path}",
                fix=f"Remove unused: {std_path if config_path == store else store}",
            )

    if not config_path.exists():
        if required:
            c.fail("Config file does not exist", fix="stocklens-setup")
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
        return c

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        c.fail(f"Config is not valid JSON: {e}", fix="Back up and re-run stocklens-setup")
        return c
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcpServers", {}) or {}
    entry = servers.get(SERVER_KEY)

    legacy_found = [k for k in LEGACY_KEYS if k in servers]
    if legacy_found:
        c.warn(
            f"Legacy entries present: {legacy_found}",
            fix="stocklens-setup (auto-removes)",
        )

    if not entry:
        if required or legacy_found:
            msg = (
                f"'{SERVER_KEY}' entry missing in mcpServers"
                + (f" (legacy {legacy_found} present)" if legacy_found else "")
            )
            c.fail(msg, fix=f"stocklens-setup --target {label_to_target(label)}")
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
        return c

    cmd = entry.get("command")
    args = entry.get("args", [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.fail(f"Command file missing: {cmd}", fix="stocklens-setup")
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — client will fail to launch the server",
                fix="stocklens-setup",
            )

    return c


def check_config_desktop() -> Check:
    return _check_config_file(
        "Claude Desktop", get_claude_desktop_config_path(), required=False
    )


def check_config_code() -> Check:
    return _check_config_file(
        "Claude Code CLI", get_claude_code_config_path(), required=False
    )


def check_config_codex() -> Check:
    return _check_config_toml_file("Codex CLI", get_codex_config_path(), required=False)


def _check_config_toml_file(label: str, config_path: Path, *, required: bool) -> Check:
    """TOML 기반 클라이언트(`~/.codex/config.toml` 의 `[mcp_servers.<key>]`) config 점검.

    JSON 쪽 `_check_config_file` 과 같은 계약(entry 유무 · command 유효성)을 TOML 구조로
    다시 쓴 것이고, `setup_claude._configure_toml_target()` 이 쓰는 구조를 읽기만 한다.

    이 검사가 없던 동안 ChatGPT 만 쓰는 사람은 도구가 정상 동작하는데도 아래 종합 판정이
    "어디에도 등록 안 됨" FAIL 로 떨어졌다. 지원 번들에도 그 줄이 들어가서 원인을 찾는
    쪽까지 엉뚱한 곳을 보게 된다."""
    c = Check(f"Config — {label}")
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail("Config file does not exist", fix="stocklens-setup --target codex")
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        import tomlkit

        cfg = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    entry = (cfg.get("mcp_servers", {}) or {}).get(SERVER_KEY)
    if not entry:
        if required:
            c.fail(
                f"'{SERVER_KEY}' entry missing in mcp_servers",
                fix="stocklens-setup --target codex",
            )
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
            c.summary = f"'{SERVER_KEY}' entry not present (target not in use — OK)"
        return c

    cmd = entry.get("command")
    args = list(entry.get("args") or [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.fail(f"Command file missing: {cmd}", fix="stocklens-setup --target codex")
        return c

    resolved = shutil.which(cmd)
    if resolved:
        c.ok(f"Command resolvable via PATH: {resolved}")
    else:
        c.fail(
            f"Command '{cmd}' not in PATH — client will fail to launch the server",
            fix="stocklens-setup --target codex",
        )
    return c


def check_at_least_one_config(*configs: Check) -> Check:
    c = Check("Registered targets")
    registered = [
        cc for cc in configs
        if cc.status == "ok" or (cc.status == "warn" and "Legacy" in " ".join(cc.lines))
    ]
    if registered:
        c.ok(f"{len(registered)} target(s) configured")
        return c
    c.fail(
        "stocklens not registered in any MCP client (Claude Desktop / Code / ChatGPT·Codex)",
        fix="stocklens-setup --target {claude-desktop|claude-code|both|codex}",
    )
    return c


# 하위 호환 — 기존 import/외부 호출 보존
def check_config() -> Check:
    return check_config_desktop()


def check_license() -> Check:
    """StockLens 라이선스 활성화 상태. 키 원문은 절대 노출하지 않는다."""
    c = Check("License (StockLens)")
    from stock_mcp_server import licensing

    key = licensing.stored_key()
    if not key:
        c.fail("No license key stored", fix="stocklens-activate <라이선스-키>")
        return c

    res = licensing.verify_key(key)
    if not res["valid"]:
        c.fail(
            f"Stored license key is invalid ({res.get('reason', 'unknown')})",
            fix="stocklens-activate <라이선스-키>",
        )
        return c

    reason = licensing.license_block_reason()
    if reason in ("expired", "revoked", "clock"):
        c.fail(
            {
                "expired": "License has expired",
                "revoked": "License has been revoked",
                "clock": "System clock is set in the past",
            }[reason],
            fix="stocklens-activate <라이선스-키>" if reason == "expired" else None,
        )
        return c

    c.ok("License active")
    license_id = res.get("license_id") or ""
    if license_id:
        c.info(f"license_id: {licensing.mask_tail(license_id.upper())}")
    return c


STATUS_ICON = {
    "ok": "[ OK ]",
    "warn": "[WARN]",
    "fail": "[FAIL]",
    "info-skip": "[SKIP]",
    None: "[ ?  ]",
}


def print_check(c: Check):
    icon = STATUS_ICON.get(c.status, "[ ?  ]")
    print(f"{icon} {c.name}")
    for line in c.lines:
        print(f"       {line}")
    if c.fix:
        print(f"       Fix: {c.fix}")
    print()


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="stocklens-doctor",
        description="StockLens installation/license/data-connectivity diagnosis.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print a structured DiagnosticReport as JSON (for Manager/automation) instead of human text.",
    )
    p.add_argument(
        "--online",
        action="store_true",
        help="Include network reachability checks (KR/US sample quote + update check). Only applies with --json.",
    )
    return p


def _run_json_mode(*, online: bool) -> int:
    from stock_mcp_server import diagnostics

    try:
        report = diagnostics.run_diagnostics(online=online)
    except Exception as e:
        # 개별 체크는 diagnostics._safe_check로 이미 보호되지만, 이 바깥 레이어도
        # 예상 못한 예외에 traceback 대신 항상 JSON을 내보내도록 한 번 더 막는다.
        print(json.dumps(
            {"schema_version": diagnostics.SCHEMA_VERSION, "overall": "fail", "ok": False,
             "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        ))
        return 1
    print(report.to_json())
    return 0 if report.overall != "fail" else 1


def main():
    # Windows cp949 터미널 호환을 위해 stdout UTF-8 시도
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 서버와 같은 TLS 기준으로 진단한다.
    from stock_mcp_server import _tls

    _tls.apply()

    args = _build_parser().parse_args()

    if args.json:
        sys.exit(_run_json_mode(online=args.online))

    print("=" * 60)
    print("  StockLens Doctor - Installation Diagnosis")
    print("=" * 60)
    print()

    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return Check(fn.__name__).fail(f"진단 중 예상치 못한 오류: {type(e).__name__}: {e}")

    desktop_check = _safe(check_config_desktop)
    code_check = _safe(check_config_code)
    codex_check = _safe(check_config_codex)

    checks = [
        _safe(check_uv),
        _safe(check_package),
        _safe(check_stocklens_command),
        desktop_check,
        code_check,
        codex_check,
        _safe(check_at_least_one_config, desktop_check, code_check, codex_check),
        _safe(check_license),
    ]

    for c in checks:
        print_check(c)

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
        print("  (tray icon -> Quit) and restart.")
    print("=" * 60)


if __name__ == "__main__":
    main()
