"""KRX 애프터마켓(2026-09-14~) 시세 스냅샷 도구의 세션 이름표.

2026-09-15 16:03 실측: 종목 상세 tradingSessionType 이 AFTER_MARKET 으로 바뀌고 현재가가
움직였다(036930 애프터마켓 194,000 / 정규장 종가 192,800). v1.0.1 은 그 값을
"현재가 · last_close · 장마감 최근 거래일 기준"으로 내보냈다.
"""

import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from stock_mcp_server import _result_meta as rmeta
from stock_mcp_server import naver, server
from stock_mcp_server._cache import clear_cache
from stock_mcp_server.market_clock import get_market_clock

KST = ZoneInfo("Asia/Seoul")


def _meta(text: str) -> dict:
    return json.loads(text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0])


def _clock(*args):
    if len(args) == 2:
        args = (2026, 9, 15, *args)
    return lambda: get_market_clock(datetime(*args, tzinfo=KST))


class NaverSessionMappingTests(unittest.TestCase):
    def test_only_observed_values_are_mapped(self) -> None:
        self.assertEqual(naver._price_session("REGULAR_MARKET"), "regular")
        self.assertEqual(naver._price_session("AFTER_MARKET"), "after_market")
        self.assertEqual(naver._price_session("regularMarket"), "regular")
        self.assertEqual(naver._price_session("afterMarket"), "after_market")
        self.assertIsNone(naver._price_session(None))
        # 아직 본 적 없는 값은 이름을 추측하지 않는다.
        self.assertEqual(naver._price_session("PRE_MARKET"), "unknown")


class CurrentPriceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_session_and_base_price(self) -> None:
        clear_cache()
        detail = {
            "itemcode": "036930", "itemname": "주성엔지니어링", "tradeTime": "20260915160218",
            "tradingSessionType": "AFTER_MARKET", "nowPrice": "194000", "prevChangePrice": "-12000",
            "stdPrice": "206000", "prevClosePrice": "202500", "openPrice": "206500",
            "highPrice": "211000", "lowPrice": "191800", "tradeVolume": "1410426",
            "manageStatusGb": "0", "marketAlertType": "00", "tradeStopYn": "N",
        }
        with patch.object(naver, "_stock_detail", AsyncMock(return_value=detail)), \
             patch.object(naver, "_api_json", AsyncMock(return_value={"isNxtYn": "N"})):
            data = await naver.get_current_price("036930")
        clear_cache()

        self.assertEqual(data["price_session"], "after_market")
        # prevClosePrice(202,500)는 전날 20:00 마지막가다. 전일대비 기준은 기준가다.
        self.assertEqual(data["base_price"], 206000)


