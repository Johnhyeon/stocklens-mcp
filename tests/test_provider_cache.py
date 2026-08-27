"""공급자별 분봉 캐시 테스트 (Task 10).

- provider/profile/market/session/interval/date 네임스페이스 분리
- 비밀값이 key·경로에 없음, 경로 탈출 불가
- 불완전 응답을 완전 캐시로 승격하지 않음
- 미연결 프로필은 남은 캐시로 결과를 만들 수 없음
- 총량 제한과 결정적 LRU 정리
- 손상 entry 개별 무시·제거
- 현재 프로필 해제는 과거 캐시 유지, 공급자 전체 해제는 KIS 캐시 전체 삭제
- Naver·Yahoo 캐시 무접촉
- generation 변경 시 프로세스 메모리 캐시 폐기
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import broker_cli
from stock_mcp_server.market_data.broker_profiles import (
    BrokerCredentials,
    BrokerProfileStore,
)
from stock_mcp_server.market_data.provider_cache import (
    MemoryBarCache,
    ProviderCache,
)

SENTINEL_KEY = "PSA-SENTINEL-APP-KEY-333"


class FakeKeyring:
    def __init__(self):
        self.entries = {}

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise Exception("not found")
        del self.entries[(service, username)]


def _key(**overrides) -> dict:
    base = dict(
        provider="kis", profile="real", market="KR", symbol="005930",
        venue="KRX", session="regular", source_interval="1m",
        trading_date="20260827", cursor="", adjustment="unadjusted",
    )
    base.update(overrides)
    return base


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cache = ProviderCache(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_namespacing(self):
        self.cache.put(_key(), {"rows": [1, 2, 3]}, complete=True)
        entry = self.cache.get(_key(), connected=True)
        self.assertEqual(entry["payload"], {"rows": [1, 2, 3]})
        self.assertTrue(entry["complete"])

        # 다른 프로필·간격·날짜는 서로 보이지 않는다.
        self.assertIsNone(self.cache.get(_key(profile="demo"), connected=True))
        self.assertIsNone(
            self.cache.get(_key(source_interval="5m"), connected=True))
        self.assertIsNone(
            self.cache.get(_key(trading_date="20260826"), connected=True))

        # 디렉터리 구조가 provider/profile 로 나뉜다.
        root = self.home / "cache" / "market_data" / "kis" / "real"
        self.assertTrue(root.exists())
        self.assertTrue(any(root.rglob("*.json")))

    def test_no_secret_in_path(self):
        self.cache.put(_key(), {"rows": []}, complete=True)
        for path in (self.home / "cache").rglob("*"):
            self.assertNotIn(SENTINEL_KEY, str(path))
            self.assertNotIn("app_key", str(path))

    def test_path_escape_rejected(self):
        for bad in ("../../evil", "..\\evil", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                self.cache.put(_key(symbol=bad), {"rows": []}, complete=True)

    def test_incomplete_never_promoted_to_complete(self):
        self.cache.put(_key(), {"rows": [1]}, complete=False)
        entry = self.cache.get(_key(), connected=True)
        self.assertFalse(entry["complete"])

        # 완전 entry 를 불완전 응답으로 덮어써도 완전 상태가 유지되면 안 되고,
        # 불완전 응답이 완전으로 승격되어도 안 된다.
        self.cache.put(_key(), {"rows": [1, 2]}, complete=True)
        self.cache.put(_key(), {"rows": [1]}, complete=False)
        entry = self.cache.get(_key(), connected=True)
        self.assertFalse(entry["complete"])

    def test_disconnected_profile_cannot_serve_cache(self):
        self.cache.put(_key(), {"rows": [1]}, complete=True)
        self.assertIsNone(self.cache.get(_key(), connected=False))

    def test_corrupt_entry_ignored_and_removed(self):
        self.cache.put(_key(), {"rows": [1]}, complete=True)
        path = next((self.home / "cache").rglob("*.json"))
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.cache.get(_key(), connected=True))
        self.assertFalse(path.exists())

    def test_incompatible_schema_version_ignored_and_removed(self):
        self.cache.put(_key(), {"rows": [1]}, complete=True)
        path = next((self.home / "cache").rglob("*.json"))
        doc = json.loads(path.read_text("utf-8"))
        doc["schema_version"] = 999
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIsNone(self.cache.get(_key(), connected=True))
        self.assertFalse(path.exists())

    def test_lru_eviction_is_bounded_and_deterministic(self):
        small = ProviderCache(home=self.home, max_total_bytes=2000)
        for i in range(20):
            small.put(_key(trading_date=f"202608{i:02d}"),
                      {"rows": ["x" * 50]}, complete=True)
        self.assertLessEqual(small.total_bytes(), 2000)
        # 가장 최근 entry 는 살아 있어야 한다.
        self.assertIsNotNone(
            small.get(_key(trading_date="20260819"), connected=True))

    def test_profile_disconnect_keeps_history_provider_disconnect_removes(self):
        self.cache.put(_key(profile="real"), {"rows": [1]}, complete=True)
        self.cache.put(_key(profile="demo"), {"rows": [2]}, complete=True)

        real_dir = self.home / "cache" / "market_data" / "kis" / "real"
        demo_dir = self.home / "cache" / "market_data" / "kis" / "demo"
        self.assertTrue(real_dir.exists())
        self.assertTrue(demo_dir.exists())

        # 공급자 전체 해제만 캐시를 지운다.
        self.cache.remove_provider("kis")
        self.assertFalse(real_dir.exists())
        self.assertFalse(demo_dir.exists())

    def test_remove_provider_does_not_touch_other_cache(self):
        other = self.home / "cache" / "naver_cache.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("{}", encoding="utf-8")
        self.cache.put(_key(), {"rows": [1]}, complete=True)
        self.cache.remove_provider("kis")
        self.assertTrue(other.exists())

    def test_remove_provider_validates_root(self):
        with self.assertRaises(ValueError):
            self.cache.remove_provider("../..")


class MemoryCacheTests(unittest.TestCase):
    def test_generation_change_clears_memory_cache(self):
        gen = [1]
        cache = MemoryBarCache(generation_provider=lambda: gen[0])
        cache.put("k1", {"rows": [1]})
        self.assertEqual(cache.get("k1"), {"rows": [1]})
        gen[0] = 2
        self.assertIsNone(cache.get("k1"))


class CliCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.keyring = FakeKeyring()
        self.store = BrokerProfileStore(
            provider="kis", keyring_module=self.keyring, home=self.home)
        self.cache = ProviderCache(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _handle(self, request):
        return broker_cli.handle_request(
            request, store=self.store, cache=self.cache)

    def test_disconnect_provider_removes_kis_cache(self):
        self.store.save_profile("real", BrokerCredentials(
            app_key="k", app_secret="s"))
        self.cache.put(_key(profile="real"), {"rows": [1]}, complete=True)
        self.cache.put(_key(profile="demo", market="US", venue="NAS",
                            symbol="AAPL"), {"rows": [2]}, complete=True)
        naver = self.home / "cache" / "naver_cache.json"
        naver.write_text("{}", encoding="utf-8")

        resp = self._handle({"contract_version": 1,
                             "action": "disconnect_provider",
                             "provider": "kis"})
        self.assertTrue(resp["ok"])
        kis_dir = self.home / "cache" / "market_data" / "kis"
        self.assertFalse(kis_dir.exists())
        self.assertTrue(naver.exists())

    def test_disconnect_profile_keeps_cache(self):
        self.store.save_profile("real", BrokerCredentials(
            app_key="k", app_secret="s"))
        self.cache.put(_key(profile="real"), {"rows": [1]}, complete=True)
        resp = self._handle({"contract_version": 1,
                             "action": "disconnect_profile",
                             "provider": "kis", "profile": "real"})
        self.assertTrue(resp["ok"])
        self.assertIsNotNone(self.cache.get(_key(profile="real"),
                                            connected=True))


if __name__ == "__main__":
    unittest.main()
