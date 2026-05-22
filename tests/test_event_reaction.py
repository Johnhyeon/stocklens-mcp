"""event_date 기준 주가·수급 반응 계산 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from stock_mcp_server.event_reaction import build_event_reaction, format_event_reaction


def _bar(date, close, volume=1000):
    return {
        "date": date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _flow(date, inst, foreign):
    return {
        "date": date,
        "close": 0,
        "volume": 0,
        "institutional": inst,
        "foreign": foreign,
    }


class EventReactionTests(unittest.TestCase):
    def test_uses_exact_event_trading_day(self):
        bars = [
            _bar("20260508", 100, 1000),
            _bar("20260511", 105, 1100),
            _bar("20260512", 110, 1200),
            _bar("20260513", 120, 1300),
            _bar("20260514", 130, 1400),
            _bar("20260515", 150, 3000),
            _bar("20260518", 165, 2500),
            _bar("20260519", 160, 2000),
            _bar("20260520", 180, 2200),
            _bar("20260521", 210, 2400),
            _bar("20260522", 200, 2600),
        ]
        flows = [
            _flow("2026.05.14", -100, 200),
            _flow("2026.05.15", 500, 700),
            _flow("2026.05.18", 600, -100),
            _flow("2026.05.19", -200, 300),
            _flow("2026.05.20", 100, 400),
            _flow("2026.05.21", 0, 500),
            _flow("2026.05.22", 200, 600),
        ]

        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=bars,
            flows=flows,
            before=5,
            after=5,
        )

        self.assertEqual(reaction["event_date"], "2026-05-15")
        self.assertEqual(reaction["basis_trading_date"], "2026-05-15")
        self.assertEqual(reaction["points"]["D-5"]["date"], "2026-05-08")
        self.assertAlmostEqual(reaction["points"]["D-5"]["return_pct"], -33.33)
        self.assertEqual(reaction["points"]["D+5"]["date"], "2026-05-22")
        self.assertAlmostEqual(reaction["points"]["D+5"]["return_pct"], 33.33)
        self.assertEqual(reaction["flow"]["post"]["institutional"], 1200)
        self.assertEqual(reaction["flow"]["post"]["foreign"], 2400)

    def test_weekend_event_uses_next_trading_day(self):
        bars = [
            _bar("20260515", 150),
            _bar("20260518", 165),
            _bar("20260519", 160),
        ]

        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-16",
            ohlcv=bars,
            flows=[],
            before=1,
            after=1,
        )

        self.assertEqual(reaction["event_date"], "2026-05-16")
        self.assertEqual(reaction["basis_trading_date"], "2026-05-18")
        self.assertEqual(reaction["points"]["D0"]["close"], 165)

    def test_markdown_states_dartlens_timeline_contract(self):
        reaction = build_event_reaction(
            code="005930",
            event_date="2026-05-15",
            ohlcv=[_bar("20260514", 130), _bar("20260515", 150), _bar("20260518", 165)],
            flows=[],
            before=1,
            after=1,
        )
        text = format_event_reaction(reaction)

        self.assertIn("event_date=2026-05-15", text)
        self.assertIn("기준 거래일=2026-05-15", text)
        self.assertIn("DartLens", text)
        self.assertIn("공시 접수일", text)
        self.assertIn("해당 기간 공시", text)


class EventReactionToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_formats_event_reaction_without_network(self):
        from stock_mcp_server import server

        bars = [_bar("20260514", 130), _bar("20260515", 150), _bar("20260518", 165)]
        flows = [_flow("2026.05.15", 500, 700)]

        with patch.object(server, "get_ohlcv", AsyncMock(return_value=bars)), patch.object(
            server, "get_investor_flow", AsyncMock(return_value=flows)
        ):
            text = await server.get_event_reaction(
                code="005930",
                event_date="2026-05-15",
                before=1,
                after=1,
            )

        self.assertIn("event_date=2026-05-15", text)
        self.assertIn("기준 거래일=2026-05-15", text)
        self.assertIn("DartLens", text)


if __name__ == "__main__":
    unittest.main()
