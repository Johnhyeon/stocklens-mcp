"""미국 심볼·거래소 코드 해석.

거래소를 추측하지 않는다. venue 가 없거나 지원 목록 밖이면 구조화된
SymbolMappingError 를 던진다. 조용히 전체 거래소를 순회하는 것은 금지다.
"""

from __future__ import annotations

# KIS 해외주식 정규장 거래소 코드
REGULAR_EXCHANGES = ("NYS", "NAS", "AMS")

# KIS 미국 주간거래(한국 낮 시간 거래) 코드. 정규장과 섞지 않는다.
DAYTIME_EXCHANGES = ("BAY", "BAA", "BAQ")


class SymbolMappingError(ValueError):
    """비밀 없는 심볼·거래소 매핑 오류."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def resolve_us_symbol(
    symbol: str,
    venue: str | None,
    *,
    session: str = "regular",
) -> tuple[str, str]:
    """(거래소 코드, KIS 심볼) 을 돌려준다.

    심볼 정규화: 대문자, 공백 제거, 클래스 구분자 '-' 를 '.' 로 통일.
    실제 KIS 심볼 표기(클래스 주식)는 실계정 UAT 에서 확정한다.
    """
    cleaned = (symbol or "").strip().upper()
    if not cleaned:
        raise SymbolMappingError("symbol_empty", "심볼이 비어 있습니다")
    cleaned = cleaned.replace("-", ".")

    if venue is None:
        raise SymbolMappingError(
            "exchange_unresolved",
            "거래소(venue)를 확정할 수 없습니다. NYS/NAS/AMS 중 하나를 "
            "지정하세요. 거래소를 추측해 전체 순회하지 않습니다.")

    venue_upper = venue.strip().upper()

    if session == "daytime":
        if venue_upper not in DAYTIME_EXCHANGES:
            raise SymbolMappingError(
                "exchange_unresolved",
                f"daytime 세션에서 지원하지 않는 거래소: {venue_upper} "
                f"(지원: {DAYTIME_EXCHANGES})")
        return venue_upper, cleaned

    if venue_upper in DAYTIME_EXCHANGES:
        raise SymbolMappingError(
            "exchange_unresolved",
            f"{venue_upper}는 주간거래 코드입니다. 정규장 세션과 섞을 수 "
            "없습니다.")
    if venue_upper not in REGULAR_EXCHANGES:
        raise SymbolMappingError(
            "exchange_unresolved",
            f"지원하지 않는 거래소: {venue_upper} (지원: {REGULAR_EXCHANGES})")
    return venue_upper, cleaned
