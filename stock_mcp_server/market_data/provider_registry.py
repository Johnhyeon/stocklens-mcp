"""증권사 Provider Registry (1.0 설계 5절).

증권사 정의는 이 파일의 불변 상수뿐이다. 외부 설정 파일·환경 변수·
사용자 입력으로 공급자를 추가하거나 host·path 를 바꿀 수 없다.

허용 목록 원칙: 실측으로 검증한 시세·인증 endpoint 만 올린다.
주문·계좌·잔고 계열 경로는 어떤 descriptor 에도 정의하지 않는다.
(키움 미국 분 차트 경로는 공식 가이드 확정 후 해당 Task 에서 추가한다.)

factory 필드는 이후 Task 에서 어댑터가 구현되는 대로 연결된다.
serialize 되는 공개 출력(describe_public)에는 절대 포함하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class UnknownProviderError(Exception):
    """레지스트리에 없는 provider 요청."""


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = True
    max_length: int = 2048


@dataclass(frozen=True)
class EndpointSpec:
    """descriptor 가 소유하는 정확한 endpoint. 호출자는 URL 을 만들 수 없다."""

    endpoint_id: str
    method: str
    path: str


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    credential_schema: tuple[CredentialField, ...]
    supported_profiles: tuple[str, ...]
    signup_url: str
    docs_url: str
    # 프로필 -> API host. transport 가 여기서만 host 를 고른다.
    hosts_by_profile: tuple[tuple[str, str], ...] = ()
    # endpoint_id -> (method, path). path 는 allowed_paths 부분집합이다.
    endpoints: tuple[EndpointSpec, ...] = ()
    # 어댑터 factory. 공개 직렬화 대상이 아니며 repr 에서도 감춘다.
    auth_factory: Callable[..., Any] | None = field(default=None, repr=False)
    provider_factory: Callable[..., Any] | None = field(
        default=None, repr=False)
    capability_probe_factory: Callable[..., Any] | None = field(
        default=None, repr=False)

    def host_for_profile(self, profile: str) -> str | None:
        for name, host in self.hosts_by_profile:
            if name == profile:
                return host
        return None

    def endpoint(self, endpoint_id: str) -> EndpointSpec | None:
        for spec in self.endpoints:
            if spec.endpoint_id == endpoint_id:
                return spec
        return None


_KIS = ProviderDescriptor(
    provider_id="kis",
    display_name="한국투자증권",
    allowed_hosts=(
        "openapi.koreainvestment.com:9443",
        "openapivts.koreainvestment.com:29443",
    ),
    allowed_paths=(
        "/oauth2/tokenP",
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
        "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
        # 상세 수급 (1.1, 2026-08-28 실측). 시세 조회 전용 경로만 올린다.
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
    ),
    credential_schema=(
        CredentialField(name="app_key", label="앱 키"),
        CredentialField(name="app_secret", label="앱 시크릿"),
    ),
    supported_profiles=("real", "demo"),
    signup_url="https://apiportal.koreainvestment.com",
    docs_url="https://apiportal.koreainvestment.com/docs",
    hosts_by_profile=(
        ("real", "openapi.koreainvestment.com:9443"),
        ("demo", "openapivts.koreainvestment.com:29443"),
    ),
    endpoints=(
        EndpointSpec("token", "POST", "/oauth2/tokenP"),
        EndpointSpec(
            "kr_minute", "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"),
        EndpointSpec(
            "us_minute", "GET",
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"),
        # 종목별 투자자 일별 수급 (FHKST01010900)
        EndpointSpec(
            "kr_investor_daily", "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-investor"),
    ),
)

_KIWOOM = ProviderDescriptor(
    provider_id="kiwoom",
    display_name="키움증권",
    allowed_hosts=(
        "api.kiwoom.com",
        "mockapi.kiwoom.com",
    ),
    allowed_paths=(
        "/oauth2/token",
        "/api/dostk/chart",
        "/api/us/chart",
        # 상세 수급 (1.1). 2026-08-28 실측으로 확정한 경로만 올린다.
        # 계좌·주문·잔고 계열 경로는 어떤 이유로도 추가하지 않는다.
        # (키움 신용 '주문' TR kt100xx 는 조회 TR 과 별개이며 쓰지 않는다.)
        "/api/dostk/stkinfo",
        "/api/dostk/mrkcond",
        "/api/dostk/shsa",
        "/api/dostk/slb",
        "/api/dostk/frgnistt",
    ),
    credential_schema=(
        CredentialField(name="app_key", label="앱 키"),
        CredentialField(name="secret_key", label="시크릿 키"),
    ),
    supported_profiles=("real", "demo"),
    signup_url="https://openapi.kiwoom.com",
    docs_url="https://openapi.kiwoom.com/m/guide/apiguide",
    hosts_by_profile=(
        ("real", "api.kiwoom.com"),
        ("demo", "mockapi.kiwoom.com"),
    ),
    endpoints=(
        EndpointSpec("token", "POST", "/oauth2/token"),
        EndpointSpec("kr_chart", "POST", "/api/dostk/chart"),
        # 공식 스펙(usa06011): 미국주식 분 차트. POST /api/us/chart
        EndpointSpec("us_chart", "POST", "/api/us/chart"),
        # 상세 수급 (1.1, 2026-08-28 실측):
        # ka10059 종목별 투자자·기관별 일별 -> /api/dostk/stkinfo
        # ka10063 장중 투자자별 / ka10066 장마감 투자자별 -> /api/dostk/mrkcond
        EndpointSpec("kr_investor_daily", "POST", "/api/dostk/stkinfo"),
        EndpointSpec("kr_investor_market", "POST", "/api/dostk/mrkcond"),
        # 수급 압력 (1.1, 2026-08-28 실측):
        # ka90013 프로그램매매(종목별) / ka10014 공매도추이 /
        # ka10013 신용매매동향 / ka20068 대차거래추이 /
        # ka10008 주식외국인종목별매매동향
        EndpointSpec("kr_program_trade", "POST", "/api/dostk/mrkcond"),
        EndpointSpec("kr_short_selling", "POST", "/api/dostk/shsa"),
        EndpointSpec("kr_credit_trade", "POST", "/api/dostk/stkinfo"),
        EndpointSpec("kr_securities_lending", "POST", "/api/dostk/slb"),
        EndpointSpec("kr_foreign_holding", "POST", "/api/dostk/frgnistt"),
    ),
)

_TOSS = ProviderDescriptor(
    provider_id="toss",
    display_name="토스증권",
    allowed_hosts=(
        "openapi.tossinvest.com",
    ),
    allowed_paths=(
        "/oauth2/token",
        "/api/v1/candles",
    ),
    credential_schema=(
        CredentialField(name="client_id", label="클라이언트 ID"),
        CredentialField(name="client_secret", label="클라이언트 시크릿"),
    ),
    # 캔들 세션·시장 범위 실측 인증 전까지 실전 프로필만 노출한다.
    supported_profiles=("real",),
    signup_url="https://corp.tossinvest.com/ko/open-api",
    docs_url="https://developers.tossinvest.com/docs",
    hosts_by_profile=(
        ("real", "openapi.tossinvest.com"),
    ),
    endpoints=(
        EndpointSpec("token", "POST", "/oauth2/token"),
        EndpointSpec("candles", "GET", "/api/v1/candles"),
    ),
)


# ---------------------------------------------------------------------------
# 출시 검증 게이트 (리뷰 차단 항목 2, 2026-08-27)
#
# 연결 시험의 available 은 "키와 endpoint 호출이 정상"만 뜻한다
# (endpoint_available). 자동 라우터가 실제로 쓰는 능력은 실계좌 UAT 와
# 출시 게이트를 통과해 이 표에 True 로 기록된 것뿐이다(release_verified).
# 항목을 켜는 커밋은 반드시 해당 UAT 증거 커밋과 짝을 이룬다.
# ---------------------------------------------------------------------------

_RELEASE_VERIFIED: dict[tuple[str, str], bool] = {
    # KIS KR: 0.9 브랜치 국내 15종목 UAT 2회(장중·장마감) + 재계산
    # 3213 버킷 불일치 0 + 공식 종가 대조 (2026-08-27)
    ("kis", "kr_intraday"): True,
    # KIS US: strict 러너(독립 집계) 15사례 failures=0, 검산 9,690 버킷
    # 불일치 0, 페이지네이션 7~11페이지 실증 (2026-08-28 01:43 KST 본장,
    # docs/uat/evidence/kis/uat_kis_us_20260828.json)
    # SPY·IWM 은 EXCD=AMS, BRK.B 는 SYMB "BRK/B" (실측 표기)
    ("kis", "us_intraday"): True,
    # 키움 KR: strict 러너 2회 통과 - 장마감(2026-08-27, 16사례
    # failures=0, KIS 교차 전종목 완전 일치) + 장중(2026-08-28 09:45,
    # failures=0, 검산 11,154 버킷 0, KIS 교차 16/16, 형성 중 분 제외
    # 규칙 고정). uat_kiwoom_kr_20260827.json / _20260828.json
    ("kiwoom", "kr_intraday"): True,
    # 키움 US: strict 러너 15사례 failures=0, 검산 4,663 버킷 0,
    # 완결일 KIS 교차 391/391·OHLC diff 0·거래량 비율 1.0 (2026-08-28,
    # docs/uat/evidence/kiwoom/uat_kiwoom_us_20260828.json). 원인 규명
    # us-contract-resolution-20260828.json (cntr_tm=ET 라벨, db51bab)
    ("kiwoom", "us_intraday"): True,
    # 토스 KR: 계약 불일치로 차단 (봉 라벨 시프트·통합 거래량·마감
    # 동시호가 부재, 2026-08-27 실측)
    ("toss", "kr_intraday"): False,
    ("toss", "us_intraday"): False,
    # 일·주·월봉은 수정주가·기업행위 검증 게이트 전이라 전부 미검증.
}


def is_release_verified(provider_id: str, capability: str) -> bool:
    return bool(_RELEASE_VERIFIED.get((provider_id, capability)))


class ProviderRegistry:
    def __init__(self, descriptors: tuple[ProviderDescriptor, ...]):
        self._by_id = {d.provider_id: d for d in descriptors}
        self._order = tuple(d.provider_id for d in descriptors)

    def ids(self) -> tuple[str, ...]:
        return self._order

    def require(self, provider_id: str) -> ProviderDescriptor:
        descriptor = self._by_id.get(provider_id)
        if descriptor is None:
            raise UnknownProviderError(
                f"등록되지 않은 provider: {provider_id!r}")
        return descriptor

    def describe_public(self) -> list[dict]:
        """Manager·CLI·doctor 용 안전한 공개 계약.

        factory·host·path 는 포함하지 않는다. host·path 는 transport
        계층이 레지스트리에서 직접 읽는다.
        """
        entries = []
        for provider_id in self._order:
            d = self._by_id[provider_id]
            entries.append({
                "provider_id": d.provider_id,
                "display_name": d.display_name,
                "supported_profiles": list(d.supported_profiles),
                "signup_url": d.signup_url,
                "docs_url": d.docs_url,
                "credential_fields": [
                    {
                        "name": f.name,
                        "label": f.label,
                        "secret": f.secret,
                        "max_length": f.max_length,
                    }
                    for f in d.credential_schema
                ],
            })
        return entries


registry = ProviderRegistry((_KIS, _KIWOOM, _TOSS))
