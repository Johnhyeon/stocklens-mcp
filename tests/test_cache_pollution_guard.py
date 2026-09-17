"""실사용 캐시 오염 방지 (1.0 결함 수정, 2026-08-27 실측 발견).

테스트 스위트가 STOCKLENS_HOME 미설정 상태로 _fetch_intraday_dataset 을
돌리면 가짜 봉이 개발자 실사용 캐시(~/.stocklens/cache)에 '완전한 하루'
로 저장됐다 (kis/kiwoom/toss 8개 entry, 종가 100·1001 가짜 값 실확인).

방어 2겹:
1. conftest 가 모든 테스트에 tmp STOCKLENS_HOME 을 강제한다
2. 캐시 로더가 entry 의 봉 날짜를 키의 trading_date 와 대조한다
   (20260826 키에 08-27 봉이 든 오염 entry 는 복원 자체가 거부된다)
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from stock_mcp_server import server
from stock_mcp_server.market_data.models import bar_to_dict
from stock_mcp_server.market_data.provider_cache import ProviderCache


class HomeIsolationGuardTests(unittest.TestCase):
    def test_tests_never_see_the_real_user_home(self):
        home = os.environ.get("STOCKLENS_HOME")
        self.assertTrue(home, "conftest 가 STOCKLENS_HOME 을 강제해야 한다")
        real = Path.home() / ".stocklens"
        self.assertNotEqual(Path(home).resolve(), real.resolve())

    def test_intraday_disk_cache_points_at_isolated_home(self):
        cache = server._intraday_disk_cache()
        home = Path(os.environ["STOCKLENS_HOME"]).resolve()
        self.assertTrue(str(cache._root.resolve()).startswith(str(home)))


class UnisolatedRunnerGuardTests(unittest.TestCase):
    def test_unittest_runner_cannot_import_the_test_package(self):
        # 2026-09-17: unittest discover 로 돌려 conftest 격리가 빠졌고 실사용
        # broker_state.json·캐시가 덮였다. pytest 밖에서는 패키지가 멈춰야 한다.
        import subprocess

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env.pop("STOCKLENS_HOME", None)
        proc = subprocess.run(
            [sys.executable, "-c", "import tests"],
            cwd=root, env=env, capture_output=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"pytest", proc.stderr)


class CachedDayDateValidationTests(unittest.TestCase):
    def _bar(self, day: dt.date):
        from decimal import Decimal
        from zoneinfo import ZoneInfo
        from stock_mcp_server.market_data.models import NormalizedBar
        start = dt.datetime(day.year, day.month, day.day, 9, 0,
                            tzinfo=ZoneInfo("Asia/Seoul"))
        return NormalizedBar(
            start_at=start, end_at=start + dt.timedelta(minutes=1),
            open=Decimal(100), high=Decimal(101), low=Decimal(99),
            close=Decimal(100), volume=10, interval="1m",
            session="regular", complete=True, session_tail=False,
            expected_minutes=1, actual_minutes=1,
            data_integrity="complete", source_gap_status="none")

    def test_date_mismatched_entry_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            cache = ProviderCache(home=Path(home))
            key = server._broker_day_cache_key(
                "kis", "real", "KR", "005930", "KRX", "regular",
                dt.date(2026, 8, 26))
            # 오염 재현: 26일 키에 27일 봉을 넣는다.
            wrong_day_bar = self._bar(dt.date(2026, 8, 27))
            cache.put(key, {"bars": [bar_to_dict(wrong_day_bar)]},
                      complete=True)
            self.assertIsNone(
                server._load_cached_broker_day(cache, key),
                "키 날짜와 다른 봉이 든 entry 는 복원되면 안 된다")

    def test_matching_entry_still_loads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            cache = ProviderCache(home=Path(home))
            key = server._broker_day_cache_key(
                "kis", "real", "KR", "005930", "KRX", "regular",
                dt.date(2026, 8, 27))
            good = self._bar(dt.date(2026, 8, 27))
            cache.put(key, {"bars": [bar_to_dict(good)]}, complete=True)
            bars = server._load_cached_broker_day(cache, key)
            self.assertIsNotNone(bars)
            self.assertEqual(len(bars), 1)


if __name__ == "__main__":
    unittest.main()
