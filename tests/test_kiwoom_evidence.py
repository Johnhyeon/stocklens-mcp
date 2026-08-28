"""키움 상세 수급 어댑터 (1.1 Task 9, 2026-08-28 실계좌 실측 기반).

실측으로 확정한 계약 (ka10059, POST /api/dostk/stkinfo):
- 응답 키 `stk_invsr_orgn`, 페이지 100행, 헤더 cont-yn / next-key
- 투자자 구분 13종이 원본 필드명으로 온다 (ind_invsr, frgnr_invsr, orgn,
  fnnc_invt, insrnc, invtrt, etc_fnnc, bank, penfnd_etc, samo_fund,
  natn, etc_corp, natfor)
- **정산 전 당일 행은 0 이 "매매 없음"이 아니라 "아직 정산 안 됨"이다.**
  정산된 날은 개인+외국인+기관계+기타법인+내외국인 합이 0(반올림 오차
  ±1)이고 기관 세부 8종 합이 기관계와 같다. 당일 행은 이 검산이 깨진다
  (실측 2026-08-28: 개인 0, 5주체 합 216,832). 그 0 을 확정 수치로
  내보내면 라벨-값 계약 위반이라 provisional 로 표시한다.
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

from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
from stock_mcp_server.market_data.provider_registry import registry
from stock_mcp_server.market_data.secrets import SecretPayload

_FIXTURES = Path(__file__).parent / "fixtures" / "kiwoom"
_DAILY = json.loads(
    (_FIXTURES / "evidence_investor_daily.json").read_text("utf-8"))
_TOKEN = json.loads((_FIXTURES / "token_success.json").read_text("utf-8"))


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": "k", "secret_key": "s"})


class Server:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOKEN)
        self.requests.append(request)
        payload, cont_yn, next_key = self.pages.pop(0)
        headers = {}
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        return httpx.Response(200, json=payload, headers=headers)


def _provider(server: Server):
    from stock_mcp_server.market_data.kiwoom_evidence import (
        KiwoomEvidenceProvider,
    )

    client = KiwoomClient(
        _payload(), "real", transport=httpx.MockTransport(server.handler))
    return KiwoomEvidenceProvider(client, "real")


def _run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    """endpoint 는 레지스트리 허용 목록에만 존재한다 (호출자가 URL 을 만들 수 없다)."""

    def test_evidence_endpoint_is_registered(self):
        descriptor = registry.require("kiwoom")
        spec = descriptor.endpoint("kr_investor_daily")
        self.assertIsNotNone(spec, "ka10059 endpoint 미등록")
        self.assertEqual(spec.method, "POST")
        self.assertEqual(spec.path, "/api/dostk/stkinfo")
        self.assertIn(spec.path, descriptor.allowed_paths)

    def test_no_account_or_order_paths_anywhere(self):
        for provider_id in registry.ids():
            for path in registry.require(provider_id).allowed_paths:
                low = path.lower()
                for banned in ("account", "order", "balance", "ccnl",
                               "psbl", "deposit", "acnt"):
                    self.assertNotIn(
                        banned, low,
                        f"{provider_id} 에 계좌·주문 계열 경로: {path}")


class InvestorFlowMappingTests(unittest.TestCase):
    def setUp(self):
        self.server = Server([(_DAILY, None, None)])
        self.result = _run(_provider(self.server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), max_pages=1))

    def test_request_uses_registered_endpoint_and_tr(self):
        req = self.server.requests[0]
        self.assertEqual(req.url.path, "/api/dostk/stkinfo")
        self.assertEqual(req.headers["api-id"], "ka10059")

    def test_default_measure_is_shares_and_labelled(self):
        # 실측: amt_qty_tp=2 + unit_tp=1 이 단주 수량이고, 그 값이 KIS
        # prsn_ntby_qty 와 자릿수까지 일치한다. 라벨이 요청과 같은 표에서
        # 나오므로 값과 이름표가 갈라질 수 없다.
        body = json.loads(self.server.requests[0].content)
        self.assertEqual(body["amt_qty_tp"], "2")
        self.assertEqual(body["unit_tp"], "1")
        self.assertEqual(self.result.measure, "net_quantity")
        self.assertEqual(self.result.unit, "shares")

    def test_amount_measure_switches_request_and_label_together(self):
        server = Server([(_DAILY, None, None)])
        result = _run(_provider(server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), measure="net_amount"))
        body = json.loads(server.requests[0].content)
        self.assertEqual(body["amt_qty_tp"], "1")
        self.assertEqual(result.measure, "net_amount")
        self.assertEqual(result.unit, "KRW_million")

    def test_unknown_measure_is_rejected(self):
        with self.assertRaises(ValueError):
            _run(_provider(Server([(_DAILY, None, None)]))
                 .fetch_investor_flow("005930",
                                      base_date=date(2026, 8, 28),
                                      measure="net_dollars"))

    def test_categories_keep_their_raw_names(self):
        row = self.result.rows[1]  # 20260827 (정산 완료)
        self.assertEqual(row.raw_category("private_equity_fund"),
                         "samo_fund")
        self.assertEqual(row.raw_category("government"), "natn")
        self.assertEqual(row.raw_category("investment_trust"), "invtrt")

    def test_settled_day_is_final_and_balances(self):
        row = self.result.rows[1]  # 20260827
        self.assertEqual(row.date, date(2026, 8, 27))
        self.assertEqual(row.data_state, "final")
        self.assertTrue(row.balance_ok)
        # 수량(단주). KIS 교차 확인: prsn_ntby_qty 도 -3,223,427.
        self.assertEqual(row.value("individual"), -3223427)
        self.assertEqual(row.value("institution_total"), -97433)
        # 정산일에는 5주체 순매매 합이 정확히 0 이다.
        self.assertEqual(row.principal_sum, 0)
        # 기관 세부 8종 합 == 기관계
        self.assertEqual(row.institution_subtotal(),
                         row.value("institution_total"))

    def test_unsettled_day_is_provisional_not_zero(self):
        row = self.result.rows[0]  # 20260828 (당일, 정산 전)
        self.assertEqual(row.date, date(2026, 8, 28))
        self.assertEqual(row.data_state, "provisional")
        self.assertFalse(row.balance_ok)
        # 0 을 확정 수치로 내보내지 않는다 - 미정산은 값이 아니라 상태다.
        self.assertIsNone(row.value("individual"))
        self.assertTrue(row.is_unsettled("individual"))
        # 실제로 값이 있는 항목은 그대로 준다.
        self.assertEqual(row.value("institution_total"), -902000)

    def test_dataset_warns_about_the_provisional_day(self):
        joined = " ".join(self.result.warnings)
        self.assertIn("정산", joined)
        self.assertEqual(self.result.data_state, "provisional")

    def test_no_silent_correction(self):
        # 어댑터는 합을 맞추려고 값을 만들거나 고치지 않는다.
        row = self.result.rows[0]
        self.assertEqual(row.value("institution_total"), -902000)
        self.assertEqual(row.principal_sum, 842000)


class PaginationTests(unittest.TestCase):
    def test_continues_while_cont_yn_is_y(self):
        page2 = {"return_code": 0, "return_msg": "정상",
                 "stk_invsr_orgn": [dict(_DAILY["stk_invsr_orgn"][4],
                                         dt="20260821")]}
        server = Server([(_DAILY, "Y", "KEY1"), (page2, None, None)])
        result = _run(_provider(server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), max_pages=3))
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(server.requests[1].headers["cont-yn"], "Y")
        self.assertEqual(server.requests[1].headers["next-key"], "KEY1")
        self.assertEqual(result.coverage["pages"], 2)

    def test_page_budget_marks_incomplete(self):
        server = Server([(_DAILY, "Y", "KEY1")])
        result = _run(_provider(server).fetch_investor_flow(
            "005930", base_date=date(2026, 8, 28), max_pages=1))
        self.assertFalse(result.coverage["complete"])


class ErrorTests(unittest.TestCase):
    def test_unknown_symbol_is_entity_not_found(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError

        blank = {"return_code": 0, "return_msg": "정상",
                 "stk_invsr_orgn": []}
        server = Server([(blank, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_investor_flow(
                "ZZZZZZ", base_date=date(2026, 8, 28)))
        self.assertEqual(ctx.exception.provider_status, "entity_not_found")

    def test_provider_error_code_is_classified(self):
        from stock_mcp_server.market_data.kiwoom_client import KiwoomApiError

        bad = {"return_code": 2, "return_msg": "입력 값 오류입니다[1511:...]"}
        server = Server([(bad, None, None)])
        with self.assertRaises(KiwoomApiError) as ctx:
            _run(_provider(server).fetch_investor_flow(
                "005930", base_date=date(2026, 8, 28)))
        self.assertEqual(ctx.exception.provider_status,
                         "provider_unavailable")


if __name__ == "__main__":
    unittest.main()
