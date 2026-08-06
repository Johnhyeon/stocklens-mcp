"""StockLens 진단 데이터 모델 + 실행 로직 (출력 부작용 없음).

`doctor.py`(사람용 텍스트 CLI)와 Manager 연동(JSON)이 공유하는 단일 진단 계약.
이 모듈의 함수들은 print를 하지 않는다 — 결과를 `DiagnosticCheck`/
`DiagnosticReport` 데이터클래스로 반환할 뿐이고, 보여주는 방식은 호출자 몫이다.

`DiagnosticReport.to_dict()`의 최상위 필드 이름(schema_version/product/
package_name/installed_version/latest_version/update_available/overall/
checked_at/license/targets/checks)과 checks[] 항목 필드 이름(id/status/summary/
details/repairable/repair_id/action)은 Manager 공통 계약(LeetKit Manager Program
Requirements 3.1)과 문자 그대로 일치해야 한다 — DartLens/TelegramLens의
동일 계약과 이름이 갈리면 Manager가 Lens별 파서를 따로 둬야 하므로 여기서
임의로 새 이름을 만들지 말 것.

검사 ID (Manager가 파싱하는 고정 식별자, 값 자체를 바꾸지 말 것):
    PACKAGE_IMPORTABLE, COMMAND_AVAILABLE, PYTHON_SUPPORTED, MCP_CONFIG_VALID,
    LICENSE_ACTIVE, CACHE_WRITABLE, KR_DATA_REACHABLE, US_DATA_REACHABLE,
    UPDATE_CHECK_REACHABLE

기본(online=False) 진단은 네트워크 호출이 전혀 없어 수 초 내 끝난다.
online=True 일 때만 KR_DATA_REACHABLE/US_DATA_REACHABLE/UPDATE_CHECK_REACHABLE가
실행되며, 각각 국내·미국 대표 종목 1개와 PyPI/GitHub만 조회한다(전수 조사 아님).
latest_version/update_available도 --online일 때만 채워진다(기본 모드는 무네트워크
원칙을 지키므로 null).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import sysconfig
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PRODUCT = "stocklens"
PACKAGE_NAME = "stocklens-mcp"

# pyproject.toml의 requires-python과 동일하게 유지할 것.
MIN_PYTHON = (3, 11)

_ONLINE_CHECK_TIMEOUT = 8.0  # 초. 대표 종목 1개 조회가 이 시간을 넘기면 미도달로 간주.

# 검사 실패(status="fail") 시 붙는 오류 코드 카탈로그.
# 각 항목은 summary(무엇이 문제인지)/impact(무엇이 안 되는지)/action(어떻게 고치는지)/
# repairable(사용자가 직접 고칠 수 있는지)을 고정 필드로 갖는다.
ERROR_CATALOG: dict[str, dict] = {
    "DEPENDENCY_BROKEN": {
        "summary": "StockLens 패키지 또는 실행 커맨드가 현재 환경에서 정상 동작하지 않습니다.",
        "impact": "MCP 서버 자체가 실행되지 않습니다.",
        "action": "uv tool install --force stocklens-mcp",
        "repairable": True,
    },
    "UNSUPPORTED_PYTHON": {
        "summary": f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요합니다.",
        "impact": "일부 문법/의존성이 동작하지 않을 수 있습니다.",
        "action": "Python 3.11 이상 설치 후 stocklens-mcp를 재설치하세요.",
        "repairable": True,
    },
    "MCP_CONFIG_MISSING": {
        "summary": "Claude Desktop/Code 설정에 stocklens 항목이 없거나 실행 불가능합니다.",
        "impact": "Claude에서 StockLens 도구가 보이지 않습니다.",
        "action": "stocklens-setup --target both",
        "repairable": True,
    },
    "STOCKLENS_LICENSE_MISSING": {
        "summary": "저장된 라이선스 키가 없습니다.",
        "impact": "모든 도구가 잠김 상태로 응답합니다.",
        "action": "stocklens-activate <라이선스-키>",
        "repairable": True,
    },
    "STOCKLENS_LICENSE_INVALID": {
        "summary": "저장된 라이선스 키가 서명 검증에 실패했습니다.",
        "impact": "모든 도구가 잠김 상태로 응답합니다.",
        "action": "구매 시 발송된 키를 다시 정확히 붙여넣어 stocklens-activate로 재활성화하세요.",
        "repairable": True,
    },
    "CACHE_NOT_WRITABLE": {
        "summary": "로컬 출력/캐시 폴더에 쓰기 권한이 없습니다.",
        "impact": "Excel 내보내기, 메트릭 로그 저장이 실패합니다.",
        "action": "폴더 권한을 확인하거나 STOCKLENS_HOME 환경변수로 쓰기 가능한 경로를 지정하세요.",
        "repairable": True,
    },
    "KR_DATA_UNREACHABLE": {
        "summary": "네이버 증권에서 국내 시세를 가져오지 못했습니다.",
        "impact": "한국 주식 도구가 동작하지 않습니다.",
        "action": "인터넷 연결/방화벽을 확인하세요. 일시적 장애일 수 있습니다.",
        "repairable": False,
    },
    "US_DATA_UNREACHABLE": {
        "summary": "Yahoo Finance에서 미국 시세를 가져오지 못했습니다.",
        "impact": "미국 주식 도구가 동작하지 않습니다.",
        "action": "인터넷 연결/방화벽을 확인하세요. 일시적 장애일 수 있습니다.",
        "repairable": False,
    },
    "UPDATE_CHECK_FAILED": {
        "summary": "PyPI/GitHub에서 최신 버전 정보를 가져오지 못했습니다.",
        "impact": "업데이트 알림만 못 받을 뿐 기존 기능에는 영향이 없습니다.",
        "action": "네트워크 상태를 확인하거나 무시해도 됩니다.",
        "repairable": False,
    },
}


@dataclass
class DiagnosticCheck:
    id: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    critical: bool
    summary: str
    detail: list[str] = field(default_factory=list)
    error_code: str | None = None
    fix: str | None = None

    def to_dict(self) -> dict:
        details: dict = {}
        if self.detail:
            details["lines"] = list(self.detail)
        if self.error_code:
            details["error_code"] = self.error_code
            impact = ERROR_CATALOG.get(self.error_code, {}).get("impact")
            if impact:
                details["impact"] = impact
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "details": details,
            # StockLens는 아직 `--repair`를 구현하지 않아 전부 수동 조치(action)만 제공한다.
            "repairable": False,
            "repair_id": None,
            "action": self.fix,
            "critical": self.critical,
        }


@dataclass
class LicenseSummary:
    status: str  # "active" | "missing" | "invalid"
    license_id_masked: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "license_id_masked": self.license_id_masked}


@dataclass
class DiagnosticReport:
    schema_version: int
    checked_at: str
    installed_version: str
    online: bool
    duration_ms: float
    overall: str  # "ok" | "degraded" | "fail"
    ok: bool
    license: LicenseSummary
    targets: list[str]
    checks: list[DiagnosticCheck]
    latest_version: str | None = None
    update_available: bool | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "product": PRODUCT,
            "package_name": PACKAGE_NAME,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "overall": self.overall,
            "checked_at": self.checked_at,
            "online": self.online,
            "duration_ms": self.duration_ms,
            "license": self.license.to_dict(),
            "targets": self.targets,
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _overall_status(checks: list[DiagnosticCheck]) -> str:
    """critical 검사의 fail만 전체를 'fail'로 만든다.

    나머지(비-critical fail/warn — 예: 국내만 불통, 캐시 못 씀)는 'degraded'로
    구분해서, 공급자 하나의 장애와 전체 불능을 섞어 보고하지 않는다.
    """
    if any(c.status == "fail" and c.critical for c in checks):
        return "fail"
    if any(c.status in ("fail", "warn") for c in checks):
        return "degraded"
    return "ok"


# --- 오프라인 검사 (네트워크 호출 없음) ---


def _check_package_importable() -> DiagnosticCheck:
    try:
        import stock_mcp_server  # noqa: F401

        return DiagnosticCheck(
            id="PACKAGE_IMPORTABLE",
            status="ok",
            critical=True,
            summary="stocklens-mcp import 가능",
            detail=[
                f"위치: {Path(stock_mcp_server.__file__).parent}",
                f"인터프리터: {sys.executable}",
            ],
        )
    except ImportError as e:
        return DiagnosticCheck(
            id="PACKAGE_IMPORTABLE",
            status="fail",
            critical=True,
            summary="stocklens-mcp를 현재 인터프리터에서 import할 수 없습니다.",
            detail=[str(e)],
            error_code="DEPENDENCY_BROKEN",
            fix="uv tool install --force stocklens-mcp",
        )


def _check_command_available() -> DiagnosticCheck:
    exe = shutil.which("stocklens")
    if exe:
        return DiagnosticCheck(
            id="COMMAND_AVAILABLE",
            status="ok",
            critical=True,
            summary="'stocklens' 커맨드를 PATH에서 찾았습니다.",
            detail=[exe],
        )

    from stock_mcp_server.setup_claude import _uv_tool_bin_dirs

    for bin_dir in _uv_tool_bin_dirs():
        for name in ("stocklens.exe", "stocklens"):
            candidate = bin_dir / name
            if candidate.exists():
                return DiagnosticCheck(
                    id="COMMAND_AVAILABLE",
                    status="warn",
                    critical=True,
                    summary="'stocklens' 커맨드는 존재하지만 PATH에 없습니다.",
                    detail=[str(candidate)],
                    fix=f'PATH에 "{bin_dir}" 추가 (또는 stocklens-setup이 절대경로로 등록하므로 무시 가능)',
                )

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in ("stocklens.exe", "stocklens"):
            candidate = scripts_dir / name
            if candidate.exists():
                return DiagnosticCheck(
                    id="COMMAND_AVAILABLE",
                    status="warn",
                    critical=True,
                    summary="'stocklens' 커맨드가 sysconfig scripts에는 있지만 PATH에 없습니다.",
                    detail=[str(candidate)],
                    fix=f'PATH에 "{scripts_dir}" 추가',
                )
    except Exception:
        pass

    return DiagnosticCheck(
        id="COMMAND_AVAILABLE",
        status="fail",
        critical=True,
        summary="'stocklens' 커맨드를 찾을 수 없습니다.",
        error_code="DEPENDENCY_BROKEN",
        fix="uv tool install --force stocklens-mcp",
    )


def _check_python_supported() -> DiagnosticCheck:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] >= MIN_PYTHON:
        return DiagnosticCheck(
            id="PYTHON_SUPPORTED",
            status="ok",
            critical=True,
            summary=f"Python {ver} (지원 범위)",
            detail=[sys.executable],
        )
    return DiagnosticCheck(
        id="PYTHON_SUPPORTED",
        status="fail",
        critical=True,
        summary=f"Python {ver}는 지원 범위(>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}) 미만입니다.",
        error_code="UNSUPPORTED_PYTHON",
        fix="Python 3.11 이상 설치 후 stocklens-mcp 재설치",
    )


def _registered_targets() -> list[str]:
    """실제로 등록·실행 가능한 MCP 타겟 slug 목록 ("claude-desktop"/"claude-code").

    `_check_mcp_config_valid()`와 top-level `targets` 필드가 같은 판정을 쓰도록
    이 함수 하나로 판정 로직을 모은다.
    """
    from stock_mcp_server.setup_claude import (
        SERVER_KEY,
        get_claude_code_config_path,
        get_claude_desktop_config_path,
        get_codex_config_path,
    )

    def _resolvable(cmd: str | None) -> bool:
        return bool(cmd) and (
            (Path(cmd).is_absolute() and Path(cmd).exists())
            or (not Path(cmd).is_absolute() and shutil.which(cmd) is not None)
        )

    slugs = [
        ("claude-desktop", get_claude_desktop_config_path()),
        ("claude-code", get_claude_code_config_path()),
    ]

    result: list[str] = []
    for slug, path in slugs:
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = (cfg.get("mcpServers") or {}).get(SERVER_KEY)
        if not entry:
            continue
        if _resolvable(entry.get("command")):
            result.append(slug)

    # Codex는 TOML(`~/.codex/config.toml`, [mcp_servers.<key>])이라 위 JSON 루프와 구조가
    # 달라 별도로 읽는다 — setup_claude._configure_toml_target()이 쓰는 것과 동일한 구조.
    codex_path = get_codex_config_path()
    if codex_path.exists():
        try:
            import tomlkit

            codex_cfg = tomlkit.parse(codex_path.read_text(encoding="utf-8"))
            codex_entry = (codex_cfg.get("mcp_servers") or {}).get(SERVER_KEY)
            if codex_entry and _resolvable(codex_entry.get("command")):
                result.append("codex")
        except Exception:
            pass

    return result


def _check_mcp_config_valid(targets: list[str]) -> DiagnosticCheck:
    label_by_slug = {"claude-desktop": "Claude Desktop", "claude-code": "Claude Code CLI", "codex": "Codex CLI"}
    if targets:
        labels = [label_by_slug[t] for t in targets]
        return DiagnosticCheck(
            id="MCP_CONFIG_VALID",
            status="ok",
            critical=True,
            summary=f"{', '.join(labels)}에 정상 등록됨",
            detail=[f"등록된 타겟: {', '.join(targets)}"],
        )
    return DiagnosticCheck(
        id="MCP_CONFIG_VALID",
        status="fail",
        critical=True,
        summary="Claude Desktop/Code/Codex 어디에도 stocklens가 정상 등록돼 있지 않습니다.",
        error_code="MCP_CONFIG_MISSING",
        fix="stocklens-setup --target both",
    )


def _license_summary() -> LicenseSummary:
    from stock_mcp_server import licensing

    key = licensing.stored_key()
    if not key:
        return LicenseSummary(status="missing")

    res = licensing.verify_key(key)
    if not res["valid"]:
        return LicenseSummary(status="invalid")

    license_id = res.get("license_id") or ""
    masked = licensing.mask_tail(license_id.upper()) if license_id else None
    return LicenseSummary(status="active", license_id_masked=masked)


def _check_license_active(summary: LicenseSummary) -> DiagnosticCheck:
    if summary.status == "missing":
        return DiagnosticCheck(
            id="LICENSE_ACTIVE",
            status="fail",
            critical=True,
            summary="저장된 라이선스 키가 없습니다.",
            error_code="STOCKLENS_LICENSE_MISSING",
            fix="stocklens-activate <라이선스-키>",
        )
    if summary.status == "invalid":
        return DiagnosticCheck(
            id="LICENSE_ACTIVE",
            status="fail",
            critical=True,
            summary="저장된 라이선스 키가 유효하지 않습니다.",
            error_code="STOCKLENS_LICENSE_INVALID",
            fix="stocklens-activate <라이선스-키>",
        )
    return DiagnosticCheck(
        id="LICENSE_ACTIVE",
        status="ok",
        critical=True,
        summary="라이선스 활성화됨",
        detail=[f"license_id_masked: {summary.license_id_masked}"] if summary.license_id_masked else [],
    )


def _check_cache_writable() -> DiagnosticCheck:
    from stock_mcp_server._excel import get_snapshot_dir

    try:
        folder = get_snapshot_dir()
        probe = folder / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return DiagnosticCheck(
            id="CACHE_WRITABLE",
            status="ok",
            critical=False,
            summary="캐시/출력 폴더에 쓰기 가능",
            detail=[str(folder)],
        )
    except Exception as e:
        return DiagnosticCheck(
            id="CACHE_WRITABLE",
            status="fail",
            critical=False,
            summary="캐시/출력 폴더에 쓸 수 없습니다.",
            detail=[str(e)],
            error_code="CACHE_NOT_WRITABLE",
            fix="폴더 권한 확인 또는 STOCKLENS_HOME 환경변수로 다른 경로 지정",
        )


def _safe_check(fn, *args, fallback_id: str, fallback_critical: bool = True, **kwargs) -> DiagnosticCheck:
    """개별 체크 함수 하나가 예상 못한 예외를 던져도 전체 리포트가 죽지 않게 감싼다.

    각 체크 내부에 이미 자체 try/except가 있지만(예: 파일 읽기, crypto 검증),
    여기서 한 번 더 막아둬야 '오프라인 진단은 절대 traceback으로 안 죽는다'는
    계약을 실제로 보장할 수 있다 — 체크 로직이 하나 늘어날 때마다 그 안에
    빠짐없이 try/except를 넣었는지 매번 감사하는 것보다 이 안전망이 낫다.

    fallback_critical은 이 체크가 정상 동작했을 때의 critical 값과 맞춰서
    넘겨야 한다 — 그래야 예컨대 LICENSE_ACTIVE가 죽었을 때도 전체 상태가
    'degraded'가 아니라 'fail'로 정확히 올라간다(critical=False로 뭉뚱그리면
    심각한 체크의 크래시가 사소한 경고처럼 보인다).
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return DiagnosticCheck(
            id=fallback_id,
            status="fail",
            critical=fallback_critical,
            summary=f"진단 중 예상치 못한 오류: {type(e).__name__}",
            detail=[str(e)],
        )


