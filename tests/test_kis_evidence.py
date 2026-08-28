"""KIS 상세 수급 어댑터 (1.1, 2026-08-28 실계좌 실측 기반).

실측으로 확정한 계약 (GET inquire-investor, tr_id FHKST01010900):
- 응답 키 `output`, 30행
- 투자자 구분은 **3종뿐**이다 (prsn 개인, frgn 외국인, orgn 기관계).
  키움이 주는 금융투자·보험·투신·은행·연기금·사모·국가 같은 기관 세부
  분해가 없다.
- 대신 구분마다 **순매수·매수·매도 × 수량·금액** 6개를 준다. 키움은
  순매매 하나뿐이다. 어느 쪽도 상위집합이 아니라서 capability 를
  공급자별로 정직하게 갈라야 한다.
- **정산 전 당일 행은 빈 문자열이다** (18개 필드 전부). 키움이 0 을
  주는 것과 표현만 다르고 뜻은 같다 - 값이 아니라 미정산 상태다.

교차 검증 (2026-08-27 기준, 삼성전자):
- KIS prsn_ntby_qty -3,223,427 = 키움 ind_invsr -3,223,427 (자릿수 일치)
- KIS 자체 산술: 매수 2,310,368 - 매도 5,533,795 = -3,223,427
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.kis_client import KisClient
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_F = Path(__file__).parent / "fixtures" / "kis"
_DAILY = json.loads((_F / "evidence_investor_daily.json").read_text("utf-8"))
_TOKEN = {"access_token": "T", "expires_in": 86400}


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kis").credential_schema,
        {"app_key": "k", "app_secret": "s"})


class Server:
    def __init__(self, payload=None, status=200):
        self.requests: list[httpx.Request] = []
        self.payload = payload if payload is not None else _DAILY
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json=_TOKEN)
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


def _provider(server: Server):
    from stock_mcp_server.market_data.kis_evidence import (
        KisEvidenceProvider,
    )

    client = KisClient(_payload(), "real",
                       transport=httpx.MockTransport(server.handler))
    return KisEvidenceProvider(client, "real")


def _run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    def test_investor_endpoint_is_registered(self):
        d = registry.require("kis")
        spec = d.endpoint("kr_investor_daily")
        self.assertIsNotNone(spec, "inquire-investor 미등록")
        self.assertEqual(spec.method, "GET")
        self.assertTrue(spec.path.endswith("/inquire-investor"))
        self.assertIn(spec.path, d.allowed_paths)

    def test_no_account_or_order_paths(self):
        for path in registry.require("kis").allowed_paths:
            low = path.lower()
            for banned in ("order", "account", "balance", "psbl",
                           "deposit", "acnt", "ccnl"):
                self.assertNotIn(banned, low, path)


class InvestorFlowTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.result = _run(_provider(self.server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28)))

    def test_request_contract(self):
        req = self.server.requests[0]
        self.assertTrue(req.url.path.endswith("/inquire-investor"))
        self.assertEqual(req.headers["tr_id"], "FHKST01010900")
        self.assertEqual(req.url.params.get("FID_INPUT_ISCD"), "005930")
        self.assertEqual(req.url.params.get("FID_COND_MRKT_DIV_CODE"), "J")

    def test_settled_day_values_and_units(self):
        row = self.result.rows[1]  # 20260827
        self.assertEqual(row.date, date(2026, 8, 27))
        self.assertEqual(row.data_state, "final")
        self.assertEqual(row.value("individual"), -3223427)
        self.assertEqual(row.value("foreign"), 1381786)
        self.assertEqual(row.value("institution_total"), -97433)
        self.assertEqual(self.result.measure, "net_quantity")
        self.assertEqual(self.result.unit, "shares")

    def test_net_equals_buy_minus_sell(self):
        # KIS 는 매수·매도를 함께 주므로 자체 검산이 된다.
        for row in self.result.rows:
            if row.data_state != "final":
                continue
            for name in ("individual", "foreign", "institution_total"):
                net = row.value(name)
                buy = row.value(f"{name}_buy")
                sell = row.value(f"{name}_sell")
                if None in (net, buy, sell):
                    continue
                self.assertEqual(net, buy - sell,
                                 f"{row.date} {name} 검산 불일치")

    def test_amount_measure_switches_fields(self):
        server = Server()
        result = _run(_provider(server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), measure="net_amount"))
        self.assertEqual(result.unit, "KRW_million")
        self.assertEqual(result.rows[1].value("individual"), -862106)

    def test_unsettled_day_is_provisional_not_zero(self):
        row = self.result.rows[0]  # 20260828, 빈 문자열
        self.assertEqual(row.date, date(2026, 8, 28))
        self.assertEqual(row.data_state, "provisional")
        self.assertIsNone(row.value("individual"))
        self.assertTrue(row.is_unsettled("individual"))
        self.assertEqual(self.result.data_state, "provisional")
        self.assertIn("정산", " ".join(self.result.warnings))

    def test_raw_field_names_are_preserved(self):
        row = self.result.rows[1]
        self.assertEqual(row.raw_category("individual"), "prsn_ntby_qty")
        self.assertEqual(row.raw_category("foreign_buy"), "frgn_shnu_vol")


class CapabilityDifferenceTests(unittest.TestCase):
    """공급자 차이를 정직하게 신고한다. 어느 쪽도 상위집합이 아니다."""

    def test_kis_reports_no_institution_breakdown(self):
        from stock_mcp_server.market_data.kis_evidence import (
            KisEvidenceProvider,
        )

        caps = KisEvidenceProvider.capabilities()
        self.assertEqual(caps["kr.investor_flow.daily.total"], "available")
        self.assertEqual(caps["kr.investor_flow.daily.breakdown"],
                         "unsupported")
        # 대신 매수·매도 분해는 KIS 만 준다.
        self.assertEqual(caps["kr.investor_flow.daily.buy_sell"],
                         "available")

    def test_kiwoom_reports_the_mirror_image(self):
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )

        caps = KiwoomEvidenceProvider.capabilities()
        self.assertEqual(caps["kr.investor_flow.daily.breakdown"],
                         "available")
        self.assertEqual(caps["kr.investor_flow.daily.buy_sell"],
                         "unsupported")

    def test_no_provider_claims_a_capability_it_lacks(self):
        from stock_mcp_server.market_data.kis_evidence import (
            KisEvidenceProvider,
        )
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )

        for provider in (KisEvidenceProvider, KiwoomEvidenceProvider):
            for key, value in provider.capabilities().items():
                self.assertIn(value, ("available", "unsupported"), key)


class ErrorTests(unittest.TestCase):
    def test_provider_error_is_classified(self):
        from stock_mcp_server.market_data.kis_client import KisApiError

        server = Server(payload={"rt_cd": "1", "msg1": "오류"})
        with self.assertRaises(KisApiError) as ctx:
            _run(_provider(server).fetch_investor_flow(
                "005930", base_date=date(2026, 8, 28)))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")

    def test_empty_output_is_entity_not_found(self):
        from stock_mcp_server.market_data.kis_client import KisApiError

        server = Server(payload={"rt_cd": "0", "output": []})
        with self.assertRaises(KisApiError) as ctx:
            _run(_provider(server).fetch_investor_flow(
                "ZZZZZZ", base_date=date(2026, 8, 28)))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")


if __name__ == "__main__":
    unittest.main()
