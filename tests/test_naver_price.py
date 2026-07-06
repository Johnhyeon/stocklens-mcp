import unittest

from bs4 import BeautifulSoup

from stock_mcp_server.naver import _parse_rate_info

# 2026-07-NXT 사태 회귀 테스트용 최소 마크업.
# 네이버가 코스피200/코스닥150 등 NXT 대상 종목 페이지에 KRX/NXT 탭을 추가하면서,
# table.no_info가 페이지에 두 번(KRX용, NXT용) 나타나게 됐다. 스코프 없이 select()로
# 순회하면 나중에 나오는 NXT 값이 KRX 값을 덮어써 종가=KRX, 거래량=NXT인 뒤섞인
# 레코드가 만들어졌던 실제 버그(005930 확인)를 재현한다.
NXT_TAB_HTML = """
<div id="rate_info_krx" style="display: none;">
    <p class="no_today"><em class="no_up"><span class="blind">318,000</span></em></p>
    <p class="no_exday">
        <em class="no_up"><span class="ico up">상승</span><span class="blind">8,500</span></em>
    </p>
    <table class="no_info">
        <tr>
            <td><span class="sptxt">전일</span><em><span class="blind">309,500</span></em></td>
            <td><span class="sptxt">고가</span><em><span class="blind">325,000</span></em></td>
            <td><span class="sptxt">거래량</span><em><span class="blind">23,805,229</span></em></td>
        </tr>
        <tr>
            <td><span class="sptxt">시가</span><em><span class="blind">320,000</span></em></td>
            <td><span class="sptxt">저가</span><em><span class="blind">303,000</span></em></td>
        </tr>
    </table>
</div>
<div id="rate_info_nxt" style="display: block;">
    <p class="no_today"><em class="no_up"><span class="blind">320,000</span></em></p>
    <p class="no_exday">
        <em class="no_up"><span class="ico up">상승</span><span class="blind">10,500</span></em>
    </p>
    <table class="no_info">
        <tr>
            <td><span class="sptxt">전일</span><em><span class="blind">309,500</span></em></td>
            <td><span class="sptxt">고가</span><em><span class="blind">324,500</span></em></td>
            <td><span class="sptxt">거래량</span><em><span class="blind">19,563,716</span></em></td>
        </tr>
        <tr>
            <td><span class="sptxt">시가</span><em><span class="blind">315,500</span></em></td>
            <td><span class="sptxt">저가</span><em><span class="blind">303,000</span></em></td>
        </tr>
    </table>
</div>
"""

NO_TABS_HTML = """
<p class="no_today"><em class="no_up"><span class="blind">50,000</span></em></p>
<p class="no_exday">
    <em class="no_down"><span class="ico down">하락</span><span class="blind">500</span></em>
</p>
<table class="no_info">
    <tr>
        <td><span class="sptxt">전일</span><em><span class="blind">50,500</span></em></td>
        <td><span class="sptxt">고가</span><em><span class="blind">50,800</span></em></td>
        <td><span class="sptxt">거래량</span><em><span class="blind">1,234,567</span></em></td>
    </tr>
    <tr>
        <td><span class="sptxt">시가</span><em><span class="blind">50,600</span></em></td>
        <td><span class="sptxt">저가</span><em><span class="blind">49,900</span></em></td>
    </tr>
</table>
"""


class ParseRateInfoTests(unittest.TestCase):
    def test_krx_and_nxt_blocks_stay_separate(self) -> None:
        soup = BeautifulSoup(NXT_TAB_HTML, "lxml")

        krx = _parse_rate_info(soup.select_one("#rate_info_krx"))
        nxt = _parse_rate_info(soup.select_one("#rate_info_nxt"))

        self.assertEqual(
            krx,
            {
                "price": 318000,
                "change": 8500,
                "high": 325000,
                "volume": 23805229,
                "open": 320000,
                "low": 303000,
            },
        )
        self.assertEqual(
            nxt,
            {
                "price": 320000,
                "change": 10500,
                "high": 324500,
                "volume": 19563716,
                "open": 315500,
                "low": 303000,
            },
        )
        # 회귀 확인: 종가/거래량이 서로 다른 시장 값으로 섞이지 않아야 한다.
        self.assertNotEqual(krx["price"], nxt["price"])
        self.assertNotEqual(krx["volume"], nxt["volume"])

    def test_stock_without_nxt_tabs_falls_back_to_whole_scope(self) -> None:
        soup = BeautifulSoup(NO_TABS_HTML, "lxml")

        self.assertIsNone(soup.select_one("#rate_info_krx"))

        result = _parse_rate_info(soup)

        self.assertEqual(
            result,
            {
                "price": 50000,
                "change": -500,
                "high": 50800,
                "volume": 1234567,
                "open": 50600,
                "low": 49900,
            },
        )


if __name__ == "__main__":
    unittest.main()
