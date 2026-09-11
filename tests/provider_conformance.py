"""공급자 공통 계약 케이스 (1.0 Task 17).

세 증권사 x 시장별 어댑터를 같은 계약으로 검사하기 위한 시나리오 빌더.
각 케이스는 세 시나리오를 제공한다:

- fetch_single(): 정상 1페이지 (요청일 행 + 전일 행 혼입)
- fetch_partial(): 1페이지 성공 후 2페이지 장애 (partial 이어야 한다)
- fetch_error(): 첫 페이지 장애 (어휘 안의 provider_status 로 raise)

네트워크 없음. 전부 MockTransport.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from stock_mcp_server.market_data.broker_profiles import BrokerCredentials
from stock_mcp_server.market_data.kis_client import KisClient
from stock_mcp_server.market_data.kis_domestic import KisDomesticProvider
from stock_mcp_server.market_data.kis_overseas import KisOverseasProvider
from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
from stock_mcp_server.market_data.kiwoom_domestic import (
    KiwoomDomesticProvider,
)
from stock_mcp_server.market_data.kiwoom_overseas import (
    KiwoomOverseasProvider,
)
from stock_mcp_server.market_data.models import BarRequest
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload
from stock_mcp_server.market_data.toss_client import TossClient
from stock_mcp_server.market_data.toss_provider import TossBarProvider

_KR_DATE = date(2026, 8, 27)
_US_DATE = date(2026, 8, 26)


def run(coro):
    return asyncio.run(coro)


def _secret(provider):
    schema = registry.require(provider).credential_schema
    names = [f.name for f in schema]
    return SecretPayload.from_schema(
        schema, {names[0]: "conf-key", names[1]: "conf-secret"})


def _request(market, **overrides) -> BarRequest:
    base = dict(
        symbol="005930" if market == "KR" else "AAPL",
        market=market, interval="1m", start=None, end=None,
        trading_date=_KR_DATE if market == "KR" else _US_DATE,
        row_limit=50, venue="KRX" if market == "KR" else "NAS",
        session="regular", adjustment="unadjusted", completed_only=True,
        source="auto")
    base.update(overrides)
    return BarRequest(**base)


class Case:
    def __init__(self, name, provider_id, market, tz_name,
                 fetch_single, fetch_partial, fetch_error):
        self.name = name
        self.provider_id = provider_id
        self.market = market
        self.tz_name = tz_name
        self.trading_date = _KR_DATE if market == "KR" else _US_DATE
        self.fetch_single = fetch_single
        self.fetch_partial = fetch_partial
        self.fetch_error = fetch_error


# --- KIS ---

def _kis_kr_rows(times, day="20260827"):
    return [{"stck_bsop_date": day, "stck_cntg_hour": t,
             "stck_oprc": "70000", "stck_hgpr": "70100",
             "stck_lwpr": "69900", "stck_prpr": "70050",
             "cntg_vol": "100"} for t in times]


def _kis_domestic(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json={
                "access_token": "t", "expires_in": 86400})
        cursor = dict(request.url.params).get("FID_INPUT_HOUR_1", "")
        item = pages.get(cursor)
        if item is None:
            return httpx.Response(200, json={"rt_cd": "0", "output2": []})
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    client = KisClient(
        credentials=BrokerCredentials(app_key="k", app_secret="s"),
        profile="real", transport=httpx.MockTransport(handler))
    return KisDomesticProvider(client, "real")


def _kis_kr_single():
    page = {"rt_cd": "0", "output2":
            _kis_kr_rows(["153000", "152900"])
            + _kis_kr_rows(["152900"], day="20260826")}
    provider = _kis_domestic({"153000": page})
    return run(provider.fetch_bars(_request("KR")))


def _kis_kr_partial():
    page = {"rt_cd": "0", "output2": _kis_kr_rows(["153000", "152900"])}
    provider = _kis_domestic({
        "153000": page,
        "152800": httpx.Response(500, json={}),
    })
    return run(provider.fetch_bars(_request("KR")))


def _kis_kr_error():
    provider = _kis_domestic({"153000": httpx.Response(500, json={})})
    return run(provider.fetch_bars(_request("KR")))


def _kis_us_rows(times, day="20260826"):
    return [{"xymd": day, "xhms": t, "open": "230.10", "high": "230.55",
             "low": "229.90", "last": "230.40", "evol": "1200"}
            for t in times]


def _kis_overseas(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json={
                "access_token": "t", "expires_in": 86400})
        keyb = dict(request.url.params).get("KEYB", "")
        item = pages.get(keyb)
        if item is None:
            return httpx.Response(200, json={"rt_cd": "0", "output2": []})
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    client = KisClient(
        credentials=BrokerCredentials(app_key="k", app_secret="s"),
        profile="real", transport=httpx.MockTransport(handler))
    return KisOverseasProvider(client, "real")


def _kis_us_single():
    page = {"rt_cd": "0", "output2":
            _kis_us_rows(["093500", "093400"])
            + _kis_us_rows(["093400"], day="20260825")}
    provider = _kis_overseas({"20260827000000": page})
    return run(provider.fetch_bars(_request("US", source="kis")))


def _kis_us_partial():
    page = {"rt_cd": "0", "output2": _kis_us_rows(["093500", "093400"])}
    provider = _kis_overseas({
        "20260827000000": page,
        "20260826093300": httpx.Response(500, json={}),
    })
    return run(provider.fetch_bars(_request("US", source="kis")))


def _kis_us_error():
    provider = _kis_overseas({
        "20260827000000": httpx.Response(500, json={})})
    return run(provider.fetch_bars(_request("US", source="kis")))


# --- Kiwoom ---

_KIWOOM_TOKEN = {"expires_dt": "20270101000000", "token_type": "bearer",
                 "token": "t", "return_code": 0}


def _kiwoom_kr_rows(times, day="20260827"):
    return [{"cntr_tm": day + t, "cur_prc": "-70050", "open_pric": "+70000",
             "high_pric": "70100", "low_pric": "-69900",
             "trde_qty": "100"} for t in times]


def _kiwoom_pages(path, pages):
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_KIWOOM_TOKEN)
        assert request.url.path == path
        payload, cont, key = pages[state["i"]]
        state["i"] += 1
        if isinstance(payload, httpx.Response):
            return payload
        headers = {}
        if cont:
            headers["cont-yn"] = cont
        if key:
            headers["next-key"] = key
        return httpx.Response(200, json=payload, headers=headers)
    return handler


def _kiwoom_kr(pages):
    handler = _kiwoom_pages("/api/dostk/chart", pages)
    client = KiwoomClient(_secret("kiwoom"), "real",
                          transport=httpx.MockTransport(handler))
    return KiwoomDomesticProvider(client, "real")


def _kiwoom_kr_single():
    page = {"return_code": 0, "stk_min_pole_chart_qry":
            _kiwoom_kr_rows(["153000", "152900"])
            + _kiwoom_kr_rows(["152900"], day="20260826")}
    return run(_kiwoom_kr([(page, None, None)]).fetch_bars(_request("KR")))


def _kiwoom_kr_partial():
    page = {"return_code": 0,
            "stk_min_pole_chart_qry": _kiwoom_kr_rows(
                ["153000", "152900"])}
    provider = _kiwoom_kr([
        (page, "Y", "k1"), (httpx.Response(500, json={}), None, None)])
    return run(provider.fetch_bars(_request("KR")))


def _kiwoom_kr_error():
    provider = _kiwoom_kr([(httpx.Response(500, json={}), None, None)])
    return run(provider.fetch_bars(_request("KR")))


# 키움 US 케이스 없음: 실계좌 실측(2026-08-27, AAPL 완결일 전수 +
# 야후·KIS 이중 기준 대조)에서 가격·거래량·커버리지 계약 불일치가
# 확정되어 어댑터가 US 요청을 차단한다.


# --- Toss ---

_TOSS_TOKEN = {"access_token": "t", "token_type": "Bearer",
               "expires_in": 86400}


def _toss_candles(times, day="2026-08-27", offset="+09:00", price="70050"):
    return [{"timestamp": f"{day}T{t}{offset}", "openPrice": price,
             "highPrice": price, "lowPrice": price, "closePrice": price,
             "volume": "100",
             "currency": "KRW" if offset == "+09:00" else "USD"}
            for t in times]


def _toss(pages):
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOSS_TOKEN)
        item = pages[state["i"]]
        state["i"] += 1
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)

    client = TossClient(_secret("toss"), "real",
                        transport=httpx.MockTransport(handler))
    return TossBarProvider(client, "real")


def _toss_us_single():
    page = {"result": {"candles":
            _toss_candles(["09:35:00", "09:34:00"], day="2026-08-26",
                          offset="-04:00", price="230.40")
            + _toss_candles(["09:34:00"], day="2026-08-25",
                            offset="-04:00", price="229.00"),
            "nextBefore": None}}
    return run(_toss([page]).fetch_bars(_request("US")))


def _toss_us_partial():
    page = {"result": {"candles": _toss_candles(
        ["09:35:00", "09:34:00"], day="2026-08-26", offset="-04:00",
        price="230.40"), "nextBefore": "2026-08-26T09:34:00-04:00"}}
    provider = _toss([page, httpx.Response(500, json={})])
    return run(provider.fetch_bars(_request("US")))


def _toss_us_error():
    provider = _toss([httpx.Response(500, json={})])
    return run(provider.fetch_bars(_request("US")))


ALL_PROVIDER_CASES = (
    Case("kis_kr", "kis", "KR", "Asia/Seoul",
         _kis_kr_single, _kis_kr_partial, _kis_kr_error),
    Case("kis_us", "kis", "US", "America/New_York",
         _kis_us_single, _kis_us_partial, _kis_us_error),
    Case("kiwoom_kr", "kiwoom", "KR", "Asia/Seoul",
         _kiwoom_kr_single, _kiwoom_kr_partial, _kiwoom_kr_error),
    # kiwoom_us 케이스 없음: 계약 불일치 차단 (위 주석 참조)
    # toss_kr 케이스 없음: 실계좌 실측(2026-08-27)에서 정규장 계약
    # 불일치가 확정되어 어댑터가 KR 요청을 차단한다.
    Case("toss_us", "toss", "US", "America/New_York",
         _toss_us_single, _toss_us_partial, _toss_us_error),
)
