"""키움 미국주식 심볼·거래소 매핑 (1.0 Task 14).

usa06011 의 stex_tp 는 NA(AMEX), ND(NASDAQ), NY(NYSE) 다. 요청 venue
(NYS/NAS/AMS, KIS 계열 코드)를 stex_tp 로 변환한다.

원칙: 추측 금지. 모르는 거래소·미검증 심볼 형식은 구조화된 오류를 낸다.
키움의 class 주식 표기(BRK.B / BRK/B 등)는 실계좌 실측으로 검증하기
전까지 지원하지 않는다.
"""

from __future__ import annotations

_VENUE_TO_STEX = {
    "NYS": "NY",
    "NAS": "ND",
    "AMS": "NA",
}


class KiwoomSymbolMappingError(Exception):
    """비밀 없는 매핑 오류. provider_status 는 entity_not_found 로 다룬다."""

    provider_status = "entity_not_found"


def to_stex_tp(venue: object) -> str:
    stex = _VENUE_TO_STEX.get(venue) if isinstance(venue, str) else None
    if stex is None:
        raise KiwoomSymbolMappingError(
            f"kiwoom 미국 시세가 지원하지 않는 거래소 코드입니다: {venue!r} "
            f"(지원: {tuple(_VENUE_TO_STEX)})")
    return stex


def validate_ticker(symbol: object) -> str:
    """대문자 영숫자 ticker 만 허용한다. class 표기는 검증 전이라 거부."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise KiwoomSymbolMappingError("빈 심볼은 조회할 수 없습니다")
    ticker = symbol.strip().upper()
    if not ticker.isalnum() or not ticker.isascii():
        raise KiwoomSymbolMappingError(
            f"검증되지 않은 심볼 형식입니다: {symbol!r}. "
            "키움 미국 시세는 영숫자 ticker만 지원합니다 "
            "(class 주식 표기는 실측 검증 전).")
    return ticker
