"""KIS 수급 압력 (1.1, 2026-08-28 실계좌 실측 기반).

실측 결과가 키움과 상당히 다르다. 같은 이름의 종류라도 **모양과 범위가
달라서** 그대로 섞으면 안 된다:

| 종류 | 키움 | KIS |
|---|---|---|
| 공매도 | 일별 종목별 | 일별 종목별 (available) |
| 프로그램매매 | **일별** | **장중 시계열** (bsop_hour) |
| 대차거래 | 종목별 (ka20068) | **시장 전체만** - 종목별 없음 |
| 신용 | 종목별 (ka10013) | 파라미터를 다 채워도 빈 결과 (unverified) |
| 외국인 보유 | 보유주수·한도소진율 (ka10008) | 종목별 조회 없음 |
| CFD | 없음 | 없음 |

그래서 KIS 블록은 granularity 를 함께 싣고, 못 주는 것은 사유와 함께
`unsupported` 나 `unverified` 로 신고한다. 시장 전체 값을 종목별 요청의
답으로 돌려주지 않는다 (라벨-값 계약).
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
_SS = json.loads((_F / "evidence_short_selling.json").read_text("utf-8"))
_PG = json.loads((_F / "evidence_program.json").read_text("utf-8"))
_TOKEN = {"access_token": "T", "expires_in": 86400}

ALL_KINDS = ("program_trading", "short_selling", "credit",
             "securities_lending", "foreign_holding", "cfd")


def _payload() -> SecretPayload:
    return SecretPayload.from_schema(
        registry.require("kis").credential_schema,
        {"app_key": "k", "app_secret": "s"})


class Server:
    def __init__(self, overrides=None):
        self.requests: list[httpx.Request] = []
        self.overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/tokenP"):
            return httpx.Response(200, json=_TOKEN)
        self.requests.append(request)
        tr = request.headers["tr_id"]
        if tr in self.overrides:
            return self.overrides[tr]
        if tr == "FHPST04830000":
            return httpx.Response(200, json=_SS)
        if tr == "FHPPG04650100":
            return httpx.Response(200, json=_PG)
        return httpx.Response(200, json={"rt_cd": "0", "output": []})


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
    def test_measured_endpoints_are_registered(self):
        d = registry.require("kis")
        for endpoint_id, tail in (
            ("kr_short_selling", "/daily-short-sale"),
            ("kr_program_trade", "/program-trade-by-stock"),
        ):
            spec = d.endpoint(endpoint_id)
            self.assertIsNotNone(spec, endpoint_id)
            self.assertTrue(spec.path.endswith(tail), spec.path)
            self.assertIn(spec.path, d.allowed_paths)

    def test_unmeasured_endpoints_are_not_registered(self):
        # 데이터를 확인하지 못한 endpoint 는 허용 목록에 올리지 않는다.
        d = registry.require("kis")
        for path in d.allowed_paths:
            self.assertNotIn("daily-loan-trans", path)
            self.assertNotIn("daily-credit-balance", path)


class CapabilityTests(unittest.TestCase):
    def test_kis_reports_what_it_actually_has(self):
        from stock_mcp_server.market_data.kis_evidence import (
            KisEvidenceProvider,
        )

        caps = KisEvidenceProvider.pressure_capabilities()
        self.assertEqual(caps["short_selling"], "available")
        self.assertEqual(caps["program_trading"], "available")
        # 시장 전체 값을 종목별 답으로 쓰지 않는다.
        self.assertEqual(caps["securities_lending"], "unsupported")
        self.assertEqual(caps["foreign_holding"], "unsupported")
        self.assertEqual(caps["cfd"], "unsupported")
        # 응답은 오는데 데이터를 확인하지 못했다. 된다고도 안 된다고도
        # 하지 않는다.
        self.assertEqual(caps["credit"], "unverified")

    def test_kiwoom_covers_what_kis_cannot(self):
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )

        caps = KiwoomEvidenceProvider.pressure_capabilities()
        self.assertEqual(caps["securities_lending"], "available")
        self.assertEqual(caps["foreign_holding"], "available")
        self.assertEqual(caps["credit"], "available")
        self.assertEqual(caps["cfd"], "unsupported")


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.blocks = _run(_provider(self.server).fetch_supply_pressure(
            "005930", kinds=ALL_KINDS, base_date=date(2026, 8, 28)))

    def test_every_kind_gets_a_block_with_its_own_state(self):
        self.assertEqual(set(self.blocks), set(ALL_KINDS))
        for kind, block in self.blocks.items():
            self.assertEqual(block.provider, "kis")
            self.assertIn(block.status,
                          ("ok", "unsupported", "unverified"))

    def test_short_selling_is_a_daily_series(self):
        block = self.blocks["short_selling"]
        self.assertEqual(block.status, "ok")
        self.assertEqual(block.granularity, "daily")
        row = block.rows[0]
        self.assertEqual(row.date, date(2026, 8, 28))
        self.assertEqual(row.raw_field("short_volume"), "ssts_cntg_qty")
        self.assertIsNotNone(row.value("short_ratio"))

    def test_program_trading_is_labelled_intraday_not_daily(self):
        # 키움은 일별이다. 같은 이름으로 다른 모양을 주면서 말하지 않으면
        # 사용자가 두 숫자를 같은 것으로 읽는다.
        block = self.blocks["program_trading"]
        self.assertEqual(block.status, "ok")
        self.assertEqual(block.granularity, "intraday")
        self.assertTrue(block.rows)
        self.assertIsNotNone(block.rows[0].value("net_qty"))

    def test_unsupported_kinds_state_a_reason_and_skip_the_network(self):
        for kind, reason in (
            ("securities_lending", "market_level_only"),
            ("foreign_holding", "not_provided_by_provider"),
            ("cfd", "not_provided_by_provider"),
        ):
            block = self.blocks[kind]
            self.assertEqual(block.status, "unsupported", kind)
            self.assertEqual(block.unavailable_reason, reason, kind)
            self.assertEqual(block.rows, ())

    def test_unverified_kind_is_not_claimed_either_way(self):
        block = self.blocks["credit"]
        self.assertEqual(block.status, "unverified")
        self.assertEqual(block.rows, ())
        self.assertEqual(block.data_completeness, "none")
        self.assertIn("확인", " ".join(block.warnings))

    def test_no_combined_score(self):
        for block in self.blocks.values():
            for name in ("score", "pressure_score", "combined"):
                self.assertFalse(hasattr(block, name))


class PartialFailureTests(unittest.TestCase):
    def test_one_failing_kind_keeps_the_others(self):
        server = Server(overrides={
            "FHPST04830000": httpx.Response(200, json={"rt_cd": "1",
                                                       "msg1": "오류"})})
        blocks = _run(_provider(server).fetch_supply_pressure(
            "005930", kinds=("short_selling", "program_trading"),
            base_date=date(2026, 8, 28)))
        self.assertEqual(blocks["short_selling"].status,
                         "provider_unavailable")
        self.assertEqual(blocks["short_selling"].rows, ())
        self.assertEqual(blocks["program_trading"].status, "ok")
        self.assertTrue(blocks["program_trading"].rows)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            _run(_provider(Server()).fetch_supply_pressure(
                "005930", kinds=("teleport",),
                base_date=date(2026, 8, 28)))


if __name__ == "__main__":
    unittest.main()
