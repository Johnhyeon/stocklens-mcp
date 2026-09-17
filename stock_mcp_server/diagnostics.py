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
    UPDATE_CHECK_REACHABLE, RECENT_TOOL_FAILURES

RECENT_TOOL_FAILURES 는 세 Lens 공통 검사다(DartLens·TelegramLens 도 같은 ID·같은 판정).
metrics 기록만 읽으므로 기본 모드에서도 실행된다.

기본(online=False) 진단은 네트워크 호출이 전혀 없어 수 초 내 끝난다.
online=True 일 때만 KR_DATA_REACHABLE/US_DATA_REACHABLE/UPDATE_CHECK_REACHABLE가
실행되며, 각각 국내·미국 대표 종목 1개와 PyPI/GitHub만 조회한다(전수 조사 아님).
latest_version/update_available도 --online일 때만 채워진다(기본 모드는 무네트워크
원칙을 지키므로 null).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import sysconfig
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from stock_mcp_server._error_class import action_for, classify_error

SCHEMA_VERSION = 1
PRODUCT = "stocklens"
PACKAGE_NAME = "stocklens-mcp"
_LENS_NAME = "StockLens"  # _error_class.action_for 가 화면 문구에 넣는 이름

# pyproject.toml의 requires-python과 동일하게 유지할 것.
MIN_PYTHON = (3, 11)

_ONLINE_CHECK_TIMEOUT = 8.0  # 초. 대표 종목 1개 조회가 이 시간을 넘기면 미도달로 간주.

# details.lines 는 Manager 상세 창에 그대로 뜬다(명령어·경로처럼 보이는 줄만 걸러진다).
# 그래서 줄 앞은 고객이 읽을 한국어로 쓰고, 예외 원문은 줄 끝 괄호 안에 짧게만 붙인다.
# URL·쿼리스트링·키처럼 보이는 덩어리는 지운다.
_RAW_MAX = 80
_URL_RE = re.compile(r"https?://\S+")
_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_SECRETISH_RE = re.compile(r"[A-Za-z0-9_\-]{32,}|[A-Fa-f0-9]{24,}|[A-Z2-7]{24,}")


def _redact(text: object) -> str:
    t = _URL_RE.sub("…", str(text or ""))
    t = _QUERY_RE.sub("?…", t)
    return _SECRETISH_RE.sub("…", t).strip()


def _short(exc: BaseException) -> str:
    """괄호 안에 붙일 예외 원문. 예외 이름 + 가린 메시지, 80자 이내."""
    msg = _redact(exc)
    raw = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return raw if len(raw) <= _RAW_MAX else raw[: _RAW_MAX - 1] + "…"


# RECENT_TOOL_FAILURES: 최근 이틀(오늘·어제 metrics 파일), 도구별 줄은 최대 8줄·원문 120자.
_RECENT_LOOKBACK_DAYS = 2
_RECENT_DETAIL_MAX_LINES = 8
_RECENT_DETAIL_CHARS = 120

# 고객 문구(summary/action)는 LeetKit Manager 화면에 그대로 뜬다. 규칙:
# - 터미널 명령·환경변수 이름·예외 이름을 쓰지 않는다. 그런 원문은 details.lines 에만
#   남긴다(Manager [결과 복사]로 지원 쪽에 오는 용도).
# - 할 일은 Manager 버튼 하나로, 이름은 화면 글자 그대로. 해요체.
# DartLens·TelegramLens 와 같은 상태는 Lens 이름만 다른 같은 문장을 쓴다.
_ACTION_UPDATE_THEN_SUPPORT = (
    "StockLens 카드의 [업데이트]를 확인해 주세요. "
    "업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
)
_ACTION_SUPPORT = "상단 [지원 문의]를 눌러주세요."
_ACTION_LICENSE_MISSING = "StockLens 카드의 [활성화]를 눌러 메일로 받은 키를 넣어주세요."
_ACTION_LICENSE_INVALID = "StockLens 카드의 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요."
_ACTION_MCP_REGISTER = "StockLens 카드의 [MCP 등록]을 눌러주세요."

