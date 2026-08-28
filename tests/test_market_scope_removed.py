"""시장 단위 투자자 수급은 제공하지 않는다 (1.1 Task 13 축소, 실측 근거).

계획에는 `market` 인자와 `market_investor_flow()` 가 있었다. 실측
(2026-08-28)으로 확인한 결과 **공급자가 그 데이터를 주지 않는다.**

키움 ka10063(장중 투자자별)·ka10066(장마감 투자자별)은 이름과 달리
시장 집계가 아니라 **종목별 행**을 돌려준다:

    {"stk_cd": "000020_AL", "stk_nm": "동화약품", "ind_invsr": "97", ...}

100~111행이 오고, `mrkt_tp` 를 0(전체)에서 001(코스피)로 바꿔도 첫 행이
같다. 시장 구분 필터로 동작하지 않는다는 뜻이다.

상위 100종목을 더해서 "시장 전체"라고 부를 수는 없다. 그건 일부
유니버스로 만든 숫자에 전체라는 이름을 붙이는 것이고, KIS 대차거래를
종목별 답으로 쓰지 않기로 한 것과 같은 이유로 하지 않는다.

그래서 요구사항에서 뺀다. 안 만든 것이 아니라 **줄 수 없다고 확인한
것**이고, 이 테스트가 그 판단을 기록한다. 나중에 공급자가 진짜 시장
집계를 열면 그때 근거를 새로 남기고 되살린다.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_service import EvidenceService
from stock_mcp_server.market_data.provider_registry import registry


class ScopeTests(unittest.TestCase):
    def test_no_market_argument_is_advertised(self):
        from stock_mcp_server.server import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        props = tools["get_detailed_investor_flow"].inputSchema["properties"]
        self.assertNotIn("market", props)

    def test_no_market_service_method_pretends_to_exist(self):
        self.assertFalse(hasattr(EvidenceService, "market_investor_flow"))

    def test_the_misleading_endpoint_id_is_gone(self):
        """`kr_investor_market` 은 실제로 종목별 순위였다.

        이름을 남겨 두면 다음 사람이 그 위에 시장 집계를 만들고,
        상위 100종목을 전체 시장이라고 부르게 된다.
        """
        kiwoom = registry.require("kiwoom")
        ids = {e.endpoint_id for e in kiwoom.endpoints}
        self.assertNotIn("kr_investor_market", ids)

    def test_the_guides_do_not_promise_market_level_flow(self):
        root = Path(__file__).resolve().parents[1]
        for lang in ("ko", "en"):
            text = (root / f"guides/{lang}/TOOLS.md").read_text("utf-8")
            self.assertNotIn("market_investor_flow", text, lang)


if __name__ == "__main__":
    unittest.main()
