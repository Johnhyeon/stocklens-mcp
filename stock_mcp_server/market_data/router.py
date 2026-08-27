"""공급원 라우터. 요청 단위로 공급자를 고정한다.

규칙 (설계 8절):
1. 유효한 봉을 하나도 채택하지 않았을 때만 요청 전체 재시작 가능
2. 봉을 하나라도 채택한 뒤에는 공급원 변경 금지
3. 일부 페이지를 다른 공급원으로 보충 금지
4. 공급원 간 평균·합성·다수결 금지
5. 선택 이유와 fallback 여부를 결과에 기록

정책은 선언적 표로 둔다. 사람이 읽는 오류 메시지로 분기하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from stock_mcp_server.market_data.models import (
    DAILY_INTERVALS,
    SUPPORTED_INTERVALS,
    BarDataset,
    BarRequest,
)

CAPABILITY_VERSION = 1


class RouterError(Exception):
    """비밀 없는 라우팅 오류. provider_status 는 설계 27절 값."""

    def __init__(self, provider_status: str, message: str):
        self.provider_status = provider_status
        super().__init__(message)


@dataclass(frozen=True)
class SourceResolution:
    requested_source: str
    selected_provider: str
    selection_reason: str
    fallback_allowed_before_first_bar: bool
    mode: str
    capability_version: int
    fallback_provider: str | None = None


# 시장별 기존(legacy) 공급자. KR 분봉은 기존 공급자가 없다.
_LEGACY_PROVIDER = {
    ("KR", "daily"): "naver",
    ("US", "daily"): "yahoo",
    ("US", "intraday"): "yahoo",
}

# Yahoo 가 지원하는 분봉 간격 (기존 get_us_chart 계약과 동일).
_YAHOO_INTRADAY = ("1m", "5m", "15m", "30m", "60m")


def _kind(interval: str) -> str:
    if interval in DAILY_INTERVALS:
        return "daily"
    if interval in SUPPORTED_INTERVALS:
        return "intraday"
    raise RouterError("entity_not_found",
                      f"지원하지 않는 interval: {interval}")


def _legacy_for(market: str, interval: str) -> str | None:
    kind = _kind(interval)
    provider = _LEGACY_PROVIDER.get((market, kind))
    if provider == "yahoo" and kind == "intraday" and \
            interval not in _YAHOO_INTRADAY:
        return None
    return provider


def _kis_capable(market: str, interval: str, capabilities: dict) -> bool:
    if not capabilities.get("connected"):
        return False
    kind = _kind(interval)
    if kind == "daily":
        # 수정주가·기업행위 검증 전까지 KIS 일·주·월봉은 unverified 다.
        return bool(capabilities.get(f"{market.lower()}_daily"))
    return bool(capabilities.get(f"{market.lower()}_intraday"))


def resolve_source(
    *,
    mode: str,
    market: str,
    interval: str,
    requested_source: str,
    capabilities: dict,
) -> SourceResolution:
    """공급자 하나를 고정해 돌려준다. 선택 불가면 RouterError."""

    def _make(provider: str, reason: str, *, fallback: str | None = None):
        return SourceResolution(
            requested_source=requested_source,
            selected_provider=provider,
            selection_reason=reason,
            fallback_allowed_before_first_bar=fallback is not None,
            mode=mode,
            capability_version=CAPABILITY_VERSION,
            fallback_provider=fallback,
        )

    legacy = _legacy_for(market, interval)
    kis_ok = _kis_capable(market, interval, capabilities)

    # --- 요청별 명시 지정 ---
    if requested_source == "kis":
        if not kis_ok:
            raise RouterError(
                "not_configured",
                "KIS가 연결되어 있지 않거나 이 요청을 지원하지 않습니다. "
                "source=kis는 strict 모드라 다른 공급원으로 전환하지 않습니다.")
        return _make("kis", "explicit_source_kis_strict")

    if requested_source == "naver":
        if market != "KR" or _kind(interval) != "daily":
            raise RouterError(
                "entity_not_found", "naver는 KR 일·주·월봉만 지원합니다.")
        return _make("naver", "explicit_source_naver")

    if requested_source == "yahoo":
        if market != "US" or _legacy_for("US", interval) != "yahoo":
            raise RouterError(
                "entity_not_found",
                "yahoo는 US 일·주·월봉과 1m/5m/15m/30m/60m 분봉만 지원합니다.")
        return _make("yahoo", "explicit_source_yahoo")

    if requested_source != "auto":
        raise RouterError(
            "entity_not_found",
            f"지원하지 않는 source: {requested_source}")

    # --- legacy 모드: KIS 호출 0회 ---
    if mode == "legacy":
        if legacy is None:
            raise RouterError(
                "not_configured",
                f"기본 데이터 유지 모드에서는 {market} {interval}을 "
                "지원하는 공급원이 없습니다.")
        return _make(legacy, "legacy_mode_default_source")

    kind = _kind(interval)

    # --- 일·주·월봉: KIS 능력 검증 전까지 기존 공급원 ---
    if kind == "daily":
        if mode == "broker_first" and kis_ok:
            return _make("kis", "broker_first_daily_verified")
        if legacy is None:
            raise RouterError(
                "not_configured",
                f"{market} {interval}을 지원하는 공급원이 없습니다.")
        reason = ("broker_first_daily_capability_unverified"
                  if mode == "broker_first" else "auto_daily_default_source")
        return _make(legacy, reason)

    # --- 분·시간봉 ---
    if kis_ok:
        fallback = legacy if mode in ("auto", "broker_first") else None
        return _make("kis", "broker_connected_and_intraday_supported",
                     fallback=fallback)

    if legacy is not None:
        reason = ("broker_first_unavailable_full_restart_on_legacy"
                  if mode == "broker_first"
                  else "auto_broker_unavailable_default_source")
        return _make(legacy, reason)

    raise RouterError(
        "not_configured",
        f"{market} {interval} 분봉을 제공할 공급원이 없습니다. "
        "KIS 연결이 필요합니다.")


async def fetch_with_failover(
    resolution: SourceResolution,
    providers: dict,
    request: BarRequest,
) -> tuple[BarDataset, dict]:
    """고정된 공급자로 조회한다.

    공급자가 봉을 하나도 채택하지 못하고 실패했을 때만, 그리고 fallback 이
    허용된 경우에만 요청 전체를 fallback 공급자에서 새로 시작한다.
    일부 반환(partial)은 그대로 반환하고 절대 다른 공급원으로 메우지 않는다.
    """
    primary = providers[resolution.selected_provider]
    meta = {
        "requested_source": resolution.requested_source,
        "selected_provider": resolution.selected_provider,
        "selection_reason": resolution.selection_reason,
        "mode": resolution.mode,
        "fallback_used": False,
        "fallback_from": None,
    }
    fallback_possible = (resolution.fallback_allowed_before_first_bar
                         and resolution.fallback_provider is not None)

    async def _restart_on_fallback():
        fallback = providers[resolution.fallback_provider]
        ds = await fallback.fetch_bars(request)
        meta.update({
            "selected_provider": resolution.fallback_provider,
            "fallback_used": True,
            "fallback_from": resolution.selected_provider,
        })
        return ds, meta

    try:
        dataset = await primary.fetch_bars(request)
    except Exception:
        # 예외 = 봉 0개 채택. 공급자는 봉을 하나라도 채택했으면 partial
        # dataset 을 반환하지 예외를 던지지 않는다 (kis_domestic 참고).
        if not fallback_possible:
            raise
        return await _restart_on_fallback()

    # 빈 dataset 도 "유효한 봉을 하나도 채택하지 않은" 상태다. 이때만
    # 요청 전체 재시작이 허용된다 (2026-08-27 실측: 미국 야간에 KIS 가
    # 정규장 밖 행만 돌려줘 빈 결과가 나왔다).
    if not dataset.bars and fallback_possible:
        return await _restart_on_fallback()
    return dataset, meta