def _skip(check_id: str) -> DiagnosticCheck:
    return DiagnosticCheck(
        id=check_id,
        status="skip",
        critical=False,
        summary="온라인 진단 생략 (--online 으로 실행하면 확인합니다)",
    )


# --- 온라인 검사 (online=True 일 때만) ---


async def _check_kr_data_reachable() -> DiagnosticCheck:
    from stock_mcp_server.naver import get_current_price

    try:
        data = await asyncio.wait_for(get_current_price("005930"), timeout=_ONLINE_CHECK_TIMEOUT)
    except Exception as e:
        return DiagnosticCheck(
            id="KR_DATA_REACHABLE",
            status="fail",
            critical=False,
            summary="네이버 증권(국내) 시세 조회 실패",
            detail=[f"{type(e).__name__}: {e}"],
            error_code="KR_DATA_UNREACHABLE",
        )

    queried_at = datetime.now(timezone.utc).isoformat()
    name = data.get("name")
    price = data.get("price")
    # 휴장일에도 최근 종가는 내려오므로 장 상태는 실패 조건에 넣지 않는다.
    if name and isinstance(price, (int, float)) and price > 0:
        return DiagnosticCheck(
            id="KR_DATA_REACHABLE",
            status="ok",
            critical=False,
            summary=f"{name} 시세 정상 조회 ({price:,})",
            detail=[f"조회 시각: {queried_at}"],
        )
    return DiagnosticCheck(
        id="KR_DATA_REACHABLE",
        status="fail",
        critical=False,
        summary="네이버 증권 응답에 필수 필드가 없습니다.",
        detail=[f"name={name!r} price={price!r} 조회 시각: {queried_at}"],
        error_code="KR_DATA_UNREACHABLE",
    )


