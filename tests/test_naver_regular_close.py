"""정규장 종가는 네이버 분봉의 15:30 봉에서만 읽는다 (2026-09-14 애프터마켓 이후)."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from stock_mcp_server import naver
from stock_mcp_server._cache import clear_cache

# 2026-09-14 005930 실측 모양. 15:20~15:29 는 종가 단일가 접수라 봉이 없다.
ROWS_0914 = [
    {"localDateTime": "20260914151900", "currentPrice": 249500.0},
    {"localDateTime": "20260914153000", "currentPrice": 249000.0,
     "accumulatedTradingVolume": 1199772},
]


class RegularCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_cache()

    def test_reads_the_1530_bar(self) -> None:
        with patch.object(naver, "_api_json", AsyncMock(return_value=ROWS_0914)) as api:
            self.assertEqual(asyncio.run(naver.get_regular_session_close("005930", "2026-09-14")), 249000)
        params = api.call_args.kwargs["params"]
        self.assertEqual(params, {"startDateTime": "202609141520", "endDateTime": "202609141530"})

    def test_missing_1530_bar_is_none_not_last_trade(self) -> None:
        # 277810 은 그 날 15:19 가 마지막 체결이었다. 15:19 가격을 종가로 쓰면 틀린다.
        with patch.object(naver, "_api_json", AsyncMock(return_value=ROWS_0914[:1])):
            self.assertIsNone(asyncio.run(naver.get_regular_session_close("277810", "20260914")))

    def test_bad_day_does_not_call_the_source(self) -> None:
        with patch.object(naver, "_api_json", AsyncMock()) as api:
            self.assertIsNone(asyncio.run(naver.get_regular_session_close("005930", "2026-9")))
        api.assert_not_called()

    def test_non_list_payload_is_a_parse_error(self) -> None:
        with patch.object(naver, "_api_json", AsyncMock(return_value={"error": "x"})):
            with self.assertRaises(naver.NaverParseError):
                asyncio.run(naver.get_regular_session_close("005930", "2026-09-14"))


if __name__ == "__main__":
    unittest.main()
