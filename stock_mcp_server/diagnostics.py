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
    UPDATE_CHECK_REACHABLE, RECENT_TOOL_FAILURES, BROKER_DATA_REACHABLE

RECENT_TOOL_FAILURES 는 세 Lens 공통 검사다(DartLens·TelegramLens 도 같은 ID·같은 판정).
metrics 기록만 읽으므로 기본 모드에서도 실행된다.

기본(online=False) 진단은 네트워크 호출이 전혀 없어 수 초 내 끝난다.
online=True 일 때만 KR_DATA_REACHABLE/US_DATA_REACHABLE/UPDATE_CHECK_REACHABLE가
실행되며, 국내는 데이터 종류별 대표 조회 1건씩(현재가·일봉·투자자 수급·시가총액 순위·
테마 목록·재무), 미국은 대표 종목 1개, 업데이트는 PyPI/GitHub만 조회한다(전수 조사 아님).
BROKER_DATA_REACHABLE 도 online=True 일 때만, 그리고 연결한 증권사가 있을 때만 checks 에
들어간다(연결이 없으면 항목 자체가 없다). 증권사마다 읽기 전용 분봉 1건만 조회한다.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_mcp_server._error_class import action_for, classify_error, classify_exception, still_failing

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
# - 터미널 명령·환경변수 이름·예외 이름을 쓰지 않는다.
# - details.lines 도 Manager 상세 창에 보인다(명령어·경로 모양 줄만 걸러지고,
#   RECENT_TOOL_FAILURES·BROKER_DATA_REACHABLE 줄은 숨겨진다). 줄 앞은 한국어로 쓰고
#   예외 원문은 줄 끝 괄호 안에 짧게만 붙인다(_short).
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

