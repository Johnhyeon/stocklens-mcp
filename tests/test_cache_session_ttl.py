"""KRX 애프터마켓(2026-09-14~) 동안 시세 캐시가 '장마감' TTL 로 묶이지 않게 한다."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_mcp_server._cache import choose_ttl, is_market_open
from stock_mcp_server.market_clock import krx_seconds_to_next_session

KST = ZoneInfo("Asia/Seoul")


def at(*args):
    return datetime(*args, tzinfo=KST)


class KrxQuoteLivenessTests(unittest.TestCase):
    def test_after_market_counts_as_moving_quotes(self) -> None:
        self.assertTrue(is_market_open(at(2026, 9, 15, 17, 0)))
        self.assertTrue(is_market_open(at(2026, 9, 15, 10, 0)))

    def test_settle_window_right_after_regular_close(self) -> None:
        self.assertTrue(is_market_open(at(2026, 9, 15, 15, 35)))
        self.assertFalse(is_market_open(at(2026, 9, 15, 15, 45)))

    def test_closed_outside_sessions_weekends_holidays_and_before_start(self) -> None:
        self.assertFalse(is_market_open(at(2026, 9, 15, 20, 30)))
        self.assertFalse(is_market_open(at(2026, 9, 19, 17, 0)))   # 토요일
        self.assertFalse(is_market_open(at(2026, 9, 24, 10, 0)))   # 추석
        self.assertFalse(is_market_open(at(2026, 9, 11, 17, 0)))   # 애프터마켓 시행 전

    def test_seconds_to_next_session_today(self) -> None:
        self.assertEqual(krx_seconds_to_next_session(at(2026, 9, 15, 15, 45)), 900)
        self.assertEqual(krx_seconds_to_next_session(at(2026, 9, 15, 8, 55)), 300)
        self.assertIsNone(krx_seconds_to_next_session(at(2026, 9, 15, 20, 30)))
        self.assertIsNone(krx_seconds_to_next_session(at(2026, 9, 19, 8, 55)))


class ChooseTtlTests(unittest.TestCase):
    def test_open_uses_market_ttl(self) -> None:
        self.assertEqual(choose_ttl(30, 3600, True, 900), 30)

    def test_closed_ttl_is_cut_at_next_session(self) -> None:
        # 15:45 에 받은 시세를 16:45 까지 들고 있으면 애프터마켓 45분이 통째로 가려진다.
        self.assertEqual(choose_ttl(30, 3600, False, 900), 900)
        self.assertEqual(choose_ttl(30, 3600, False, None), 3600)
        self.assertEqual(choose_ttl(30, 3600, False, 0.2), 1.0)


if __name__ == "__main__":
    unittest.main()
