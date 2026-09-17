"""StockLens 상태 요약 — `stocklens_status` MCP 도구가 쓰는 경량 스냅샷.

`doctor`/`diagnostics`처럼 네트워크로 매번 재확인하지 않는다 — 대화 중 가볍게
불러도 되는 "지금 상태 한눈에" 용도라, 이미 모든 도구 호출을 기록하는
`_metrics` JSONL 로그와 `_update_check`의 24시간 캐시를 그대로 재사용해서
조립한다(새 계측·새 네트워크 호출 없음).

예외 하나: 라이선스 판정은 도구 잠금과 같은 `licensing.license_block_reason()`을
쓰므로, 폐기 목록 캐시가 하루 지났으면 프로세스당 한 번 목록을 받는다(2.5초 제한).
다른 도구를 처음 부를 때도 똑같이 일어나는 일이라 여기서만 느려지는 건 아니다.

깊은 진단(오프라인 재현, 실제 국내/미국 시세 재조회 등)은 `diagnostics`(LeetKit
Manager의 [진단]) 몫이다. 고객에게는 Manager 버튼으로만 안내한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from stock_mcp_server import __version__
from stock_mcp_server._metrics import load_metrics

# 최근 성공/실패, 국내·미국 시장 상태 판정에 쓰는 로그 조회 범위(일).
_METRICS_LOOKBACK_DAYS = 2
# 시장 상태 판정 시 볼 최근 호출 개수 — 오래된 실패가 최신 상태를 왜곡하지 않도록 제한.
_MARKET_STATUS_SAMPLE = 20

# 시장 데이터 조회와 무관한 유틸리티 도구 — kr/us 버킷 판정에서 제외.
_NON_MARKET_TOOLS = {
    "get_metrics_summary",
    "get_market_clock",
    "stocklens_status",
    "export_to_excel",
    "export_us_to_excel",
    "scan_to_excel",
    "query_excel",
}


@dataclass
class StatusSnapshot:
    package_version: str
    # "active" | "missing" | "invalid" | "expired" | "revoked" | "clock"
    # 뒤의 셋은 키 자체는 진짜인데 지금 못 쓰는 상태다. 예전엔 서명만 보고 전부
    # "active"로 보고해서, 체험이 끝나 도구는 잠겼는데 여기서는 "활성화됨"이라고 했다.
    license_status: str
    kr_market_status: str  # "ok" | "degraded" | "down" | "unknown"
    us_market_status: str
    last_success_at: str | None
    last_failure_at: str | None
    last_failure_error_code: str | None
    cache_writable: bool
    update_available: bool
    latest_version: str | None

    def to_dict(self) -> dict:
        return {
            "package_version": self.package_version,
            "license_status": self.license_status,
            "kr_market_status": self.kr_market_status,
            "us_market_status": self.us_market_status,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_error_code": self.last_failure_error_code,
            "cache_writable": self.cache_writable,
            "update_available": self.update_available,
            "latest_version": self.latest_version,
        }


def _license_status() -> str:
    """도구 잠금 안내(`licensing.locked_message`)와 Manager 진단
    (`diagnostics._license_summary`)이 쓰는 판정 함수를 그대로 쓴다.

    `verify_key`는 형식·서명만 본다. 그걸로 판정하면 기간이 끝났거나 중지된 키도
    "활성화됨"이 된다 — 도구는 잠겨 있는데.
    """
    from stock_mcp_server import licensing

    return licensing.license_block_reason() or "active"


def _cache_writable() -> bool:
    from stock_mcp_server._excel import get_snapshot_dir

    try:
        folder = get_snapshot_dir()
        probe = folder / ".status_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _recent_success_and_failure(records: list[dict]) -> tuple[str | None, str | None, str | None]:
    """기록에서 가장 최근 성공/실패 timestamp(+실패 시 error 타입)를 뽑는다."""
    last_success: str | None = None
    last_failure: str | None = None
    last_failure_error: str | None = None
    for r in records:
        ts = r.get("timestamp")
        if not ts:
            continue
        if r.get("error"):
            if last_failure is None or ts > last_failure:
                last_failure = ts
                last_failure_error = r["error"]
        elif last_success is None or ts > last_success:
            last_success = ts
    return last_success, last_failure, last_failure_error


def _market_bucket(tool: str) -> str | None:
    if not tool or tool in _NON_MARKET_TOOLS:
        return None
    return "us" if tool.startswith("get_us_") or tool.startswith("export_us_") else "kr"


def _market_status(records: list[dict], bucket: str) -> str:
    relevant = [r for r in records if _market_bucket(r.get("tool", "")) == bucket]
    if not relevant:
        return "unknown"
    relevant.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    recent = relevant[:_MARKET_STATUS_SAMPLE]
    errors = sum(1 for r in recent if r.get("error"))
    if errors == 0:
        return "ok"
    if errors == len(recent):
        return "down"
    return "degraded"


def _update_info() -> tuple[bool, str | None]:
    """`_update_check`의 24시간 캐시를 그대로 읽는다 — 여기서 새로 네트워크를 타지 않는다."""
    from stock_mcp_server._update_check import _load_cache, _version_gt

    cached = _load_cache()
    if not cached:
        return False, None
    latest = cached.get("latest_version") or None
    if not latest:
        return False, None
    return _version_gt(latest, __version__), latest


def build_status() -> StatusSnapshot:
    records = load_metrics(days=_METRICS_LOOKBACK_DAYS)
    last_success, last_failure, last_failure_error = _recent_success_and_failure(records)
    update_available, latest_version = _update_info()

    return StatusSnapshot(
        package_version=__version__,
        license_status=_license_status(),
        kr_market_status=_market_status(records, "kr"),
        us_market_status=_market_status(records, "us"),
        last_success_at=last_success,
        last_failure_at=last_failure,
        last_failure_error_code=last_failure_error,
        cache_writable=_cache_writable(),
        update_available=update_available,
        latest_version=latest_version,
    )


_LICENSE_LABEL = {
    "active": "활성화됨",
    "missing": "미활성화(키 없음)",
    # 깨진 키·다른 제품 키·서명 불일치가 전부 여기로 온다. "서명 불일치"로 못박으면
    # 복사하다 한 글자 빠진 사람에게도 위조라고 말하는 셈이다.
    "invalid": "유효하지 않은 키",
    "expired": "사용 기간 끝남",
    "revoked": "사용 중지됨",
    "clock": "컴퓨터 날짜 확인 필요",
}

# 상태마다 할 일이 다르다(diagnostics._LICENSE_BLOCKED_FIX와 같은 이유). 안내는
# LeetKit Manager 버튼으로만 한다 — 터미널 명령은 적지 않는다. 이 글은 Claude 답변 안에
# 들어가므로 도구 잠금 안내(licensing.*_MESSAGE)와 같은 문장을 쓴다. 둘이 다르면
# 같은 상태를 두 가지 말로 듣게 된다.
_LICENSE_NEXT_STEP = {
    "missing": (
        "LeetKit Manager의 StockLens 카드에서 [활성화]를 눌러 메일로 받은 키를 넣어주세요.",
        "그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요.",
    ),
    "invalid": (
        "LeetKit Manager의 StockLens 카드에서 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요.",
        "그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요.",
    ),
    "expired": (
        "계속 쓰시려면 LeetKit Manager의 StockLens 카드에서 [구매]를 누르고, "
        "받은 키를 같은 카드의 [활성화]로 넣어주세요.",
    ),
    "revoked": (
        "환불이나 결제 취소로 중지된 키예요.",
        "착오라면 LeetKit Manager 상단 [지원 문의]를 눌러 알려주세요.",
    ),
    "clock": (
        "날짜와 시간을 오늘로 맞춘 뒤 다시 물어봐 주세요.",
        "그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요.",
    ),
}
_MARKET_LABEL = {"ok": "정상", "degraded": "일부 실패", "down": "장애", "unknown": "기록 없음"}


def format_status(status: StatusSnapshot) -> str:
    d = status.to_dict()
    version_line = f"- 버전: v{status.package_version}"
    if status.update_available and status.latest_version:
        version_line += f" (최신 v{status.latest_version} — 업데이트 가능)"

    lines = [
        "StockLens 상태",
        version_line,
        f"- 라이선스: {_LICENSE_LABEL.get(status.license_status, status.license_status)}",
        *(f"  {step}" for step in _LICENSE_NEXT_STEP.get(status.license_status, ())),
        f"- 국내 시장: {_MARKET_LABEL.get(status.kr_market_status, status.kr_market_status)}",
        f"- 미국 시장: {_MARKET_LABEL.get(status.us_market_status, status.us_market_status)}",
        f"- 최근 성공 조회: {status.last_success_at or '기록 없음'}",
        "- 최근 실패 조회: "
        + (
            f"{status.last_failure_at} ({status.last_failure_error_code})"
            if status.last_failure_at
            else "없음"
        ),
        f"- 캐시 쓰기: {'가능' if status.cache_writable else '불가'}",
        "",
        "STOCKLENS_STATUS_JSON_START",
        json.dumps(d, ensure_ascii=False, sort_keys=True),
        "STOCKLENS_STATUS_JSON_END",
    ]
    return "\n".join(lines)