async def _check_us_data_reachable() -> DiagnosticCheck:
    from stock_mcp_server import yfinance_source as us

    try:
        data = await asyncio.wait_for(us.get_price("AAPL"), timeout=_ONLINE_CHECK_TIMEOUT)
    except Exception as e:
        return DiagnosticCheck(
            id="US_DATA_REACHABLE",
            status="fail",
            critical=False,
            summary="Yahoo Finance(미국) 시세 조회 실패",
            detail=[f"{type(e).__name__}: {e}"],
            error_code="US_DATA_UNREACHABLE",
        )

    queried_at = datetime.now(timezone.utc).isoformat()
    name = (data or {}).get("name")
    price = (data or {}).get("price")
    if data and name and isinstance(price, (int, float)) and price > 0:
        return DiagnosticCheck(
            id="US_DATA_REACHABLE",
            status="ok",
            critical=False,
            summary=f"{name} 시세 정상 조회 ({price:,})",
            detail=[f"조회 시각: {queried_at}"],
        )
    return DiagnosticCheck(
        id="US_DATA_REACHABLE",
        status="fail",
        critical=False,
        summary="Yahoo Finance 응답에 필수 필드가 없습니다.",
        detail=[f"name={name!r} price={price!r} 조회 시각: {queried_at}"],
        error_code="US_DATA_UNREACHABLE",
    )


