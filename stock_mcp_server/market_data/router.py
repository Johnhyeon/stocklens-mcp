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
    primary_provider: str | None = None


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


# 레지스트리 등록 증권사. 목록을 여기 복제하지 않는다.
def _broker_ids() -> tuple[str, ...]:
    from stock_mcp_server.market_data.provider_registry import registry
    return registry.ids()


def _provider_capable(market: str, interval: str,
                      capabilities: dict | None) -> bool:
    if not capabilities or not capabilities.get("connected"):
        return False
    kind = _kind(interval)
    if kind == "daily":
        # 수정주가·기업행위 검증 전까지 증권사 일·주·월봉은 unverified 다.
        return bool(capabilities.get(f"{market.lower()}_daily"))
    return bool(capabilities.get(f"{market.lower()}_intraday"))


def resolve_source(
    *,
    mode: str,
    market: str,
    interval: str,
    requested_source: str,
    capabilities: dict,
    primary_provider: str | None = None,
) -> SourceResolution:
    """공급자 하나를 고정해 돌려준다. 선택 불가면 RouterError.

    capabilities 는 {provider_id: 능력 dict} 형태다. auto 는 주 사용
    증권사(primary_provider) 하나만 본다. 추가 연결된 다른 증권사는
    명시 source 로만 쓸 수 있다.
    """

    def _make(provider: str, reason: str, *, fallback: str | None = None):
        return SourceResolution(
            requested_source=requested_source,
            selected_provider=provider,
            selection_reason=reason,
            fallback_allowed_before_first_bar=fallback is not None,
            mode=mode,
            capability_version=CAPABILITY_VERSION,
            fallback_provider=fallback,
            primary_provider=primary_provider,
        )

    legacy = _legacy_for(market, interval)
    primary_ok = primary_provider is not None and _provider_capable(
        market, interval, capabilities.get(primary_provider))

    # --- 요청별 명시 지정 ---
    if requested_source in _broker_ids():
        # primary 가 아니어도 연결·검증된 공급자는 허용한다. 단 strict:
        # 실패해도 다른 공급원으로 전환하지 않는다.
        if not _provider_capable(market, interval,
                                 capabilities.get(requested_source)):
            raise RouterError(
                "not_configured",
                f"{requested_source}가 연결되어 있지 않거나 이 요청을 "
                f"지원하지 않습니다. source={requested_source}는 strict "
                "모드라 다른 공급원으로 전환하지 않습니다.")
        return _make(requested_source,
                     f"explicit_source_{requested_source}_strict")

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

    # --- legacy 모드: 증권사 호출 0회 ---
    if mode == "legacy":
        if legacy is None:
            raise RouterError(
                "not_configured",
                f"기본 데이터 유지 모드에서는 {market} {interval}을 "
                "지원하는 공급원이 없습니다.")
        return _make(legacy, "legacy_mode_default_source")

    kind = _kind(interval)

    # --- 일·주·월봉: 능력 검증 전까지 기존 공급원 ---
    if kind == "daily":
        if mode == "broker_first" and primary_ok:
            return _make(primary_provider, "broker_first_daily_verified")
        if legacy is None:
            raise RouterError(
                "not_configured",
                f"{market} {interval}을 지원하는 공급원이 없습니다.")
        reason = ("broker_first_daily_capability_unverified"
                  if mode == "broker_first" else "auto_daily_default_source")
        return _make(legacy, reason)

    # --- 분·시간봉 ---
    # 1.0 정책(대표 결정 2026-08-27): 자동 전환 없음. 증권사를 연결한
    # 사용자는 주 사용 증권사 데이터로 고정된다 - 장애 시 다른 공급자로
    # 조용히 바꾸면 거래량 기준(상장 거래소 vs 통합 테이프)이 소리 없이
    # 바뀐다. 전환은 사용자의 직접 선택(source 명시·primary 변경·모드
    # 변경)으로만 한다. 추가 연결된 다른 증권사도 auto 대상이 아니다.
    if primary_ok:
        return _make(primary_provider,
                     "broker_connected_and_intraday_supported")

    if legacy is not None:
        # 미연결·능력 없음은 "전환"이 아니라 요청 시작 전의 초기 선택이다.
        reason = ("broker_capability_unavailable_default_source"
                  if mode == "broker_first"
                  else "auto_broker_unavailable_default_source")
        return _make(legacy, reason)

    raise RouterError(
        "not_configured",
        f"{market} {interval} 분봉을 제공할 공급원이 없습니다. "
        "증권사 연결이 필요합니다.")


async def fetch_with_failover(
    resolution: SourceResolution,
    providers: dict,
    request: BarRequest,
) -> tuple[BarDataset, dict]:
    """고정된 공급자로 조회한다. 자동 전환은 없다 (1.0 정책).

    장애·빈 결과·partial 모두 그대로 보고한다. 다른 공급자의 데이터로
    메우는 순간 거래량 기준 등 숫자의 의미가 소리 없이 바뀐다. 전환은
    사용자의 직접 선택(source 명시·모드 변경)으로만 일어난다.

    함수 이름의 failover 는 역사적 이름이다 - meta 의 fallback_used 는
    하위 호환을 위해 항상 False 로 남는다.
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
    dataset = await primary.fetch_bars(request)
    return dataset, meta