# 검사 실패(status="fail") 시 붙는 오류 코드 카탈로그.
# 각 항목은 summary(무엇이 문제인지)/impact(무엇이 안 되는지)/action(어떻게 고치는지)/
# repairable(사용자가 직접 고칠 수 있는지)을 고정 필드로 갖는다.
ERROR_CATALOG: dict[str, dict] = {
    "DEPENDENCY_BROKEN": {
        "summary": "StockLens 설치가 온전하지 않아요.",
        "impact": "AI 앱에서 StockLens가 실행되지 않아요.",
        "action": _ACTION_UPDATE_THEN_SUPPORT,
        "repairable": True,
    },
    "UNSUPPORTED_PYTHON": {
        "summary": f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요해요.",
        "impact": "일부 기능이 동작하지 않을 수 있어요.",
        "action": _ACTION_SUPPORT,
        "repairable": True,
    },
    "MCP_CONFIG_MISSING": {
        "summary": "StockLens가 아직 AI 앱에 등록되지 않았어요.",
        "impact": "AI 앱에서 StockLens 도구가 보이지 않아요.",
        "action": _ACTION_MCP_REGISTER,
        "repairable": True,
    },
    "STOCKLENS_LICENSE_MISSING": {
        "summary": "라이선스 키가 아직 없어요.",
        "impact": "모든 도구가 잠겨 있어요.",
        "action": _ACTION_LICENSE_MISSING,
        "repairable": True,
    },
    "STOCKLENS_LICENSE_INVALID": {
        "summary": "저장된 라이선스 키를 확인할 수 없어요.",
        "impact": "모든 도구가 잠겨 있어요.",
        "action": _ACTION_LICENSE_INVALID,
        "repairable": True,
    },
    "CACHE_NOT_WRITABLE": {
        "summary": "캐시 폴더에 쓸 수 없어요.",
        "impact": "엑셀 저장과 사용 기록 저장이 안 돼요.",
        "action": _ACTION_SUPPORT,
        "repairable": True,
    },
    "KR_DATA_UNREACHABLE": {
        "summary": "국내 데이터를 가져오지 못했어요.",
        "impact": "국내 주식 도구가 동작하지 않을 수 있어요.",
        "action": _ACTION_UPDATE_THEN_SUPPORT,
        "repairable": False,
    },
    "US_DATA_UNREACHABLE": {
        "summary": "미국 시세를 가져오지 못했어요.",
        "impact": "미국 주식 도구가 동작하지 않을 수 있어요.",
        "action": _ACTION_UPDATE_THEN_SUPPORT,
        "repairable": False,
    },
    "UPDATE_CHECK_FAILED": {
        "summary": "최신 버전 정보를 가져오지 못했어요.",
        "impact": "업데이트 알림만 늦어지고, 다른 기능에는 영향이 없어요.",
        "action": "그대로 쓰셔도 돼요. 잠시 뒤 [진단]을 다시 눌러보세요.",
        "repairable": False,
    },
}

# 최근 조회 실패는 원인 분류마다 코드가 따로 있다(예: RECENT_TOOL_FAILURES_TLS). 할 일은
# 분류별 공통 문장(_error_class)을 쓴다. cancelled 는 실패로 세지 않으므로 코드가 없다.
for _category in ("tls", "dns", "timeout", "blocked", "auth", "connect", "schema", "other"):
    ERROR_CATALOG[f"RECENT_TOOL_FAILURES_{_category.upper()}"] = {
        "summary": "최근 조회 중 아직 실패로 남아 있는 것이 있어요.",
        "impact": "AI 앱에서 일부 조회가 실패하고 있어요.",
        "action": action_for(_category, _LENS_NAME),
        "repairable": False,
    }
