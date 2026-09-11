import unittest
from unittest.mock import AsyncMock, patch

from stock_mcp_server import naver
from stock_mcp_server.naver import PARSE_MISS_KEY, _quote_date, _rate_info, _status_flags

# 2026-07 NXT 사태 회귀 테스트.
# 네이버는 코스피200/코스닥150 등 NXT 대상 종목의 시세를 KRX/NXT 두 벌로 준다.
# 두 벌을 한 레코드에 섞으면 종가=KRX, 거래량=NXT 인 뒤섞인 값이 만들어진다
# (005930 에서 실제로 났던 버그). 화면이 JSON API 로 바뀐 뒤에도 이 경계는 같다.
KRX_DETAIL = {
    "itemcode": "005930",
    "itemname": "삼성전자",
    "tradeTime": "20260911161021",
    "manageStatusGb": "0",
    "tradeStopYn": "N",
    "marketAlertType": "00",
    "nowPrice": "318000",
    "prevChangePrice": "8500",
    "openPrice": "320000",
    "highPrice": "325000",
    "lowPrice": "303000",
    "tradeVolume": "23805229",
}

NXT_DETAIL = dict(
    KRX_DETAIL,
    nowPrice="320000",
    prevChangePrice="10500",
    openPrice="315500",
    highPrice="324500",
    tradeVolume="19563716",
)


class RateInfoTests(unittest.TestCase):
    def test_reads_every_quote_field(self) -> None:
        info, missing = _rate_info(KRX_DETAIL)

        self.assertEqual(missing, [])  # 정상 응답이면 못 읽은 항목이 없어야 한다
        self.assertEqual(
            info,
            {
                "price": 318000,
                "change": 8500,
                "open": 320000,
                "high": 325000,
                "low": 303000,
                "volume": 23805229,
            },
        )

    def test_negative_change_keeps_its_sign(self) -> None:
        info, _ = _rate_info(dict(KRX_DETAIL, prevChangePrice="-500"))
        self.assertEqual(info["change"], -500)

    def test_missing_field_is_reported_not_zeroed(self) -> None:
        """거래량 0은 거래정지 종목의 실제 값이다. 결측과 같은 자리에 두면 안 된다."""
        info, missing = _rate_info(dict(KRX_DETAIL, tradeVolume=None))

        self.assertNotIn("volume", info)
        self.assertEqual(missing, ["volume"])

    def test_real_zero_volume_is_kept_as_zero(self) -> None:
        info, missing = _rate_info(dict(KRX_DETAIL, tradeVolume="0"))

        self.assertEqual(info["volume"], 0)
        self.assertEqual(missing, [])


class StatusFlagTests(unittest.TestCase):
    def test_normal_stock_has_no_flags(self) -> None:
        self.assertEqual(_status_flags(KRX_DETAIL), [])

    def test_management_and_alert_labels_come_from_the_source(self) -> None:
        flags = _status_flags(
            dict(KRX_DETAIL, manageStatusGb="1", marketAlertType="02", tradeStopYn="Y")
        )
        self.assertEqual(flags, ["관리종목", "투자경고", "거래정지"])

    def test_quote_date_from_trade_time(self) -> None:
        self.assertEqual(_quote_date("20260911161021"), "2026-09-11")
        self.assertIsNone(_quote_date(""))
        self.assertIsNone(_quote_date(None))


class CurrentPriceMarketSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, is_nxt: bool) -> dict:
        async def fake_detail(code, code_type="KRX"):
            return NXT_DETAIL if code_type == "NXT" else KRX_DETAIL

        async def fake_json(url, *, what, params=None):
            return {"sosok": "KOSPI", "isNxtYn": "Y" if is_nxt else "N"}

        with patch.object(naver, "_stock_detail", AsyncMock(side_effect=fake_detail)), \
             patch.object(naver, "_api_json", AsyncMock(side_effect=fake_json)):
            return await naver.get_current_price.__wrapped__("005930")

    async def test_nxt_values_never_overwrite_krx(self) -> None:
        result = await self._run(is_nxt=True)

        self.assertEqual(result["price"], 318000)
        self.assertEqual(result["volume"], 23805229)
        self.assertEqual(result["nxt_price"], 320000)
        self.assertEqual(result["nxt_volume"], 19563716)
        self.assertNotEqual(result["price"], result["nxt_price"])
        self.assertNotEqual(result["volume"], result["nxt_volume"])
        self.assertEqual(result[PARSE_MISS_KEY], [])

    async def test_non_nxt_stock_has_no_nxt_fields(self) -> None:
        result = await self._run(is_nxt=False)

        self.assertEqual(result["price"], 318000)
        self.assertFalse([k for k in result if k.startswith("nxt_")])


if __name__ == "__main__":
    unittest.main()
