"""1.0.0 으로 되돌려도 상태 파일이 그대로 읽힌다 (1.1 릴리스 조건).

1.1 은 상세 수급을 더하면서 **상태 파일 스키마를 올리지 않았다.** 증거
능력은 어댑터가 실측으로 확정한 정적 사실이라 저장할 이유가 없고,
저장하면 구버전이 그 파일을 읽을 때 정리 대상이 된다.

여기서 확인하는 것은 "안 넣었다"는 선언이 아니라, **실제로 증거 조회를
돌린 뒤의 상태 파일**에 그 키가 없다는 사실이다. 선언은 코드가 바뀌면
같이 안 바뀐다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.connection_state import (
    connect_profile_v2,
    load_state_v2,
    sanitize_v2,
    save_state_v2,
)
from stock_mcp_server.market_data.evidence_router import (
    EVIDENCE_CAPABILITIES,
)

# 1.0.0 이 아는 능력 키. 이 밖의 키가 저장되면 구버전이 정리한다.
V1_0_CAPABILITY_KEYS = {"auth", "kr_intraday", "us_intraday",
                        "kr_daily", "us_daily"}


class StateSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _seed(self) -> dict:
        state = connect_profile_v2(
            load_state_v2(self.home), "kis", "real",
            credential_ref="kis:real", verified=True,
            verified_at="2026-08-28T00:00:00+09:00",
            capabilities={"auth": "ok", "kr_intraday": "available",
                          "us_intraday": "available"})
        save_state_v2(state, self.home)
        return state

    def test_no_evidence_capability_is_ever_persisted(self):
        self._seed()
        blob = json.dumps(load_state_v2(self.home))
        for name in EVIDENCE_CAPABILITIES:
            self.assertNotIn(name, blob, name)

    def test_stored_capability_keys_stay_within_what_1_0_knows(self):
        self._seed()
        state = load_state_v2(self.home)
        for record in state["providers"].values():
            for profile in record["profiles"].values():
                extra = set(profile.get("capabilities") or {}) - \
                    V1_0_CAPABILITY_KEYS
                self.assertEqual(extra, set(),
                                 f"1.0 이 모르는 능력 키가 저장됐다: {extra}")

    def test_the_state_version_is_unchanged(self):
        state = self._seed()
        self.assertEqual(state["state_version"],
                         sanitize_v2({})["state_version"])

    def test_a_1_0_shaped_state_survives_a_round_trip(self):
        """구버전이 쓴 파일을 1.1 이 읽고 다시 써도 모양이 그대로다."""
        original = self._seed()
        save_state_v2(load_state_v2(self.home), self.home)
        again = load_state_v2(self.home)
        self.assertEqual(sorted(again), sorted(original))
        self.assertEqual(sorted(again["providers"]),
                         sorted(original["providers"]))

    def test_an_evidence_request_does_not_touch_the_state_file(self):
        """증거 조회가 상태 파일을 건드리지 않는다.

        읽기만 하는 경로다. 여기서 뭔가 쓰기 시작하면 구버전 호환이
        조용히 깨진다.
        """
        from stock_mcp_server.market_data.evidence_service import (
            EvidenceService,
        )

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_evidence_service import _FakeAdapter, _FakeRuntime
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )

        self._seed()
        path = self.home / "broker_state.json"
        before = path.read_bytes() if path.exists() else None

        service = EvidenceService(
            runtime=_FakeRuntime(
                primary="kiwoom",
                adapters={"kiwoom": _FakeAdapter("kiwoom",
                                                 KiwoomEvidenceProvider)}),
            base_date=date(2026, 8, 27), release_override=True)
        asyncio.run(service.investor_flow(code="005930", days=5))

        after = path.read_bytes() if path.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
