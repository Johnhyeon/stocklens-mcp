"""상세 수급 캐시 (1.1 Task 6).

캐시가 단순 최적화가 아닌 이유 셋:

1. **API 제한.** KIS 는 토큰 발급이 1분 1회고, 조회에도 초당 한도가 있다.
   같은 종목을 다시 물을 때마다 증권사를 다시 부르면 배치가 한도에 닿는다.
2. **잠정값 교체.** 정산 전 값은 나중에 확정치로 바뀐다. 잠정값을 캐시에
   남겨 두면 확정된 뒤에도 옛 숫자를 계속 돌려준다. 그래서 이 캐시는
   **확정 행만 저장한다.** 잠정 행은 아예 담지 않는다.
3. **연결 해제 시 정리.** 사용자가 증권사를 끊으면 그 증권사에서 받은
   데이터도 남기지 않는다. 분봉 캐시와 **따로** 보고한다 - 하나가
   실패했는데 둘 다 지워졌다고 말하면 안 된다.

경로에 비밀값이나 능력 이름을 그대로 넣지 않는다. 능력 이름은 점과
슬래시를 담을 수 있어서 경로 조각이 되면 안 된다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.evidence_cache import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceCache,
)

CAPABILITY = "kr.short_selling.daily"
# 확정 행 하나. 캐시는 final 만 담으므로 픽스처도 final 이어야 한다.
FINAL_ROW = {"date": "2026-08-27", "data_state": "final",
             "values": {"foreign": 1381786}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cache = EvidenceCache(home=self.home)
        self.addCleanup(self._tmp.cleanup)

    def _key(self, symbol="005930", provider="kiwoom", capability=CAPABILITY):
        return self.cache.key(
            provider=provider, profile="real",
            schema_version=EVIDENCE_SCHEMA_VERSION,
            capability=capability, symbol=symbol)


class KeyTests(_Base):
    def test_key_carries_provider_profile_and_schema(self):
        key = self._key()
        self.assertIn("kiwoom", key.parts)
        self.assertIn("real", key.parts)
        self.assertEqual(key.schema_version, EVIDENCE_SCHEMA_VERSION)

    def test_the_capability_name_is_never_a_path_component(self):
        # 능력 이름에는 점이 들어간다. 경로 조각으로 쓰면 디렉터리가
        # 쪼개지고, 앞으로 슬래시가 들어오면 경로를 벗어난다.
        key = self._key()
        self.assertNotIn(CAPABILITY, key.parts)
        for part in key.parts:
            self.assertNotIn(".", part.rstrip(".json"), part)

    def test_different_capabilities_do_not_collide(self):
        a = self._key(capability="kr.short_selling.daily")
        b = self._key(capability="kr.credit.daily")
        self.assertNotEqual(a.parts, b.parts)

    def test_different_providers_do_not_share_an_entry(self):
        a = self._key(provider="kis")
        b = self._key(provider="kiwoom")
        self.assertNotEqual(a.parts, b.parts)

    def test_a_key_field_cannot_escape_the_root(self):
        for bad in ("../../etc", "a/b", "..\\x"):
            with self.assertRaises(ValueError):
                self.cache.key(
                    provider="kiwoom", profile="real",
                    schema_version=EVIDENCE_SCHEMA_VERSION,
                    capability=CAPABILITY, symbol=bad)


class RoundTripTests(_Base):
    def test_a_stored_entry_comes_back(self):
        key = self._key()
        self.cache.put(key, {"rows": [FINAL_ROW]}, generation=3,
                       final_through="2026-08-27")
        got = self.cache.get(key, connected=True, generation=3)
        self.assertIsNotNone(got)
        self.assertEqual(got["payload"]["rows"][0]["values"],
                         {"foreign": 1381786})
        self.assertEqual(got["final_through"], "2026-08-27")

    def test_a_disconnected_provider_gets_no_cache_hit(self):
        """캐시가 연결을 대신하지 않는다. 분봉 캐시와 같은 계약이다."""
        key = self._key()
        self.cache.put(key, {"rows": [FINAL_ROW]}, generation=3,
                       final_through="2026-08-27")
        self.assertIsNone(self.cache.get(key, connected=False, generation=3))

    def test_a_generation_change_invalidates(self):
        # 사용자가 키를 바꿨다. 옛 연결로 받은 값을 계속 쓰지 않는다.
        key = self._key()
        self.cache.put(key, {"rows": [FINAL_ROW]}, generation=3,
                       final_through="2026-08-27")
        self.assertIsNone(self.cache.get(key, connected=True, generation=4))

    def test_a_schema_change_invalidates(self):
        key = self._key()
        self.cache.put(key, {"rows": [FINAL_ROW]}, generation=3,
                       final_through="2026-08-27")
        path = Path(self.cache.path_for(key))
        doc = json.loads(path.read_text("utf-8"))
        doc["schema_version"] = EVIDENCE_SCHEMA_VERSION + 1
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIsNone(self.cache.get(key, connected=True, generation=3))

    def test_a_corrupt_entry_is_removed_not_raised(self):
        key = self._key()
        self.cache.put(key, {"rows": [FINAL_ROW]}, generation=3,
                       final_through="2026-08-27")
        path = Path(self.cache.path_for(key))
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.cache.get(key, connected=True, generation=3))
        self.assertFalse(path.exists())


class ProvisionalTests(_Base):
    def test_provisional_rows_are_never_stored(self):
        """정산 전 값은 나중에 바뀐다. 캐시에 남으면 옛 숫자를 계속 준다."""
        key = self._key()
        rows = [{"date": "2026-08-28", "data_state": "provisional", "v": 1},
                {"date": "2026-08-27", "data_state": "final", "v": 2}]
        self.cache.put(key, {"rows": rows}, generation=3,
                       final_through=None)
        got = self.cache.get(key, connected=True, generation=3)
        stored = [r["date"] for r in got["payload"]["rows"]]
        self.assertEqual(stored, ["2026-08-27"])
        self.assertEqual(got["final_through"], "2026-08-27")

    def test_an_all_provisional_dataset_is_not_cached_at_all(self):
        key = self._key()
        self.cache.put(
            key, {"rows": [{"date": "2026-08-28",
                            "data_state": "provisional"}]},
            generation=3, final_through=None)
        self.assertIsNone(self.cache.get(key, connected=True, generation=3))

    def test_a_final_row_replaces_the_provisional_one_without_duplicating(
            self):
        """같은 날짜가 두 번 나오면 호출자는 어느 쪽이 맞는지 모른다."""
        key = self._key()
        self.cache.put(
            key, {"rows": [{"date": "2026-08-27", "data_state": "final",
                            "v": 1}]},
            generation=3, final_through="2026-08-27")
        self.cache.put(
            key, {"rows": [{"date": "2026-08-28", "data_state": "final",
                            "v": 9},
                           {"date": "2026-08-27", "data_state": "final",
                            "v": 1}]},
            generation=3, final_through="2026-08-28")
        got = self.cache.get(key, connected=True, generation=3)
        dates = [r["date"] for r in got["payload"]["rows"]]
        self.assertEqual(dates, sorted(set(dates), reverse=True))
        self.assertEqual(len(dates), len(set(dates)))
        self.assertEqual(got["final_through"], "2026-08-28")

    def test_stale_final_through_does_not_go_backwards(self):
        # 늦게 도착한 짧은 응답이 이미 가진 확정 구간을 줄이지 않는다.
        key = self._key()
        self.cache.put(
            key, {"rows": [{"date": "2026-08-28", "data_state": "final"}]},
            generation=3, final_through="2026-08-28")
        self.cache.put(
            key, {"rows": [{"date": "2026-08-26", "data_state": "final"}]},
            generation=3, final_through="2026-08-26")
        got = self.cache.get(key, connected=True, generation=3)
        self.assertEqual(got["final_through"], "2026-08-28")


class SecrecyTests(_Base):
    def test_nothing_secret_reaches_disk(self):
        key = self._key()
        self.cache.put(key, {
            "rows": [{"date": "2026-08-27", "data_state": "final"}],
            "headers": {"authorization": "Bearer SECRET-123"},
            "app_key": "SECRET-123",
            "raw_error": "denied SECRET-123",
        }, generation=3, final_through="2026-08-27")
        blob = Path(self.cache.path_for(key)).read_text("utf-8")
        for banned in ("SECRET-123", "authorization", "Bearer", "app_key",
                       "raw_error"):
            self.assertNotIn(banned, blob, banned)

    def test_only_allowlisted_fields_survive(self):
        key = self._key()
        self.cache.put(key, {
            "rows": [{"date": "2026-08-27", "data_state": "final"}],
            "coverage": {"rows": 1}, "measure": "net_quantity",
            "surprise_field": "x",
        }, generation=3, final_through="2026-08-27")
        got = self.cache.get(key, connected=True, generation=3)
        self.assertIn("coverage", got["payload"])
        self.assertIn("measure", got["payload"])
        self.assertNotIn("surprise_field", got["payload"])


class RemovalTests(_Base):
    def test_removing_one_provider_leaves_the_other(self):
        self.cache.put(self._key(provider="kiwoom"),
                       {"rows": [{"date": "2026-08-27",
                                  "data_state": "final"}]},
                       generation=1, final_through="2026-08-27")
        self.cache.put(self._key(provider="kis"),
                       {"rows": [{"date": "2026-08-27",
                                  "data_state": "final"}]},
                       generation=1, final_through="2026-08-27")
        self.cache.remove_provider("kiwoom")
        self.assertIsNone(self.cache.get(self._key(provider="kiwoom"),
                                         connected=True, generation=1))
        self.assertIsNotNone(self.cache.get(self._key(provider="kis"),
                                            connected=True, generation=1))

    def test_an_unregistered_provider_is_refused(self):
        with self.assertRaises(ValueError):
            self.cache.remove_provider("../../etc")

    def test_removal_is_idempotent(self):
        self.cache.remove_provider("kis")
        self.cache.remove_provider("kis")

    def test_the_root_is_separate_from_the_bar_cache(self):
        key = self._key()
        self.cache.put(key, {"rows": [{"date": "2026-08-27",
                                       "data_state": "final"}]},
                       generation=1, final_through="2026-08-27")
        path = Path(self.cache.path_for(key))
        self.assertIn("market_evidence", str(path))
        self.assertNotIn("market_data", str(path))


if __name__ == "__main__":
    unittest.main()