class GetPriceAfterMarketTests(unittest.IsolatedAsyncioTestCase):
    DATA = {
        "code": "036930", "name": "주성엔지니어링", "quote_date": "2026-09-15",
        "status_flags": [], "price": 194000, "change": -12000, "open": 206500,
        "high": 211000, "low": 191800, "volume": 1410426,
        "price_session": "after_market", "base_price": 206000, "_parse_miss": [],
    }

    async def _run(self, data, hm, regular=192800):
        lookup = AsyncMock(return_value=regular)
        with patch.object(server, "get_current_price", AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", _clock(*hm)), \
             patch.object(server, "get_regular_session_close", lookup), \
             patch.object(server.wl, "codes", return_value=set()):
            text = await server.get_price(code=data["code"])
        return text, _meta(text), lookup

    async def test_after_market_price_is_labeled_and_regular_close_shown(self) -> None:
        text, meta, lookup = await self._run(self.DATA, (17, 0))

        self.assertIn("현재가 (KRX 애프터마켓): 194,000원", text)
        self.assertIn("기준가 대비: -12,000원 (기준가 206,000원)", text)
        self.assertIn("정규장 종가 (15:30): 192,800원 (기준가 대비 -13,200원, -6.41%)", text)
        self.assertIn("시가: 206,500원", text)
        self.assertIn("고가 (애프터마켓 포함): 211,000원", text)
        self.assertIn("거래량 (애프터마켓 포함): 1,410,426", text)
        self.assertEqual(meta["price_session"], "after_market")
        self.assertEqual(meta["data_basis"], "realtime")
        self.assertEqual(meta["base_price"], 206000)
        self.assertEqual(meta["regular_close_check"][0]["status"], "ok")
        lookup.assert_awaited_once_with("036930", "2026-09-15")

    async def test_after_20_is_final_but_still_after_market(self) -> None:
        _, meta, _ = await self._run(self.DATA, (20, 30))

        self.assertEqual(meta["data_basis"], "last_close")
        self.assertIn(rmeta.EXTENDED_SESSION_WARNING, meta["warnings"])

    async def test_missing_regular_close_is_named(self) -> None:
        text, meta, _ = await self._run(self.DATA, (17, 0), regular=None)

        self.assertIn("정규장 종가 (15:30): 확인 불가", text)
        self.assertEqual(meta["regular_close_check"][0]["status"], "no_1530_bar")

    async def test_regular_session_output_is_unchanged(self) -> None:
        data = dict(self.DATA, price_session="regular")
        text, meta, lookup = await self._run(data, (10, 0))

        self.assertIn("현재가 (KRX 정규장): 194,000원", text)
        self.assertIn("전일대비: -12,000원", text)
        self.assertNotIn("애프터마켓", text)
        self.assertEqual(meta["price_session"], "regular")
        self.assertNotIn(rmeta.EXTENDED_SESSION_WARNING, meta["warnings"])
        lookup.assert_not_awaited()


class ListSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_stocks_marks_each_row(self) -> None:
        rows = [
            {"code": "036930", "name": "주성엔지니어링", "price": 193800, "change": -12200,
             "change_rate": "-5.92%", "volume": 1, "price_session": "after_market"},
            {"code": "069500", "name": "KODEX 200", "price": 104275, "change": -1135,
             "change_rate": "-1.08%", "volume": 1, "price_session": "regular"},
        ]
        with patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", _clock(17, 0)):
            text = await server.get_multi_stocks(codes=["036930", "069500"])

        self.assertIn("| 거래량 | 세션", text)
        self.assertIn("-5.92% | 1 | 애프터마켓", text)
        self.assertIn("-1.08% | 1 | 정규장", text)
        self.assertIn("애프터마켓 1종목 · 정규장 1종목", text)
        self.assertEqual(_meta(text)["price_session"], "after_market")

    async def test_multi_stocks_regular_only_keeps_old_table(self) -> None:
        rows = [{"code": "005930", "name": "삼성전자", "price": 250000, "change": 1000,
                 "change_rate": "+0.40%", "volume": 1, "price_session": "regular"}]
        with patch.object(server, "naver_get_multi_stocks", AsyncMock(return_value=rows)), \
             patch.object(server, "build_market_clock", _clock(10, 0)):
            text = await server.get_multi_stocks(codes=["005930"])

        self.assertNotIn("세션", text.split(rmeta.MARKER_START)[0])
        self.assertEqual(_meta(text)["price_session"], "regular")

    async def test_change_ranking_names_after_market(self) -> None:
        ranks = [{"rank": 1, "code": "062970", "name": "한국첨단소재", "price": 3360,
                  "change_rate": "+29.98%", "volume": 1, "trade_value_est_krw": 3360,
                  "price_session": "after_market"}]
        with patch.object(server, "naver_get_change_ranking", AsyncMock(return_value=ranks)), \
             patch.object(server, "naver_get_alert_codes", AsyncMock(return_value={})), \
             patch.object(server.wl, "codes", return_value=set()), \
             patch.object(server, "build_market_clock", _clock(17, 0)):
            text = await server.get_change_ranking(direction="up", count=1)

        self.assertIn("※ 세션: 애프터마켓 1종목", text)
        self.assertEqual(_meta(text)["price_session"], "after_market")

    async def test_index_is_regular_session(self) -> None:
        data = [{"index": "KOSPI", "value": 6627.26, "change_value": -57.11, "change_rate": -0.85},
                {"index": "KOSDAQ", "value": 812.41, "change_value": 5.62, "change_rate": 0.70}]
        with patch.object(server, "get_market_index", AsyncMock(return_value=data)), \
             patch.object(server, "build_market_clock", _clock(17, 0)):
            meta = _meta(await server.get_index())

        self.assertEqual(meta["price_session"], "regular")
        self.assertEqual(meta["data_basis"], "last_close")
        self.assertTrue(any("애프터마켓 체결은 들어 있지 않습니다" in w for w in meta["warnings"]))


class UnlabeledListTests(unittest.IsolatedAsyncioTestCase):
    RESULT = {"theme_id": "426", "theme_name": "양자", "stocks": [
        {"code": "062970", "name": "한국첨단소재", "price": 3360, "change_rate": "+29.98%", "volume": 1}]}

    async def _theme(self, *clock_args):
        with patch.object(server, "naver_get_theme_stocks", AsyncMock(return_value=self.RESULT)), \
             patch.object(server, "build_market_clock", _clock(*clock_args)):
            return await server.get_theme_stocks(theme_name="양자", include_reason=False)

    async def test_warns_when_last_trade_may_be_after_market(self) -> None:
        for args in ((17, 0), (20, 30), (2026, 9, 16, 8, 0)):
            text = await self._theme(*args)
            self.assertIn("어느 세션 체결인지 표시하지", text, args)
            self.assertEqual(_meta(text)["price_session"], "unknown", args)

    async def test_silent_during_regular_and_before_after_market_opens(self) -> None:
        for args in ((10, 0), (15, 45), (2026, 9, 11, 17, 0)):
            text = await self._theme(*args)
            self.assertNotIn("어느 세션 체결인지 표시하지", text, args)
            self.assertNotIn("price_session", _meta(text), args)


if __name__ == "__main__":
    unittest.main()
