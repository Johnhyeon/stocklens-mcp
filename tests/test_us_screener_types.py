"""SL-12: 미국 스크리너의 증권 유형 필터.

실측(UAT): small_cap_gainers 에 GRABW(워런트)·KLXER(라이츠) 같은 파생 식별자가
보통주 후보와 섞여 들어왔다. 워런트도 Yahoo quoteType 은 EQUITY 라 유형만으로는
못 거른다 - 이름 키워드와 NASDAQ 접미사 규약을 함께 쓴다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import server


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
    return json.loads(payload.strip())


_US_CLOSED = {
    "krx": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
    "us": {"is_open": False, "status": "closed_after_hours", "last_trading_day": "2026-08-26"},
}


def _q(symbol, name=None, quote_type="EQUITY", exchange_code="NMS",
       exchange="NasdaqGS"):
    return {"symbol": symbol, "name": name, "price": 10.0, "change": 1.0,
            "change_percent": 5.0, "volume": 1_000_000, "market_cap": 500_000_000,
            "pe": None, "quote_type": quote_type, "exchange_code": exchange_code,
            "exchange": exchange}


class ClassifierTests(unittest.TestCase):
    def test_name_keywords_are_definitive(self):
        cls = server._classify_us_security
        self.assertEqual(cls(_q("GRABW", "Grab Holdings Limited Warrants")), "warrant")
        self.assertEqual(cls(_q("KLXER", "KLX Energy Services Holdings, Inc. Rights")), "right")
        self.assertEqual(cls(_q("ABCU", "Some Acquisition Corp. Units")), "unit")
        self.assertEqual(cls(_q("TSM", "Taiwan Semiconductor ADR")), "adr")
        self.assertEqual(
            cls(_q("SONY", "Sony Group American Depositary Shares")), "adr")

    def test_quote_type_maps_funds(self):
        cls = server._classify_us_security
        self.assertEqual(cls(_q("SPY", "SPDR S&P 500", quote_type="ETF")), "etf")
        self.assertEqual(cls(_q("VFIAX", "Vanguard", quote_type="MUTUALFUND")), "fund")

    def test_nasdaq_suffix_convention_when_name_is_silent(self):
        """이름이 말해주지 않아도 5글자 W/R/U 접미사는 파생으로 본다."""
        cls = server._classify_us_security
        self.assertEqual(cls(_q("GRABW", None)), "warrant")
        self.assertEqual(cls(_q("KLXER", None)), "right")
        self.assertEqual(cls(_q("ABCDU", None)), "unit")

    def test_plain_equity_is_common_stock(self):
        cls = server._classify_us_security
        self.assertEqual(cls(_q("AAPL", "Apple Inc.")), "common_stock")
        # 4글자 이하 W 로 끝나는 정상 보통주는 워런트가 아니다
        self.assertEqual(cls(_q("GLW", "Corning Incorporated")), "common_stock")

    def test_missing_type_is_unknown_not_promoted(self):
        """수용 2: 확인 불가는 unknown 으로 남는다. 보통주로 승격하지 않는다."""
        cls = server._classify_us_security
        self.assertEqual(cls(_q("XXXX", None, quote_type=None)), "unknown")


class ScreenerFilterTests(unittest.IsolatedAsyncioTestCase):
    QUOTES = [
        _q("AAPL", "Apple Inc."),
        _q("GRABW", "Grab Holdings Limited Warrants"),
        _q("KLXER", None),                                # 접미사로만 판정
        _q("SPY", "SPDR S&P 500 ETF Trust", quote_type="ETF"),
        _q("MYST", None, quote_type=None),                # unknown
    ]

    async def _run(self, **kw):
        data = {"preset": "small_cap_gainers", "title": "Small Cap Gainers",
                "description": None, "total": 120, "quotes": list(self.QUOTES)}
        opts = dict(preset="small_cap_gainers")
        opts.update(kw)
        with patch.object(server.us, "screen", AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", return_value=_US_CLOSED):
            return await server.get_us_screener(**opts)

    async def test_every_row_shows_its_security_type(self):
        """요구 1: security_type·거래소가 행마다 보인다."""
        text = await self._run()
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("warrant", body)
        self.assertIn("common_stock", body)
        self.assertIn("unknown", body)
        self.assertIn("NasdaqGS", body)
        meta = extract_meta(text)
        self.assertEqual(meta["security_types"]["warrant"], 1)
        self.assertEqual(meta["security_types"]["common_stock"], 1)
        self.assertEqual(meta["security_types"]["unknown"], 1)

    async def test_common_stock_only_excludes_derivatives(self):
        """수용 1: GRABW·KLXER 가 제외된다. unknown 도 승격되지 않는다."""
        text = await self._run(common_stock_only=True)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("AAPL", body)
        self.assertNotIn("GRABW", body)
        self.assertNotIn("KLXER", body)
        self.assertNotIn("SPY |", body)
        self.assertNotIn("MYST", body)
        meta = extract_meta(text)
        exc = meta["security_filter"]["excluded"]
        self.assertEqual(exc["warrant"], 1)
        self.assertEqual(exc["right"], 1)
        self.assertEqual(exc["etf"], 1)
        self.assertEqual(exc["unknown"], 1)
        self.assertIn("제외", body)

    async def test_filter_off_keeps_everything(self):
        text = await self._run(common_stock_only=False)
        body = text.split(rmeta.MARKER_START)[0]
        for sym in ("AAPL", "GRABW", "KLXER", "SPY", "MYST"):
            self.assertIn(sym, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