async def _check_update_check_reachable() -> tuple[DiagnosticCheck, str | None]:
    """PyPI/GitHub 업데이트 확인. (체크 결과, 최신 버전 문자열|None)을 함께 반환한다.

    latest_version은 top-level 필드(latest_version/update_available)에도 쓰이므로,
    호출부가 이 체크를 다시 파싱하지 않도록 별도 값으로 얹어 반환한다.
    """
    from stock_mcp_server._update_check import _fetch_latest

    try:
        result = await asyncio.wait_for(_fetch_latest(), timeout=_ONLINE_CHECK_TIMEOUT)
    except Exception:
        result = None

    if result is None:
        return (
            DiagnosticCheck(
                id="UPDATE_CHECK_REACHABLE",
                status="fail",
                critical=False,
                summary="PyPI/GitHub 업데이트 확인에 실패했습니다.",
                error_code="UPDATE_CHECK_FAILED",
            ),
            None,
        )
    latest, _notes = result
    return (
        DiagnosticCheck(
            id="UPDATE_CHECK_REACHABLE",
            status="ok",
            critical=False,
            summary=f"업데이트 확인 정상 (PyPI 최신: v{latest})",
        ),
        latest,
    )


async def run_diagnostics_async(*, online: bool = False) -> DiagnosticReport:
    """`run_diagnostics`의 async 코어. 이미 실행 중인 이벤트 루프(예: MCP 도구)
    안에서는 이 쪽을 직접 await 한다 — `run_diagnostics()`는 내부에서
    `asyncio.run()`을 쓰므로 루프 안에서 부르면 RuntimeError가 난다."""
    start = time.monotonic()

    try:
        targets = _registered_targets()
    except Exception:
        targets = []  # 보수적 기본값 — '등록 안 됨'으로 취급
    try:
        license_summary = _license_summary()
    except Exception:
        license_summary = LicenseSummary(status="missing")  # 보수적 기본값

    checks: list[DiagnosticCheck] = [
        _safe_check(_check_package_importable, fallback_id="PACKAGE_IMPORTABLE"),
        _safe_check(_check_command_available, fallback_id="COMMAND_AVAILABLE"),
        _safe_check(_check_python_supported, fallback_id="PYTHON_SUPPORTED"),
        _safe_check(_check_mcp_config_valid, targets, fallback_id="MCP_CONFIG_VALID"),
        _safe_check(_check_license_active, license_summary, fallback_id="LICENSE_ACTIVE"),
        _safe_check(_check_cache_writable, fallback_id="CACHE_WRITABLE", fallback_critical=False),
    ]

    latest_version: str | None = None
    if online:
        kr_check, us_check, (update_check, latest_version) = await asyncio.gather(
            _check_kr_data_reachable(),
            _check_us_data_reachable(),
            _check_update_check_reachable(),
        )
        checks.extend([kr_check, us_check, update_check])
    else:
        checks.extend([
            _skip("KR_DATA_REACHABLE"),
            _skip("US_DATA_REACHABLE"),
            _skip("UPDATE_CHECK_REACHABLE"),
        ])

    from stock_mcp_server import __version__
    from stock_mcp_server._update_check import _version_gt

    update_available = _version_gt(latest_version, __version__) if latest_version else None

    overall = _overall_status(checks)
    return DiagnosticReport(
        schema_version=SCHEMA_VERSION,
        checked_at=datetime.now(timezone.utc).isoformat(),
        installed_version=__version__,
        online=online,
        duration_ms=round((time.monotonic() - start) * 1000, 1),
        overall=overall,
        ok=(overall == "ok"),
        license=license_summary,
        targets=targets,
        checks=checks,
        latest_version=latest_version,
        update_available=update_available,
    )


def run_diagnostics(*, online: bool = False) -> DiagnosticReport:
    """동기 진입점(CLI용). 출력 부작용 없음 — 결과를 DiagnosticReport로 반환만 한다.

    online=False(기본)면 네트워크 호출이 전혀 없어 수 초 내 끝난다.
    이미 이벤트 루프 안이라면 `run_diagnostics_async`를 직접 await할 것.
    """
    return asyncio.run(run_diagnostics_async(online=online))
