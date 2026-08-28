"""키움 수급 압력 증거 (1.1, 2026-08-28 실계좌 실측 기반).

실측으로 확정한 TR 과 경로:
- 프로그램매매(종목별) ka90013 POST /api/dostk/mrkcond -> stk_daly_prm_trde_trnsn
- 공매도추이       ka10014 POST /api/dostk/shsa     -> shrts_trnsn
- 신용매매동향     ka10013 POST /api/dostk/stkinfo  -> crd_trde_trend
- 대차거래추이     ka20068 POST /api/dostk/slb      -> dbrt_trde_trnsn
- 외국인 매매동향  ka10008 POST /api/dostk/frgnistt -> stk_frgnr
- CFD 잔고: 키움 REST 조회 TR 이 없다 -> unsupported 를 그대로 돌려준다.

계약 원칙: 종류별 블록을 분리해 각각 독립적으로 상태를 갖는다. 서로 다른
종류를 하나의 점수나 숫자로 합치지 않는다. 지원하지 않는 종류를 빈 성공으로
처리하지 않는다.
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

_F = Path(__file__).parent / "fixtures" / "kiwoom"
_TOKEN = json.loads((_F / "token_success.json").read_text("utf-8"))


def _fx(name: str) -> dict:
    return json.loads((_F / name).read_text("utf-8"))


_BY_TR = {
    "ka90013": _fx("evidence_program.json"),
    "ka10014": _fx("evidence_short_selling.json"),
    "ka10013": _fx("evidence_credit.json"),
    "ka20068": _fx("evidence_lending.json"),
    "ka10008": _fx("evidence_foreign_holding.json"),
}


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kiwoom").credential_schema,
        {"app_key": "k", "secret_key": "s"})


class Server:
    """api-id 헤더로 어떤 TR 을 물었는지 보고 해당 픽스처를 돌려준다."""

    def __init__(self, overrides=None):
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json=_TOKEN)
        self.requests.append(request)
        tr = request.headers["api-id"]
        if tr in self.overrides:
            return self.overrides[tr]
        return httpx.Response(200, json=_BY_TR[tr])


def _provider(server: Server):
    from stock_mcp_server.market_data.kiwoom_evidence import (
        KiwoomEvidenceProvider,
    )

    client = KiwoomClient(
        _payload(), "real", transport=httpx.MockTransport(server.handler))
    return KiwoomEvidenceProvider(client, "real")


def _run(coro):
    return asyncio.run(coro)


ALL_KINDS = ("program_trading", "short_selling", "credit",
             "securities_lending", "foreign_holding", "cfd")


class RegistryTests(unittest.TestCase):
    def test_all_pressure_endpoints_are_registered(self):
        d = registry.require("kiwoom")
        expected = {
            "kr_program_trade": "/api/dostk/mrkcond",
            "kr_short_selling": "/api/dostk/shsa",
            "kr_credit_trade": "/api/dostk/stkinfo",
            "kr_securities_lending": "/api/dostk/slb",
            "kr_foreign_holding": "/api/dostk/frgnistt",
        }
        for endpoint_id, path in expected.items():
            spec = d.endpoint(endpoint_id)
            self.assertIsNotNone(spec, f"{endpoint_id} 미등록")
            self.assertEqual(spec.path, path)
            self.assertIn(path, d.allowed_paths)


class BlockSeparationTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.blocks = _run(_provider(self.server).fetch_supply_pressure(
            "005930", kinds=ALL_KINDS, base_date=date(2026, 8, 28)))

    def test_every_requested_kind_gets_its_own_block(self):
        self.assertEqual(set(self.blocks), set(ALL_KINDS))

    def test_each_block_carries_its_own_state(self):
        for kind, block in self.blocks.items():
            self.assertEqual(block.kind, kind)
            self.assertEqual(block.provider, "kiwoom")
            self.assertEqual(block.market, "KR")
            self.assertIn(block.status, ("ok", "unsupported"))
            self.assertIn(block.data_completeness,
                          ("complete", "partial", "none"))
            self.assertIsInstance(block.warnings, tuple)

    def test_unsupported_kind_is_not_an_empty_success(self):
        cfd = self.blocks["cfd"]
        self.assertEqual(cfd.status, "unsupported")
        self.assertEqual(cfd.data_completeness, "none")
        self.assertEqual(cfd.rows, ())
        self.assertEqual(cfd.unavailable_reason,
                         "not_provided_by_provider")
        # 미지원 종류를 물었다고 공급자를 부르지 않는다.
        self.assertNotIn("cfd", [r.headers["api-id"]
                                 for r in self.server.requests])

    def test_no_cross_kind_aggregate_is_produced(self):
        for block in self.blocks.values():
            for name in ("score", "total_score", "pressure_score",
                         "combined"):
                self.assertFalse(hasattr(block, name),
                                 f"{name} 같은 합산 값을 만들면 안 된다")


class MeasureMappingTests(unittest.TestCase):
    def setUp(self):
        self.blocks = _run(_provider(Server()).fetch_supply_pressure(
            "005930", kinds=ALL_KINDS, base_date=date(2026, 8, 28)))

    def test_short_selling_measures_keep_raw_names(self):
        row = self.blocks["short_selling"].rows[0]
        self.assertEqual(row.date, date(2026, 8, 27))
        self.assertEqual(row.value("short_volume"), 914065)
        self.assertEqual(row.raw_field("short_volume"), "shrts_qty")
        self.assertEqual(row.raw_field("trade_weight"), "trde_wght")

    def test_credit_measures(self):
        row = self.blocks["credit"].rows[0]
        self.assertEqual(row.value("new"), 2033829)
        self.assertEqual(row.value("repaid"), 1667887)
        self.assertEqual(row.raw_field("balance"), "remn")

    def test_securities_lending_measures(self):
        row = self.blocks["securities_lending"].rows[0]
        self.assertEqual(row.value("contracted"), 983500)
        self.assertEqual(row.value("repaid"), 1378602)
        self.assertEqual(row.value("balance"), 86161430)
        self.assertEqual(row.raw_field("balance"), "rmnd")

    def test_foreign_holding_measures(self):
        row = self.blocks["foreign_holding"].rows[0]
        self.assertEqual(row.value("holding_qty"), 2733143049)
        self.assertEqual(row.raw_field("holding_qty"), "poss_stkcnt")

    def test_program_trading_double_minus_sign_is_normalized(self):
        # 실측: 순매수 음수가 '--557431' 로 온다 (이중 마이너스).
        # 부호 문자를 값의 일부로 읽으면 부호가 뒤집힌다.
        row = self.blocks["program_trading"].rows[0]
        self.assertEqual(row.raw_field("net_amount"), "prm_netprps_amt")
        self.assertEqual(row.value("net_amount"), -557431)

    def test_program_trading_net_matches_buy_minus_sell(self):
        # 독립 검산: 순매수 = 매수 - 매도. 파싱이 틀리면 여기서 깨진다.
        for row in self.blocks["program_trading"].rows:
            buy, sell = row.value("buy_amount"), row.value("sell_amount")
            net = row.value("net_amount")
            if None in (buy, sell, net):
                continue
            self.assertEqual(net, buy - sell,
                             f"{row.date} 순매수 검산 불일치")

    def test_each_block_reports_its_own_data_as_of(self):
        # 종류마다 최신일이 다르다 (공매도·신용·대차는 T+1 확정).
        self.assertEqual(self.blocks["short_selling"].data_as_of,
                         date(2026, 8, 27))
        self.assertEqual(self.blocks["foreign_holding"].data_as_of,
                         date(2026, 8, 28))


class PartialFailureTests(unittest.TestCase):
    def test_one_failing_kind_does_not_discard_the_others(self):
        server = Server(overrides={
            "ka10014": httpx.Response(200, json={
                "return_code": 2, "return_msg": "입력 값 오류입니다"}),
        })
        blocks = _run(_provider(server).fetch_supply_pressure(
            "005930", kinds=("short_selling", "credit"),
            base_date=date(2026, 8, 28)))
        self.assertEqual(blocks["short_selling"].status,
                         "provider_unavailable")
        self.assertEqual(blocks["short_selling"].rows, ())
        # 실패한 종류의 값을 추정하거나 만들지 않는다.
        self.assertEqual(blocks["short_selling"].data_completeness, "none")
        # 확인된 다른 증거는 그대로 살아 있다.
        self.assertEqual(blocks["credit"].status, "ok")
        self.assertTrue(blocks["credit"].rows)

    def test_unknown_kind_is_rejected_not_guessed(self):
        with self.assertRaises(ValueError):
            _run(_provider(Server()).fetch_supply_pressure(
                "005930", kinds=("teleport",),
                base_date=date(2026, 8, 28)))


if __name__ == "__main__":
    unittest.main()