ERROR_CATALOG["BROKER_DATA_UNREACHABLE"] = {
    "summary": "연결한 증권사의 시세를 가져오지 못했어요.",
    "impact": "증권사로 받는 분봉·상세 수급이 안 될 수 있어요. 기본 시세는 그대로 돼요.",
    "action": "StockLens 카드의 [증권사 연결]에서 연결을 다시 확인해 주세요.",
    "repairable": False,
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
    - 끝에 남은 실패가 아직 실패인지는 `_error_class.still_failing`이 정한다(세 Lens 공통).
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
        slot = by_tool.setdefault(str(r["tool"]), {"failures": [], "cancelled": [], "trailing": []})
        error_type = r.get("error")
        if not error_type:
            slot["trailing"] = []
            continue
        category = classify_error(str(error_type), r.get("error_detail"))
        if category == "cancelled":
            # 취소는 성공도 실패도 아니다 — 그 도구의 마지막 결과를 바꾸지 않는다.
            slot["cancelled"].append(r)
            continue
        slot["failures"].append((r, category))
        slot["trailing"].append(category)
        failed += 1

    total = len(calls)
    unresolved = [tool for tool, slot in by_tool.items() if still_failing(slot["trailing"])]
    detail = _recent_failure_lines(by_tool, unresolved)

    if not unresolved:
        summary = (
            f"최근 이틀 동안 조회 {total}번 중 {failed}번이 실패했지만, 계속 실패하고 있지는 않아요."
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


# details 줄에 쓰는 분류 이름. 이 줄들은 Manager 상세 창에 그대로 보이므로 영문
# category 대신 고객이 읽을 말로 쓴다(category 원문은 RECENT_TOOL_FAILURES 줄과 error_code 에 있다).
_CATEGORY_LABEL = {
    "tls": "보안 프로그램이 연결을 가로챈 것 같음",
    "dns": "인터넷 주소를 찾지 못함",
    "timeout": "연결 시간 초과",
    "blocked": "요청이 막힘",
    "auth": "인증 실패",
    "connect": "서버에 연결하지 못함",
    "schema": "응답 모양이 달라짐",
    "cancelled": "중간에 취소됨",
    "other": "원인 확인 필요",
}


_classify_exception = classify_exception  # 세 Lens 공통 규칙(_error_class)


def _failure_line(name: str, category: str, raw: str | None) -> str:
    label = _CATEGORY_LABEL.get(category, _CATEGORY_LABEL["other"])
    return f"{name}: 실패 ({label}, {raw})" if raw else f"{name}: 실패 ({label})"


def _dominant_category(failures: list[dict]) -> str:
    """가장 많이 나온 분류. 같으면 목록에서 먼저 나온 것(국내는 현재가가 맨 앞)."""
    counts: dict[str, int] = {}
    for item in failures:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    top = max(counts.values())
    return next(item["category"] for item in failures if counts[item["category"]] == top)


# --- 온라인 검사 (online=True 일 때만) ---


def _judge_price(data) -> tuple[bool, str]:
    # 휴장일에도 최근 종가는 내려오므로 장 상태는 실패 조건에 넣지 않는다.
    name = (data or {}).get("name")
    price = (data or {}).get("price")
    if name and isinstance(price, (int, float)) and price > 0:
        return True, f"{name} {price:,}원"
    return False, "현재가 칸이 비어 있음"


def _judge_rows(data) -> tuple[bool, str]:
    if isinstance(data, list) and data:
        return True, ""
    return False, "빈 응답"


def _judge_financials(data) -> tuple[bool, str]:
    from stock_mcp_server.naver import PARSE_MISS_KEY

    if isinstance(data, dict) and data and PARSE_MISS_KEY not in data:
        return True, ""
    return False, "재무 표를 읽지 못함"


def _kr_probes() -> tuple:
    """국내 데이터 종류별 대표 조회 1건씩.

    2026-09-11 네이버 구 페이지 폐지 때 데이터 종류마다 따로 죽었다 — 현재가만 보면
    수급·순위·재무가 죽은 걸 모른다. 순위는 시가총액 순위를 쓴다. 등락률·거래량·거래대금
    순위는 장 시작 전에 빈 배열이 정상이라 이른 아침 진단이 거짓 경보를 낸다.
    """
    from stock_mcp_server import naver

    return (
        ("현재가", lambda: naver.get_current_price("005930"), _judge_price),
        ("일봉", lambda: naver.get_ohlcv("005930", "day", 5), _judge_rows),
        ("투자자 수급", lambda: naver.get_investor_flow("005930", 5), _judge_rows),
        ("시가총액 순위", lambda: naver.get_market_cap_ranking("KOSPI", 5), _judge_rows),
        ("테마 목록", lambda: naver.list_themes(1), _judge_rows),
        ("재무", lambda: naver.get_financials("005930"), _judge_financials),
    )


async def _run_probe(name: str, factory, judge) -> dict:
    try:
        data = await asyncio.wait_for(factory(), timeout=_ONLINE_CHECK_TIMEOUT)
    except Exception as e:  # asyncio.TimeoutError 포함
        return {"name": name, "ok": False, "category": _classify_exception(e), "raw": _short(e)}
    try:
        ok, note = judge(data)
    except Exception as e:
        return {"name": name, "ok": False, "category": "schema", "raw": _short(e)}
    if not ok:
        return {"name": name, "ok": False, "category": "schema", "raw": note}
    return {"name": name, "ok": True, "note": note}


async def _check_kr_data_reachable(probes: tuple | None = None) -> DiagnosticCheck:
    results = await asyncio.gather(*(_run_probe(*probe) for probe in (probes or _kr_probes())))
    total = len(results)
    failures = [r for r in results if not r["ok"]]
    lines = [
        (f"{r['name']}: 정상 ({r['note']})" if r["note"] else f"{r['name']}: 정상")
        if r["ok"]
        else _failure_line(r["name"], r["category"], r["raw"])
        for r in results
    ]
    lines.append(f"확인 시각: {datetime.now().strftime('%H:%M')}")

    if not failures:
        return DiagnosticCheck(
            id="KR_DATA_REACHABLE",
            status="ok",
            critical=False,
            summary=f"국내 데이터 {total}가지가 모두 정상이에요.",
            detail=lines,
        )

    # 현재가는 거의 모든 국내 도구의 바탕이라 그것만 실패해도 fail 이다.
    price_failed = any(r["name"] == "현재가" for r in failures)
    if len(failures) == total:
        summary = f"국내 데이터 {total}가지를 모두 가져오지 못했어요."
    else:
        names = ", ".join(r["name"] for r in failures)
        summary = f"국내 데이터 {total}가지 중 {total - len(failures)}가지는 정상이고, {names} 조회가 실패했어요."
    category = _dominant_category(failures)
    return DiagnosticCheck(
        id="KR_DATA_REACHABLE",
        status="fail" if price_failed or len(failures) == total else "warn",
        critical=False,
        summary=summary,
        detail=lines,
        # Manager 는 error_code 로 분기하지 않지만(2026-09-17 확인) 기존 코드를 유지한다.
        # 분류는 action 과 details 줄에 드러난다.
        error_code="KR_DATA_UNREACHABLE",
        fix=action_for(category, _LENS_NAME),
    )


async def _check_us_data_reachable() -> DiagnosticCheck:
    from stock_mcp_server import yfinance_source as us

    def _fail(category: str, raw: str) -> DiagnosticCheck:
        return DiagnosticCheck(
            id="US_DATA_REACHABLE",
            status="fail",
            critical=False,
            summary=ERROR_CATALOG["US_DATA_UNREACHABLE"]["summary"],
            detail=[_failure_line("미국 시세(AAPL)", category, raw)],
            error_code="US_DATA_UNREACHABLE",
            fix=action_for(category, _LENS_NAME),
        )

    try:
        data = await asyncio.wait_for(us.get_price("AAPL"), timeout=_ONLINE_CHECK_TIMEOUT)
    except Exception as e:
        return _fail(_classify_exception(e), _short(e))

    if data is None:
        # yfinance 쪽이 오류를 삼키고 None 을 준다 — 원인은 여기서 알 수 없다.
        return _fail("other", "응답 없음")
    name = data.get("name")
    price = data.get("price")
    if name and isinstance(price, (int, float)) and price > 0:
        return DiagnosticCheck(
            id="US_DATA_REACHABLE",
            status="ok",
            critical=False,
            summary="미국 시세를 정상으로 가져왔어요.",
            detail=[f"미국 시세: 정상 ({name} {price:,}달러)"],
        )
    return _fail("schema", "가격 칸이 비어 있음")


# --- 증권사 시세 (online=True, 연결한 증권사가 있을 때만) ---

_BROKER_CHECK_TIMEOUT = 10.0  # 초. 토큰을 새로 받아야 하는 첫 호출까지 감안한다.
_BROKER_PROBE = {"KR": ("005930", "KRX", "국내"), "US": ("AAPL", "NAS", "미국")}
_EXPERIMENTAL_BROKERS = frozenset({"toss"})  # server._EXPERIMENTAL_BROKERS 와 같은 기준


def _latest_opened_trading_day(market: str, now: datetime):
    """가장 최근에 정규장이 열린 거래일. 오늘 장이 아직 안 열렸으면 그 전 거래일."""
    from zoneinfo import ZoneInfo

    from stock_mcp_server.market_data.sessions import session_window

    tz = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
    today = now.astimezone(tz).date()
    for back in range(0, 15):
        day = today - timedelta(days=back)
        window = session_window(market, day)
        if window is None or (back == 0 and now < window.open_at):
            continue
        return day
    return today


def _safe_broker_raw(exc: BaseException) -> str:
    """증권사 오류의 원문 대신 쓰는 허용 목록 요약. 메시지 본문은 싣지 않는다.

    증권사 경로의 예외 메시지에는 원칙상 비밀이 없지만, 진단 결과는 화면·지원 번들로
    나간다. `_broker_summary` 와 같은 원칙으로 이름·상태 코드만 옮긴다.
    """
    parts = [type(exc).__name__]
    status = getattr(exc, "provider_status", None)
    if isinstance(status, str) and status.isidentifier():
        parts.append(status)
    http = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    if isinstance(http, int):
        parts.append(f"HTTP {http}")
    root = exc.__cause__ or exc.__context__
    if root is not None and root is not exc:
        parts.append(f"원인 {type(root).__name__}")
    return " ".join(parts)[:_RAW_MAX]


async def _probe_broker(runtime, snapshot, provider: str, display_name: str, market: str, now: datetime) -> dict:
    from stock_mcp_server.market_data.models import BarRequest

    symbol, venue, market_label = _BROKER_PROBE[market]
    name = f"{display_name} {market_label} 시세"
    try:
        # 도구 호출과 같은 경로: 연결 상태 스냅샷 → 저장된 키 → 캐시된 토큰(token_store).
        adapter = runtime.providers_for(market, provider, snapshot=snapshot).get(provider)
        if adapter is None:
            # 연결 기록은 있는데 저장된 키를 못 꺼냈다. 다시 연결하는 게 할 일이다.
            return {"name": name, "ok": False, "category": "auth", "raw": "저장된 연결 정보를 읽지 못함"}
        day = _latest_opened_trading_day(market, now)
        request = BarRequest(
            symbol=symbol, market=market, interval="1m", start=None, end=None,
            trading_date=day, row_limit=1, venue=venue, session="regular",
            adjustment="unadjusted", completed_only=False, source=provider,
        )
        dataset = await asyncio.wait_for(adapter.fetch_bars(request), timeout=_BROKER_CHECK_TIMEOUT)
    except Exception as e:
        return {"name": name, "ok": False, "category": _classify_exception(e), "raw": _safe_broker_raw(e)}
    return {"name": name, "ok": True, "note": f"{day.isoformat()} 분봉 {len(dataset.bars)}개"}


async def _check_broker_data_reachable(runtime=None, *, now: datetime | None = None) -> DiagnosticCheck | None:
    """연결한 증권사마다 읽기 전용 시세(분봉) 1건. 연결이 하나도 없으면 None — 검사 항목을 안 넣는다.

    주문·계좌 API 는 부르지 않는다. 확인 대상은 연결 시험·출시 검증을 통과해 도구가
    실제로 쓰는 시장(kr_intraday/us_intraday)뿐이다.
    """
    import os

    from stock_mcp_server.market_data.provider_registry import registry

    if runtime is None:
        from stock_mcp_server.market_data.runtime import ProviderRuntime

        runtime = ProviderRuntime()
    snapshot = runtime.snapshot()
    experimental = os.environ.get("LEETKIT_ENABLE_EXPERIMENTAL_BROKERS") == "1"

    targets: list[tuple[str, str, str]] = []
    providers: list[str] = []
    for provider in registry.ids():
        if provider in _EXPERIMENTAL_BROKERS and not experimental:
            continue
        caps = snapshot.capabilities(provider)
        if not caps.get("connected"):
            continue
        markets = [m for m in ("KR", "US") if caps.get(f"{m.lower()}_intraday")]
        if not markets:
            continue
        providers.append(provider)
        display = registry.require(provider).display_name
        targets.extend((provider, display, m) for m in markets)
    if not targets:
        return None

    now = now or datetime.now(timezone.utc)
    results = await asyncio.gather(
        *(_probe_broker(runtime, snapshot, pid, display, market, now) for pid, display, market in targets)
    )
    failures = [r for r in results if not r["ok"]]
    lines = [
        f"{r['name']}: 정상 ({r['note']})" if r["ok"] else _failure_line(r["name"], r["category"], r["raw"])
        for r in results
    ]
    count = len(providers)
    if not failures:
        return DiagnosticCheck(
            id="BROKER_DATA_REACHABLE",
            status="ok",
            critical=False,
            summary=f"연결한 증권사 {count}곳의 시세 조회가 모두 정상이에요.",
            detail=lines,
        )

    if len(failures) == len(results):
        summary = f"연결한 증권사 {count}곳의 시세를 모두 가져오지 못했어요."
    else:
        summary = f"연결한 증권사 {count}곳 중 {', '.join(r['name'] for r in failures)}가 실패했어요."
    # 인증 실패가 하나라도 있으면 그게 먼저다 — 다시 연결하지 않으면 재시도가 소용없다.
    category = "auth" if any(r["category"] == "auth" for r in failures) else _dominant_category(failures)
    return DiagnosticCheck(
        id="BROKER_DATA_REACHABLE",
        status="fail" if len(failures) == len(results) else "warn",
        # 증권사가 안 돼도 기본 시세(네이버·야후)는 그대로라 카드를 "사용 불가"로 만들지 않는다.
        critical=False,
        summary=summary,
        detail=lines,
        error_code="BROKER_DATA_UNREACHABLE",
        fix=action_for(category, _LENS_NAME),
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


async def _safe_broker_check() -> DiagnosticCheck | None:
    """증권사 확인이 예상 못한 예외로 죽어도 리포트 전체는 살린다(_safe_check 의 async 판)."""
    try:
        return await _check_broker_data_reachable()
    except Exception as e:  # noqa: BLE001
        return DiagnosticCheck(
            id="BROKER_DATA_REACHABLE",
            status="fail",
            critical=False,
            summary="이 항목을 확인하다가 문제가 생겼어요.",
            detail=[f"확인 중 예상치 못한 오류가 났어요 ({type(e).__name__})"],
            fix="[진단]을 다시 눌러주세요. 그래도 같으면 상단 [지원 문의]를 눌러주세요.",
        )


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
        kr_check, us_check, (update_check, latest_version), broker_check = await asyncio.gather(
            _check_kr_data_reachable(),
            _check_us_data_reachable(),
            _check_update_check_reachable(),
            _safe_broker_check(),
        )
        checks.extend([kr_check, us_check, update_check])
        # 새 항목은 뒤에 붙인다 — 기존 항목 순서를 기대하는 소비자를 흔들지 않는다.
        if broker_check is not None:
            checks.append(broker_check)
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
