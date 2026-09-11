"""meta v3 증권사 확장 계약 테스트 (Task 13).

- 기존 data_as_of 는 날짜 계약 유지 (timestamp 를 넣어도 날짜로 정규화)
- data_as_of_timestamp 는 additive 선택 필드
- 공급자 오류는 provider_status 로만 표현, 허용값은 설계 27절 목록
- 기존 coverage reason 검증은 그대로 동작
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _result_meta as rmeta


class ProviderStatusContractTests(unittest.TestCase):
    def test_provider_statuses_match_design(self):
        self.assertEqual(rmeta.PROVIDER_STATUSES, (
            "ok", "not_configured", "credential_invalid",
            "authentication_failed", "permission_denied", "rate_limited",
            "provider_unavailable", "source_parse_error", "entity_not_found",
            "no_session", "partial",
            # 1.0 승인 추가: 미지원·미검증 시장 (키 문제와 구분)
            "unsupported",
        ))

    def test_provider_extension_valid(self):
        ext = rmeta.provider_extension(
            provider="kis",
            provider_status="ok",
            provider_profile="real",
            requested_source="auto",
            selection_reason="broker_connected_and_intraday_supported",
            fallback_used=False,
            fallback_from=None,
            venue="KRX",
            timezone="Asia/Seoul",
            requested_interval="5m",
            source_interval="1m",
            aggregation_method="stocklens_session_resample",
            adjustment_basis="unadjusted",
            data_as_of_timestamp="2026-08-27T15:25:00+09:00",
        )
        self.assertEqual(ext["provider"], "kis")
        self.assertEqual(ext["provider_status"], "ok")
        self.assertEqual(ext["data_as_of_timestamp"],
                         "2026-08-27T15:25:00+09:00")

    def test_unknown_provider_status_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider_status"):
            rmeta.provider_extension(
                provider="kis", provider_status="totally_broken")

    def test_naive_timestamp_rejected(self):
        with self.assertRaisesRegex(ValueError, "data_as_of_timestamp"):
            rmeta.provider_extension(
                provider="kis", provider_status="ok",
                data_as_of_timestamp="2026-08-27T15:25:00")

    def test_garbage_timestamp_rejected(self):
        with self.assertRaisesRegex(ValueError, "data_as_of_timestamp"):
            rmeta.provider_extension(
                provider="kis", provider_status="ok",
                data_as_of_timestamp="어제쯤")


class ExistingContractGuardTests(unittest.TestCase):
    def _meta(self, **kwargs) -> dict:
        base = dict(lens="stocklens", data_basis=rmeta.BASIS_LAST_CLOSE)
        base.update(kwargs)
        return rmeta.build_meta(**base)

    def test_data_as_of_stays_a_date_even_from_timestamp(self):
        meta = self._meta(data_as_of="2026-08-27T15:25:00+09:00")
        self.assertEqual(meta["data_as_of"], "2026-08-27")

    def test_unknown_coverage_reason_still_fails(self):
        with self.assertRaisesRegex(ValueError, "coverage reason"):
            self._meta(
                data_completeness=rmeta.PARTIAL,
                coverage={"truncated": True, "coverage_complete": False,
                          "reason": "kis_broke_lol"})

    def test_meta_v_is_3(self):
        self.assertEqual(self._meta()["meta_v"], 3)


class ServerExtensionUsesContractTests(unittest.TestCase):
    def test_intraday_meta_extra_passes_validation(self):
        """server._intraday_meta_extra 산출물이 계약 검증을 통과해야 한다."""
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from stock_mcp_server import server
        from stock_mcp_server.market_data.models import (
            BarDataset,
            NormalizedBar,
        )

        start = datetime(2026, 8, 27, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        bar = NormalizedBar(
            start_at=start, end_at=start + timedelta(minutes=5),
            open=Decimal(1), high=Decimal(2), low=Decimal(1),
            close=Decimal(2), volume=1, interval="5m", session="regular",
            complete=True, session_tail=False, expected_minutes=5,
            actual_minutes=5, data_integrity="complete",
            source_gap_status="none")
        ds = BarDataset(
            bars=(bar,), market="KR", symbol="005930", provider="kis",
            profile="real", venue="KRX", timezone="Asia/Seoul",
            session="regular", requested_interval="5m",
            source_interval="1m",
            aggregation_method="stocklens_session_resample",
            adjustment_basis="unadjusted", source_endpoint="test",
            coverage={}, warnings=())
        route_meta = {
            "requested_source": "auto", "selected_provider": "kis",
            "selection_reason": "broker_connected_and_intraday_supported",
            "mode": "auto", "fallback_used": False, "fallback_from": None,
        }
        extra = server._intraday_meta_extra(ds, route_meta)
        # provider_extension 재검증: 계약 위반이면 여기서 죽는다.
        rmeta.provider_extension(**{
            k: v for k, v in extra.items()
            if k in rmeta.PROVIDER_EXTENSION_FIELDS
        })
        self.assertEqual(extra["provider_status"], "ok")


if __name__ == "__main__":
    unittest.main()