del _category


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
    # "active" | "missing" | "invalid" | "expired" | "revoked" | "clock"
    # 뒤의 셋은 키 자체는 진짜인데 지금 못 쓰는 상태다 — 예전엔 이걸 전부 "active"로
    # 보고해서, 도구는 잠겼는데 Manager는 "라이선스 활성"이라고 말했다.
    status: str
    license_id_masked: str | None = None
    # 기간이 있는 키(체험판·구독)만 채워진다. Manager가 "N일 남음"을 보여주는 근거.
    expires_on: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "license_id_masked": self.license_id_masked,
            "expires_on": self.expires_on,
        }


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
    # 증권사 연결 확장 (additive). 구 Manager 는 이 키를 몰라도 되고,
    # 새 Manager 는 capabilities 로 broker UI 지원 여부를 협상한다.
    # 공통 schema_version 은 올리지 않는다.
    capabilities: dict | None = None
    provider_connections: dict | None = None

    def to_dict(self) -> dict:
        doc = {
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
        if self.capabilities is not None:
            doc["capabilities"] = self.capabilities
        if self.provider_connections is not None:
            doc["provider_connections"] = self.provider_connections
        return doc

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
            summary="StockLens 패키지를 불러올 수 있어요.",
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
            summary=ERROR_CATALOG["DEPENDENCY_BROKEN"]["summary"],
            detail=[f"패키지를 불러오지 못했어요 ({_short(e)})", f"인터프리터: {sys.executable}"],
            error_code="DEPENDENCY_BROKEN",
            fix=_ACTION_UPDATE_THEN_SUPPORT,
        )


