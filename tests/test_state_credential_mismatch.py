"""상태 파일과 자격 증명 저장소가 어긋난 경우 (2026-08-28 리뷰 P1).

연결 상태 파일은 "connected" 라고 적혀 있는데 실제 keyring 슬롯을 읽지
못하는 상황이 있다. 다른 Windows 계정으로 실행했거나, 자격 증명이
지워졌거나, 키체인이 잠긴 경우다.

이때 라우터는 상태만 보고 그 증권사를 고르지만 공급자 객체는 만들어지지
않는다. 예전에는 그 자리에서 `KeyError` 로 죽어 사용자에게 원인도,
다음 행동도 전달되지 않았다. 구조화된 오류로 돌려주고 "다시 연결하라"는
행동을 알려줘야 한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.market_data.connection_state import (
    DEFAULT_STATE_V2,
    connect_profile_v2,
    save_state_v2,
)
from stock_mcp_server.market_data.router import (
    RouterError,
    SourceResolution,
    fetch_with_failover,
)


def _resolution(provider: str = "kis") -> SourceResolution:
    return SourceResolution(
        requested_source="auto",
        selected_provider=provider,
        selection_reason="broker_connected_and_intraday_supported",
        fallback_allowed_before_first_bar=False,
        mode="auto",
        capability_version=1,
        primary_provider=provider,
    )


class FetchWithMissingProviderTests(unittest.TestCase):
    def test_missing_provider_is_a_structured_error_not_keyerror(self):
        with self.assertRaises(RouterError) as ctx:
            asyncio.run(fetch_with_failover(
                _resolution("kis"),
                providers={"naver": object(), "yahoo": object()},
                request=None))
        self.assertEqual(ctx.exception.provider_status, "not_configured")

    def test_message_tells_the_user_to_reconnect(self):
        with self.assertRaises(RouterError) as ctx:
            asyncio.run(fetch_with_failover(
                _resolution("kiwoom"),
                providers={"naver": object()}, request=None))
        message = str(ctx.exception)
        # 고객에게는 공급자 id 가 아니라 증권사 이름으로 말한다.
        self.assertIn("키움증권", message)
        self.assertIn("다시 연결", message)
        # 키가 틀렸다는 뜻으로 읽히면 안 된다 (키는 읽지도 못한 상태다).
        self.assertNotIn("잘못", message)

    def test_no_silent_switch_to_another_provider(self):
        # 다른 공급자가 준비돼 있어도 대신 쓰지 않는다 (1.0 정책).
        with self.assertRaises(RouterError):
            asyncio.run(fetch_with_failover(
                _resolution("kis"),
                providers={"naver": object(), "kiwoom": object()},
                request=None))


class EndToEndMismatchTests(unittest.TestCase):
    """상태는 연결, keyring 은 비어 있는 홈에서 도구가 죽지 않는다."""

    def setUp(self):
        import os
        import shutil

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # conftest 가 격리 홈에 넣어 둔 라이선스·시계 파일을 그대로 쓴다.
        # 없으면 라이선스 게이트가 먼저 걸려 이 경로에 도달하지 못한다.
        source = Path(os.environ.get("STOCKLENS_HOME", ""))
        for name in ("license.key", "revoked_cache.json", "clock_seen"):
            src = source / name
            if src.exists():
                shutil.copy2(src, self.home / name)
        state = json.loads(json.dumps(DEFAULT_STATE_V2))
        state = connect_profile_v2(
            state, "kis", "real", credential_ref="gone-ref",
            verified=True, verified_at="2026-08-28T09:00:00+09:00",
            capabilities={"auth": "ok", "kr_intraday": "available",
                          "us_intraday": "available"})
        state["data_source_mode"] = "auto"
        save_state_v2(state, self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_intraday_returns_an_error_result_not_a_crash(self):
        import os

        from stock_mcp_server import server

        runtime = server._ProviderRuntimeCls(home=self.home)
        with patch.dict(os.environ,
                        {"STOCKLENS_HOME": str(self.home)}), \
             patch.object(server, "_PROVIDER_RUNTIME", runtime):
            result = asyncio.run(server.get_intraday_chart(
                "005930", market="KR", interval="5m", source="auto"))
        self.assertIsInstance(result, str)
        # 죽지 않고, 무엇을 해야 하는지 알려준다.
        self.assertIn("다시 연결", result)
        self.assertIn('"provider_status": "not_configured"', result)


if __name__ == "__main__":
    unittest.main()
