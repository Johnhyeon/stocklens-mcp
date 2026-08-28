"""1.1 이 1.0 수급 도구를 건드리지 않았는지 (Task 14).

핵심은 **이름 충돌**이다. `stock_mcp_server/naver.py` 의
`get_investor_flow` 가 server.py 로 import 되어 기존 도구 여덟 곳에서
호출된다. 신규 MCP 도구를 같은 이름으로 만들었다면 `@mcp.tool()` 로
정의된 모듈 수준 함수가 그 import 를 **조용히 가렸을 것**이다.

가려졌을 때 나는 오류가 아니다. 기존 도구가 새 도구를 호출하면서
증권사 연결을 요구하기 시작하고, 연결 안 한 사용자에게는 멀쩡하던
`get_flow` 가 갑자기 실패한다. 그래서 신규 도구 이름을
`get_detailed_investor_flow` 로 지었고, 여기서 그 사실을 고정한다.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import naver, server

# 네이버 수급을 쓰는 1.0 도구들. 증권사 연결 없이 동작해야 한다.
LEGACY_FLOW_TOOLS = ("get_flow", "get_flow_batch", "screen_by_flow",
                     "get_event_reaction", "get_event_reactions")


class NameCollisionTests(unittest.TestCase):
    def test_the_naver_helper_is_not_shadowed_by_the_new_tool(self):
        # server.get_investor_flow 는 여전히 naver 의 그 함수여야 한다.
        self.assertIs(server.get_investor_flow, naver.get_investor_flow)

    def test_the_new_tool_has_a_different_name(self):
        self.assertTrue(hasattr(server, "get_detailed_investor_flow"))
        self.assertIsNot(server.get_detailed_investor_flow,
                         naver.get_investor_flow)

    def test_the_naver_helper_is_not_registered_as_a_tool(self):
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        self.assertNotIn("get_investor_flow", names)

    def test_every_legacy_call_site_still_resolves_to_naver(self):
        """호출부가 몇 군데인지까지 센다.

        나중에 누군가 한 곳만 새 도구로 바꾸면 그 도구만 증권사를
        요구하게 되고, 사용자는 어떤 도구가 왜 실패하는지 알 수 없다.
        """
        source = Path(server.__file__).read_text("utf-8")
        calls = source.count("await get_investor_flow(")
        self.assertGreaterEqual(calls, 8)
        self.assertNotIn("await get_detailed_investor_flow(", source)


class LegacySignatureTests(unittest.TestCase):
    """시그니처가 바뀌면 기존 사용자의 호출이 깨진다."""

    def _params(self, name):
        fn = getattr(server, name)
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        return inspect.signature(fn).parameters

    def test_get_flow_signature_is_unchanged(self):
        params = self._params("get_flow")
        self.assertEqual(list(params), ["code", "days"])
        self.assertEqual(params["days"].default, 20)

    def test_get_flow_batch_signature_is_unchanged(self):
        params = self._params("get_flow_batch")
        self.assertEqual(list(params), ["codes", "days", "summary"])
        self.assertEqual(params["days"].default, 5)
        self.assertIs(params["summary"].default, False)

    def test_screen_by_flow_signature_is_unchanged(self):
        params = self._params("screen_by_flow")
        self.assertEqual(
            list(params),
            ["top_n", "market", "foreign_days", "inst_days", "exclude_etf",
             "sort_by"])
        self.assertEqual(params["market"].default, "ALL")
        self.assertEqual(params["sort_by"].default, "trade_value")


class NoBrokerRequirementTests(unittest.TestCase):
    def test_legacy_tools_do_not_take_a_source_argument(self):
        """source 인자가 생기면 증권사 선택이 기존 도구로 새어 들어간다.

        1.0 계약: 기존 네이버·야후 경로는 증권사 연결과 무관하다.
        """
        for name in LEGACY_FLOW_TOOLS:
            fn = getattr(server, name)
            while hasattr(fn, "__wrapped__"):
                fn = fn.__wrapped__
            self.assertNotIn("source", inspect.signature(fn).parameters,
                             name)

    def test_legacy_tools_do_not_reach_the_evidence_service(self):
        source = Path(server.__file__).read_text("utf-8")
        # 증거 서비스는 신규 두 도구에서만 만들어진다.
        self.assertEqual(source.count("_evidence_service()"), 3)


class NewCategoriesStayOutOfLegacyTests(unittest.TestCase):
    def test_the_detailed_categories_are_not_in_the_naver_path(self):
        """키움 13종 구분이 기존 3종 응답에 새어 들어가면 안 된다."""
        source = Path(naver.__file__).read_text("utf-8")
        for name in ("private_equity_fund", "pension_fund",
                     "investment_trust", "other_financial"):
            self.assertNotIn(name, source, name)


if __name__ == "__main__":
    unittest.main()