def _check_command_available() -> DiagnosticCheck:
    exe = shutil.which("stocklens")
    if exe:
        return DiagnosticCheck(
            id="COMMAND_AVAILABLE",
            status="ok",
            critical=True,
            summary="StockLens 실행 파일을 찾았어요.",
            detail=[exe],
        )

    from stock_mcp_server.setup_claude import _uv_tool_bin_dirs

    for bin_dir in _uv_tool_bin_dirs():
        for name in ("stocklens.exe", "stocklens"):
            candidate = bin_dir / name
            if candidate.exists():
                # PATH에 없는 것 자체는 문제가 아니다 — MCP 등록이 절대경로로 이뤄지므로
                # 그대로 동작한다. 예전엔 warn(critical)이라 멀쩡한 설치에도 카드가 계속
                # "주의"로 남고, 정작 안내문에는 "무시 가능"이라고 적혀 있었다.
                # 고칠 것도 없는데 고치라고 하면 진짜 경고까지 같이 안 믿게 된다.
                return DiagnosticCheck(
                    id="COMMAND_AVAILABLE",
                    status="ok",
                    critical=True,
                    summary="StockLens 실행 파일을 찾았어요.",
                    detail=[
                        str(candidate),
                        "PATH에는 없지만 MCP 등록은 이 절대경로로 하므로 그대로 쓰시면 됩니다.",
                    ],
                )

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in ("stocklens.exe", "stocklens"):
            candidate = scripts_dir / name
            if candidate.exists():
                # MCP 등록은 이 절대경로를 그대로 적으므로(setup_claude.resolve_server_entry
                # 4단계) PATH 를 고치라고 할 필요가 없다. 할 일은 등록 한 번이다.
                return DiagnosticCheck(
                    id="COMMAND_AVAILABLE",
                    status="warn",
                    critical=True,
                    summary="StockLens 실행 파일이 기본 위치가 아닌 곳에 있어요.",
                    detail=[str(candidate), f"PATH에 없음: {scripts_dir}"],
                    fix=_ACTION_MCP_REGISTER,
                )
    except Exception:
        pass

    return DiagnosticCheck(
        id="COMMAND_AVAILABLE",
        status="fail",
        critical=True,
        summary="StockLens 실행 파일을 찾지 못했어요.",
        error_code="DEPENDENCY_BROKEN",
        fix=_ACTION_UPDATE_THEN_SUPPORT,
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
        summary=f"Python {ver}는 지원하지 않는 버전이에요.",
        detail=[f"필요한 버전: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상", sys.executable],
        error_code="UNSUPPORTED_PYTHON",
        fix=_ACTION_SUPPORT,
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
        summary=ERROR_CATALOG["MCP_CONFIG_MISSING"]["summary"],
        error_code="MCP_CONFIG_MISSING",
        fix=_ACTION_MCP_REGISTER,
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
    # 키에 박힌 날짜가 아니라 실제로 끝나는 날. 이 값이 매니저의 "N일 남음" 배지로
    # 그대로 나가므로, 서명 날짜를 쓰면 "12월까지"라고 안내해놓고 9월에 잠긴다.
    expiry = licensing.effective_expiry(res)
    # 키는 진짜지만 지금 못 쓰는 경우(기간 종료·폐기·시계 되돌림)를 그대로 드러낸다.
    reason = licensing.license_block_reason()
    return LicenseSummary(
        status=reason if reason in ("expired", "revoked", "clock") else "active",
        license_id_masked=masked,
        expires_on=expiry.isoformat() if expiry else None,
    )


# 셋 다 "키를 다시 넣으세요"가 답이 아니다 — 할 일이 서로 다르다. 같은 문구를 쓰면
# 기간이 끝난 사람이 키를 재입력하며 시간을 버린다.
_LICENSE_BLOCKED_SUMMARY = {
    "expired": "사용 기간이 끝났어요.",
    "revoked": "이 라이선스 키는 사용이 중지돼 있어요.",
    "clock": "이 컴퓨터의 날짜가 실제보다 과거로 되어 있어요.",
}
_LICENSE_BLOCKED_FIX = {
    "expired": "StockLens 카드의 [구매]를 누르고, 받은 키를 [활성화]로 넣어주세요.",
    "revoked": "착오라면 상단 [지원 문의]를 눌러주세요.",
    "clock": "날짜와 시간을 오늘로 맞춘 뒤 [진단]을 다시 눌러주세요.",
}


def _check_license_active(summary: LicenseSummary) -> DiagnosticCheck:
    if summary.status == "missing":
        return DiagnosticCheck(
            id="LICENSE_ACTIVE",
            status="fail",
            critical=True,
            summary=ERROR_CATALOG["STOCKLENS_LICENSE_MISSING"]["summary"],
            error_code="STOCKLENS_LICENSE_MISSING",
            fix=_ACTION_LICENSE_MISSING,
        )
    if summary.status == "invalid":
        return DiagnosticCheck(
            id="LICENSE_ACTIVE",
            status="fail",
            critical=True,
            summary=ERROR_CATALOG["STOCKLENS_LICENSE_INVALID"]["summary"],
            error_code="STOCKLENS_LICENSE_INVALID",
            fix=_ACTION_LICENSE_INVALID,
        )
    if summary.status in ("expired", "revoked", "clock"):
        return DiagnosticCheck(
            id="LICENSE_ACTIVE",
            status="fail",
            critical=True,
            summary=_LICENSE_BLOCKED_SUMMARY[summary.status],
            error_code="STOCKLENS_LICENSE_INVALID",
            fix=_LICENSE_BLOCKED_FIX[summary.status],
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
            summary=ERROR_CATALOG["CACHE_NOT_WRITABLE"]["summary"],
            detail=[f"폴더에 쓰지 못했어요 ({type(e).__name__})"],
            error_code="CACHE_NOT_WRITABLE",
            fix=_ACTION_SUPPORT,
        )


def _check_recent_tool_failures(records: list[dict] | None = None) -> DiagnosticCheck:
    """최근 이틀 동안 AI 앱이 부른 도구 중, 아직 실패로 끝나 있는 것이 있는가.

    온라인 확인은 대표 종목 몇 건만 본다. 고객이 실제로 막힌 조회는 그와 다를 수 있어서
    (2026-09-11 네이버 개편 때 순위·수급만 따로 죽었다) 도구마다 남는 metrics 기록을
    그대로 읽는다. 네트워크를 타지 않으므로 기본(오프라인) 진단에서도 돈다.

    - 같은 도구가 실패 뒤에 성공했으면 해결된 것으로 본다(일시 장애가 카드를 계속 붉게
      두면 진짜 경고까지 안 믿게 된다).
    - AI 앱이 취소한 호출(CancelledError)은 실패로 세지 않는다.
    - 한계: 도구가 예외 없이 "⚠️ …" 문자열을 돌려준 실패는 metrics 에 에러로 안 남아서 못 본다.
    """
    if records is None:
        from stock_mcp_server._metrics import load_metrics

        records = load_metrics(days=_RECENT_LOOKBACK_DAYS)

    calls = [r for r in records if isinstance(r, dict) and r.get("tool")]
    if not calls:
        return DiagnosticCheck(
            id="RECENT_TOOL_FAILURES",
            status="ok",
            critical=False,
            summary="최근 이틀 동안 AI 앱이 StockLens를 쓴 기록이 없어요.",
        )
    calls.sort(key=lambda r: str(r.get("timestamp") or ""))

    by_tool: dict[str, dict] = {}
    failed = 0
    for r in calls:
        slot = by_tool.setdefault(str(r["tool"]), {"failures": [], "cancelled": [], "last": None})
        error_type = r.get("error")
        if not error_type:
            slot["last"] = "ok"
            continue
        category = classify_error(str(error_type), r.get("error_detail"))
        if category == "cancelled":
            # 취소는 성공도 실패도 아니다 — 그 도구의 마지막 결과를 바꾸지 않는다.
            slot["cancelled"].append(r)
            continue
        slot["failures"].append((r, category))
        slot["last"] = "fail"
        failed += 1

    total = len(calls)
    unresolved = [tool for tool, slot in by_tool.items() if slot["last"] == "fail"]
    detail = _recent_failure_lines(by_tool, unresolved)

    if not unresolved:
        summary = (
            f"최근 이틀 동안 조회 {total}번 중 {failed}번이 실패했지만, 그 뒤에는 정상이었어요."
            if failed
            else f"최근 이틀 동안 조회 {total}번이 모두 정상이었어요."
        )
        return DiagnosticCheck(
            id="RECENT_TOOL_FAILURES", status="ok", critical=False, summary=summary, detail=detail,
        )

    # 대표 분류: 아직 실패로 남은 도구들의 마지막 실패 분류 중 가장 많은 것. 같으면 더 최근 것.
    last_failures = sorted(
        (by_tool[tool]["failures"][-1] for tool in unresolved),
        key=lambda pair: str(pair[0].get("timestamp") or ""),
        reverse=True,
    )
    counts: dict[str, int] = {}
    for _record, category in last_failures:
        counts[category] = counts.get(category, 0) + 1
    top = max(counts.values())
    category = next(c for _r, c in last_failures if counts[c] == top)

    return DiagnosticCheck(
        id="RECENT_TOOL_FAILURES",
        status="warn",
        critical=False,
        summary=f"최근 조회 중 아직 실패로 남아 있는 것이 {len(unresolved)}가지 있어요.",
        detail=detail,
        error_code=f"RECENT_TOOL_FAILURES_{category.upper()}",
        fix=action_for(category, _LENS_NAME),
    )


def _hhmm(timestamp: object) -> str:
    text = str(timestamp or "")
    return text[11:16] if len(text) >= 16 else "?"


def _recent_failure_lines(by_tool: dict[str, dict], unresolved: list[str]) -> list[str]:
    """도구별 한 줄(지원용). 아직 실패로 남은 도구가 먼저, 그다음 최근 실패 순."""
    failing = [tool for tool, slot in by_tool.items() if slot["failures"]]
    failing.sort(key=lambda tool: str(by_tool[tool]["failures"][-1][0].get("timestamp") or ""), reverse=True)
    failing.sort(key=lambda tool: tool not in unresolved)  # 안정 정렬 — 최근 순서는 유지된다
    lines: list[str] = []
    for tool in failing:
        failures = by_tool[tool]["failures"]
        record, category = failures[-1]
        detail = _redact(record.get("error_detail"))[:_RECENT_DETAIL_CHARS]
        raw = f"{record.get('error')}: {detail}" if detail else str(record.get("error"))
        lines.append(
            f"{tool}: 실패 {len(failures)}번, 마지막 {_hhmm(record.get('timestamp'))}, 분류 {category}, {raw}"
        )
    for tool, slot in by_tool.items():
        if slot["cancelled"] and not slot["failures"]:
            record = slot["cancelled"][-1]
            lines.append(
                f"{tool}: 취소 {len(slot['cancelled'])}번(실패로 세지 않음), "
                f"마지막 {_hhmm(record.get('timestamp'))}, 분류 cancelled, {record.get('error')}"
            )
    return lines[:_RECENT_DETAIL_MAX_LINES]



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
        # 예외 이름은 summary 가 아니라 details 에 둔다 — 화면에는 할 일만 보인다.
        return DiagnosticCheck(
            id=fallback_id,
            status="fail",
            critical=fallback_critical,
            summary="이 항목을 확인하다가 문제가 생겼어요.",
            detail=[f"확인 중 예상치 못한 오류가 났어요 ({_short(e)})"],
            fix="[진단]을 다시 눌러주세요. 그래도 같으면 상단 [지원 문의]를 눌러주세요.",
        )


def _skip(check_id: str) -> DiagnosticCheck:
    return DiagnosticCheck(
        id=check_id,
        status="skip",
        critical=False,
        summary="이번 진단에서는 인터넷 연결 확인을 건너뛰었어요.",
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
                summary=ERROR_CATALOG["UPDATE_CHECK_FAILED"]["summary"],
                detail=["최신 버전 정보 조회 실패"],
                error_code="UPDATE_CHECK_FAILED",
                fix=ERROR_CATALOG["UPDATE_CHECK_FAILED"]["action"],
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


def _broker_keyring():
    """keyring 접근 지점. 테스트가 가짜 keyring 으로 대체한다."""
    import keyring
    return keyring


_BROKER_CAPABILITIES = {
    "broker_connection_contract": 1,
    "market_data_router_contract": 1,
    # 1.0 멀티 증권사 (additive)
    "broker_provider_registry_contract": 1,
    "multi_broker_primary_contract": 1,
    "credential_schema_contract": 1,
}


def _broker_summary() -> tuple[dict, dict]:
    """(capabilities, provider_connections). 비밀값 없는 allowlist DTO.

    상태 dict 를 복사하지 않는다 - 필드를 하나씩 옮겨 담아 낯선 필드,
    credential_ref, 비밀처럼 보이는 값이 doctor 출력에 닿지 못하게 한다.
    상태 파일에 기록이 없는 공급자는 keychain 을 읽지 않는다.
    """
    capabilities = dict(_BROKER_CAPABILITIES)

    from stock_mcp_server.market_data.provider_registry import registry

    connections: dict = {}
    try:
        from stock_mcp_server.market_data.connection_state import (
            load_state_v2,
        )
        state = load_state_v2()
    except Exception:  # noqa: BLE001
        state = None

    from stock_mcp_server.market_data.provider_registry import (
        is_release_verified,
    )

    for pid in registry.ids():
        descriptor = registry.require(pid)
        connection: dict = {
            "status": "not_configured",
            "primary": False,
            "lifecycle": None,
            "active_profile": None,
            "data_source_mode": "legacy",
            "profiles": {
                p: {"configured": False, "verified": False,
                    "verified_at": None, "capabilities": {}}
                for p in descriptor.supported_profiles
            },
            # 연결 시험(available)과 별개인 출시 검증 상태 (코드 고정 표).
            "release_verified": {
                "kr_intraday": is_release_verified(pid, "kr_intraday"),
                "us_intraday": is_release_verified(pid, "us_intraday"),
            },
            "storage": "os-keychain",
        }
        connections[pid] = connection
        if state is None:
            continue
        connection["data_source_mode"] = state["data_source_mode"]
        connection["primary"] = state["primary_provider"] == pid
        record = state["providers"].get(pid)
        if record is None:
            continue
        connection["lifecycle"] = record["lifecycle"]
        connection["active_profile"] = record["active_profile"]

        keyring = _broker_keyring()
        service = f"stocklens-broker-{pid}"
        unknown = False
        for name in descriptor.supported_profiles:
            prec = record["profiles"].get(name)
            usernames = []
            ref = (prec or {}).get("credential_ref")
            if ref and ref != "legacy":
                usernames.append(f"{pid}:{name}:{ref}")
            usernames.append(f"{pid}:{name}")
            configured = False
            try:
                for username in dict.fromkeys(usernames):
                    if keyring.get_password(service, username):
                        configured = True
                        break
            except Exception:  # noqa: BLE001
                unknown = True
                break
            connection["profiles"][name] = {
                "configured": configured,
                "verified": bool(
                    prec and prec.get("verified") and configured),
                "verified_at": (prec or {}).get("verified_at"),
                "capabilities": dict((prec or {}).get(
                    "capabilities") or {}),
            }
        if unknown:
            # keychain 을 못 읽으면 "미설정"으로 단정하지 않는다.
            connection["status"] = "unknown"
            continue
        if record["lifecycle"] != "connected":
            connection["status"] = "disabled"
            continue
        active = record["active_profile"]
        if active and connection["profiles"].get(
                active, {}).get("configured"):
            connection["status"] = "connected"
        elif any(v["configured"]
                 for v in connection["profiles"].values()):
            connection["status"] = "configured"
    return capabilities, connections


async def run_diagnostics_async(*, online: bool = False) -> DiagnosticReport:
    """`run_diagnostics`의 async 코어. 이미 실행 중인 이벤트 루프(예: MCP 도구)
    안에서는 이 쪽을 직접 await 한다 — `run_diagnostics()`는 내부에서
    `asyncio.run()`을 쓰므로 루프 안에서 부르면 RuntimeError가 난다."""
    # 서버와 같은 TLS 기준으로 본다. 이게 없으면 진단은 통과하는데 서버는 실패하는,
    # 가장 나쁜 종류의 어긋남이 생긴다 — 2026-08-13 문의에서 실제로 그랬다.
    from stock_mcp_server import _tls

    _tls.apply()

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
        _safe_check(
            _check_recent_tool_failures, fallback_id="RECENT_TOOL_FAILURES", fallback_critical=False
        ),
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

    try:
        broker_capabilities, provider_connections = _broker_summary()
    except Exception:  # noqa: BLE001
        broker_capabilities, provider_connections = (
            dict(_BROKER_CAPABILITIES), None)

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
        capabilities=broker_capabilities,
        provider_connections=provider_connections,
    )


def run_diagnostics(*, online: bool = False) -> DiagnosticReport:
    """동기 진입점(CLI용). 출력 부작용 없음 — 결과를 DiagnosticReport로 반환만 한다.

    online=False(기본)면 네트워크 호출이 전혀 없어 수 초 내 끝난다.
    이미 이벤트 루프 안이라면 `run_diagnostics_async`를 직접 await할 것.
    """
    return asyncio.run(run_diagnostics_async(online=online))
