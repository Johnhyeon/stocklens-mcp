"""한국 주식 데이터 MCP 서버.

네이버 증권에서 차트, 수급, 재무 데이터를 가져와
Claude에서 자연어로 분석할 수 있게 해줍니다.
"""

import functools
import httpx

from mcp.server.fastmcp import FastMCP
from stock_mcp_server.naver import (
    search_stock,
    get_ohlcv,
    get_current_price,
    get_investor_flow,
    get_financials,
    get_market_index,
    list_themes as naver_list_themes,
    get_theme_stocks as naver_get_theme_stocks,
    list_sectors as naver_list_sectors,
    get_sector_stocks as naver_get_sector_stocks,
    get_volume_ranking as naver_get_volume_ranking,
    get_change_ranking as naver_get_change_ranking,
    get_market_cap_ranking as naver_get_market_cap_ranking,
    get_multi_stocks as naver_get_multi_stocks,
    get_multi_chart_stats as naver_get_multi_chart_stats,
)


def safe_tool(func):
    """MCP 도구 함수의 예외를 사용자 친화적 메시지로 변환합니다."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.TimeoutException:
            return "⚠️ 네이버 증권 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요."
        except httpx.ConnectError:
            return "⚠️ 네이버 증권에 연결할 수 없습니다. 인터넷 연결을 확인해주세요."
        except httpx.HTTPError as e:
            return f"⚠️ 네트워크 오류가 발생했습니다: {type(e).__name__}"
        except Exception as e:
            return (
                f"⚠️ 데이터 처리 중 오류가 발생했습니다: {type(e).__name__}\n"
                f"종목코드가 올바른지, 상장된 종목인지 확인해주세요."
            )

    return wrapper

mcp = FastMCP(
    "Korean Stock Data",
    instructions="""한국 주식 데이터를 네이버 증권에서 실시간 조회합니다.
종목코드(예: 005930)나 종목명(예: 삼성전자)으로 검색할 수 있습니다.
차트 데이터, 투자자 수급, 재무지표, 시장 지수를 제공합니다.

## 차트 시각화 규칙

캔들 차트를 HTML Canvas/SVG로 렌더링할 때는 반드시 아래 규칙을 따를 것.

### 기본 기간
- **차트는 기본 180일(약 9개월)을 사용한다.** 사용자가 다른 기간을 명시하지 않으면 180일.
- get_chart 호출 시 count=180이 기본값.

### 필수 구성 (절대 생략 금지)
- **캔들 차트는 반드시 상단 캔들 패널 + 하단 거래량 패널 세트로 구성한다.**
- 거래량 패널 없이 캔들만 그리는 것은 금지. 무조건 세트로 묶일 것.

### 레이아웃
- 캔들 패널: 전체 차트 높이의 약 73%
- 간격(GAP): 두 패널 사이 ≈ 10px 공백
- 거래량 패널: 전체 차트 높이의 약 22%
- 거래량 바 색상은 해당 봉의 상승/하락 색상과 동일하게 (양봉 빨강, 음봉 파랑)

### 영역 구분
- 전체 오른쪽 여백: PAD_R = AXIS_W + LABEL_W
- AXIS_W (≈ 42px): 차트 영역 바로 오른쪽. 세로 축선 + tick + 가격 숫자("XX만")만 표시
- LABEL_W (≈ 148px): AXIS_W 오른쪽. S/R 라벨명과 가격을 2줄로 좌측 정렬 표시
- 두 영역은 물리적으로 분리되어 절대 겹치지 않아야 함

### 가격축
- 눈금선(grid)은 PAD_L ~ AXIS_X 구간에만 그릴 것
- tick은 AXIS_X에서 오른쪽으로 4px 짧게 표시
- 가격 숫자는 tick 바로 오른쪽(AXIS_X + 6px)에 표시

### S/R 라벨 렌더링
- S/R 선은 캔들 패널 내 AXIS_X(= W - PAD_R) 까지만 그릴 것
- 라벨은 LABEL_X(= W - PAD_R + AXIS_W + 4) 기준 좌측 정렬
- 1행: 라벨명 (예: "강한 지지 (이중저점)")
- 2행: 가격 (예: "16.7만"), 1행보다 약간 작은 폰트 + 낮은 opacity

### 캔들 스페이싱 (중요)

캔들 간 간격과 몸통 너비는 **가용 폭과 데이터 개수에 따라 동적 계산**할 것.
고정값(예: CANDLE_W = 100)을 쓰지 말 것.

**공식 (최종):**
```
availableW = W - PAD_L - PAD_R            # 캔들 영역 실제 폭
step = availableW / candles.length         # 캔들 간 간격 (center-to-center)
bodyW = Math.max(1, step - 1)              # 몸통 = step에서 1px만 뺌 (양옆 0.5px)
wickStrokeW = Math.max(1, Math.min(1.5, bodyW / 6))  # 심지 두께
```

**중요: bodyW = step * 0.7 같은 비율 기반 공식은 쓰지 말 것.
간격이 실제보다 넓어 보여서 이질감이 생김.**

**제약:**
- `step`은 최소 2px, 최대 20px로 클램프
- 180일 일봉 + 폭 800px + PAD(좌40, 우190) 기준 → step ≈ 3.2px, bodyW ≈ 2.2px (자연스러움)
- 60일 일봉 + 폭 800px 기준 → step ≈ 9.5px, bodyW ≈ 8.5px
- 캔들 x좌표: `x = PAD_L + (i + 0.5) * step`  (center 기준)
- 몸통 rect의 x: `x - bodyW / 2`

**나쁜 예시:**
```javascript
const CANDLE_W = 100;                      // 고정값 금지
const bodyW = step * 0.7;                  // 비율 금지 — 너무 벌어짐
<rect x="${x-6}" width="12" .../>          // 간격 대비 몸통 12%는 띄엄띄엄함
```

**좋은 예시:**
```javascript
const availableW = W - PAD_L - PAD_R;
const step = Math.max(2, Math.min(20, availableW / candles.length));
const bodyW = Math.max(1, step - 1);  // 핵심: step - 1
candles.forEach((c, i) => {
  const cx = PAD_L + (i + 0.5) * step;
  // 심지
  line(cx, py(c.high), cx, py(c.low), strokeWidth=1);
  // 몸통
  rect(cx - bodyW/2, py(top), bodyW, bodyH);
});

// 거래량 패널 (필수)
const maxVol = Math.max(...candles.map(c => c.volume));
candles.forEach((c, i) => {
  const cx = PAD_L + (i + 0.5) * step;
  const h = (c.volume / maxVol) * volPanelH;
  const color = c.close >= c.open ? redColor : blueColor;
  rect(cx - bodyW/2, volPanelBottom - h, bodyW, h, fill=color);
});
```
""",
)


@mcp.tool()
@safe_tool
async def search(query: str) -> str:
    """종목검색 — 종목명 또는 종목코드로 검색합니다.
    "삼성전자 종목코드", "반도체 관련주", "005930 뭐야" 같은 질문에 사용합니다.

    Args:
        query: 검색할 종목명 또는 코드 (예: "삼성전자", "005930")
    """
    results = await search_stock(query)
    if not results:
        return f"'{query}'에 대한 검색 결과가 없습니다."

    lines = [f"검색 결과 ({len(results)}건):"]
    for r in results:
        lines.append(f"  - {r['name']} ({r['code']})")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_chart(code: str, timeframe: str = "day", count: int = 180) -> str:
    """차트데이터 — 종목의 OHLCV(시가/고가/저가/종가/거래량) 차트 데이터를 가져옵니다.
    "삼성전자 일봉", "차트 보여줘", "3개월 주봉", "월봉 데이터" 같은 질문에 사용합니다.
    기본 180일(약 9개월)로, 충분한 기술적 분석이 가능한 기간.

    Args:
        code: 종목코드 6자리 (예: "005930")
        timeframe: "day"(일봉), "week"(주봉), "month"(월봉)
        count: 가져올 봉 개수 (기본 180, 최대 500)
    """
    count = min(count, 500)
    data = await get_ohlcv(code, timeframe, count)
    if not data:
        return f"종목코드 {code}의 차트 데이터를 가져올 수 없습니다."

    tf_name = {"day": "일봉", "week": "주봉", "month": "월봉"}.get(timeframe, timeframe)
    lines = [f"종목 {code} {tf_name} 데이터 ({len(data)}개):", ""]
    lines.append("날짜 | 시가 | 고가 | 저가 | 종가 | 거래량")
    lines.append("---|---|---|---|---|---")
    for row in data:
        lines.append(
            f"{row['date']} | {row['open']:,} | {row['high']:,} | "
            f"{row['low']:,} | {row['close']:,} | {row['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_price(code: str) -> str:
    """현재가 — 종목의 현재 시세 정보를 가져옵니다.
    "삼성전자 지금 얼마", "현재가", "오늘 시세", "주가 알려줘" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    data = await get_current_price(code)
    if not data or "price" not in data:
        return f"종목코드 {code}의 현재가를 가져올 수 없습니다."

    lines = [
        f"종목: {data.get('name', code)} ({code})",
        f"현재가: {data['price']:,}원",
    ]
    if "change" in data:
        sign = "+" if data["change"] > 0 else ""
        lines.append(f"전일대비: {sign}{data['change']:,}원")
    if "open" in data:
        lines.append(f"시가: {data['open']:,}원")
    if "high" in data:
        lines.append(f"고가: {data['high']:,}원")
    if "low" in data:
        lines.append(f"저가: {data['low']:,}원")
    if "volume" in data:
        lines.append(f"거래량: {data['volume']:,}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_flow(code: str, days: int = 20) -> str:
    """투자자수급 — 투자자별 매매동향 (외국인/기관/개인 순매수량)을 가져옵니다.
    "외국인 수급", "기관 순매수", "수급 분석", "누가 사고 있어" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
        days: 조회할 일수 (기본 20일)
    """
    days = min(days, 60)
    data = await get_investor_flow(code, days)
    if not data:
        return f"종목코드 {code}의 수급 데이터를 가져올 수 없습니다."

    lines = [f"종목 {code} 투자자별 매매동향 ({len(data)}일):", ""]
    lines.append("날짜 | 종가 | 거래량 | 기관 순매매 | 외국인 순매매")
    lines.append("---|---|---|---|---")
    for row in data:
        lines.append(
            f"{row['date']} | {row['close']:,} | {row['volume']:,} | "
            f"{row['institutional']:,} | {row['foreign']:,}"
        )

    # 합계
    total_inst = sum(r["institutional"] for r in data)
    total_frgn = sum(r["foreign"] for r in data)
    lines.append("")
    lines.append(f"합계 | - | - | {total_inst:,} | {total_frgn:,}")

    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_financial(code: str) -> str:
    """재무지표 — 종목의 주요 재무지표(PER, PBR, 시가총액 등)를 가져옵니다.
    "PER", "PBR", "재무제표", "시가총액", "저평가" 같은 질문에 사용합니다.

    Args:
        code: 종목코드 6자리 (예: "005930")
    """
    data = await get_financials(code)
    if not data:
        return f"종목코드 {code}의 재무지표를 가져올 수 없습니다."

    lines = [f"종목: {data.get('name', code)} ({code})", ""]
    for key, value in data.items():
        if key in ("code", "name"):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {' | '.join(value)}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_index() -> str:
    """시장지수 — KOSPI, KOSDAQ 지수 현재값을 가져옵니다.
    "코스피", "코스닥", "시장 지수", "오늘 시장 어때" 같은 질문에 사용합니다.
    """
    data = await get_market_index()
    if not data:
        return "시장 지수를 가져올 수 없습니다."

    lines = ["시장 지수:"]
    for item in data:
        lines.append(f"  {item['index']}: {item.get('value', '-')} ({item.get('change', '-')})")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def list_themes(page: int = 1) -> str:
    """테마목록 — 네이버 증권의 테마 목록을 가져옵니다.
    "어떤 테마가 있어?", "테마 리스트", "오늘 강세 테마" 같은 질문에 사용합니다.
    총 7페이지가 있으며 한 페이지당 40개 테마가 있습니다. 전일대비 등락률 순으로 정렬되어 있어요.

    Args:
        page: 페이지 번호 (1~7, 기본 1)
    """
    page = max(1, min(page, 7))
    themes = await naver_list_themes(page=page)
    if not themes:
        return f"페이지 {page}의 테마 목록을 가져올 수 없습니다."

    lines = [f"테마 목록 (page {page}, {len(themes)}개):", ""]
    lines.append("테마명 | 전일대비 | 최근3일 | 상승/보합/하락 | 주도주")
    lines.append("---|---|---|---|---")
    for t in themes:
        leaders = ", ".join(t["leaders"])
        counts = f"{t['up_count']}/{t['flat_count']}/{t['down_count']}"
        lines.append(
            f"{t['name']} | {t['change_rate']} | {t['recent_3d_rate']} | {counts} | {leaders}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_theme_stocks(
    theme_name: str,
    count: int = 30,
    include_reason: bool = True,
) -> str:
    """테마종목 — 특정 테마에 속한 종목 리스트를 가져옵니다.
    "반도체 테마 종목", "2차전지 관련주", "AI 테마주" 같은 질문에 사용합니다.
    테마명 부분 매칭을 지원합니다.

    Args:
        theme_name: 테마명 (예: "2차전지", "AI", "반도체")
        count: 반환할 최대 종목 수 (기본 30)
        include_reason: 편입사유 포함 여부. False로 하면 토큰 대폭 절감.
                        "왜 이 테마에 들어갔는지" 필요 없으면 False 권장.
    """
    count = min(count, 50)
    result = await naver_get_theme_stocks(
        theme_name,
        count=count,
        include_reason=include_reason,
    )
    if not result.get("theme_id"):
        return (
            f"'{theme_name}' 테마를 찾을 수 없습니다. "
            f"list_themes로 전체 테마 목록을 먼저 확인해보세요."
        )

    stocks = result["stocks"]
    lines = [f"테마: {result['theme_name']} ({len(stocks)}개 종목)", ""]

    if include_reason:
        lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량 | 편입사유")
        lines.append("---|---|---|---|---|---")
        for s in stocks:
            reason = s.get("reason", "")
            lines.append(
                f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | "
                f"{s['volume']:,} | {reason}"
            )
    else:
        lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량")
        lines.append("---|---|---|---|---")
        for s in stocks:
            lines.append(
                f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | {s['volume']:,}"
            )

    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def list_sectors() -> str:
    """업종목록 — 네이버 증권의 업종(섹터) 목록을 가져옵니다.
    "업종별 현황", "섹터 리스트", "업종 등락률" 같은 질문에 사용합니다.
    약 79개 업종이 전일대비 등락률 순으로 정렬됩니다.
    """
    sectors = await naver_list_sectors()
    if not sectors:
        return "업종 목록을 가져올 수 없습니다."

    lines = [f"업종 목록 ({len(sectors)}개):", ""]
    lines.append("업종명 | 전일대비 | 종목수 | 상승/보합/하락")
    lines.append("---|---|---|---")
    for s in sectors:
        counts = f"{s['up_count']}/{s['flat_count']}/{s['down_count']}"
        lines.append(
            f"{s['name']} | {s['change_rate']} | {s['total_count']} | {counts}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_sector_stocks(sector_name: str, count: int = 30) -> str:
    """업종종목 — 특정 업종에 속한 종목 리스트를 가져옵니다.
    "통신장비 업종 종목", "반도체 업종", "제약 섹터 종목" 같은 질문에 사용합니다.
    업종명 부분 매칭을 지원합니다.

    Args:
        sector_name: 업종명 (예: "통신장비", "반도체", "제약")
        count: 반환할 최대 종목 수 (기본 30)
    """
    count = min(count, 50)
    result = await naver_get_sector_stocks(sector_name, count=count)
    if not result.get("sector_id"):
        return (
            f"'{sector_name}' 업종을 찾을 수 없습니다. "
            f"list_sectors로 전체 업종 목록을 먼저 확인해보세요."
        )

    stocks = result["stocks"]
    lines = [f"업종: {result['sector_name']} ({len(stocks)}개 종목)", ""]
    lines.append("코드 | 종목명 | 현재가 | 등락률 | 거래량")
    lines.append("---|---|---|---|---")
    for s in stocks:
        lines.append(
            f"{s['code']} | {s['name']} | {s['price']:,} | {s['change_rate']} | {s['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_volume_ranking(market: str = "ALL", count: int = 50) -> str:
    """거래량순위 — 거래량 상위 종목을 가져옵니다.
    "거래량 많은 종목", "거래 활발한 종목", "오늘 가장 많이 거래된 종목" 같은 질문에 사용합니다.

    Args:
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL)
        count: 가져올 종목 수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    ranks = await naver_get_volume_ranking(market=market, count=count)
    if not ranks:
        return f"{market} 거래량 순위를 가져올 수 없습니다."

    lines = [f"거래량 상위 ({market}, {len(ranks)}개):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 거래량")
    lines.append("---|---|---|---|---|---")
    for i, r in enumerate(ranks, 1):
        lines.append(
            f"{i} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_change_ranking(
    direction: str = "up",
    market: str = "ALL",
    count: int = 50,
) -> str:
    """등락률순위 — 등락률 상위/하위 종목을 가져옵니다.
    "상한가 종목", "급등주", "급락주", "상승률 상위" 같은 질문에 사용합니다.

    Args:
        direction: "up"(상승률 상위) / "down"(하락률 상위)
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL)
        count: 가져올 종목 수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    ranks = await naver_get_change_ranking(direction=direction, market=market, count=count)
    if not ranks:
        return f"{direction} 등락률 순위를 가져올 수 없습니다."

    dir_label = "상승률 상위" if direction.lower() == "up" else "하락률 상위"
    lines = [f"{dir_label} ({market}, {len(ranks)}개):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 거래량")
    lines.append("---|---|---|---|---|---")
    for i, r in enumerate(ranks, 1):
        lines.append(
            f"{i} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_market_cap_ranking(market: str = "KOSPI", count: int = 50) -> str:
    """시가총액순위 — 시가총액 상위 종목을 가져옵니다.
    "대형주", "시가총액 TOP", "코스피 대장주" 같은 질문에 사용합니다.

    Args:
        market: "KOSPI" / "KOSDAQ" (기본 KOSPI, ALL 미지원)
        count: 가져올 종목 수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    ranks = await naver_get_market_cap_ranking(market=market, count=count)
    if not ranks:
        return f"{market} 시가총액 순위를 가져올 수 없습니다."

    lines = [f"시가총액 상위 ({market}, {len(ranks)}개):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 시가총액(억원)")
    lines.append("---|---|---|---|---|---")
    for r in ranks:
        lines.append(
            f"{r['rank']} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['market_cap_billion']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_multi_stocks(codes: list[str]) -> str:
    """벌크조회 — 여러 종목의 기본 정보(가격/등락률/거래량)를 한 번에 가져옵니다.
    "이 종목들 현재가 보여줘", "리스트 종목 시세 한번에" 같은 질문에 사용합니다.
    개별 get_price를 여러 번 호출하는 것보다 훨씬 토큰 효율적입니다.
    스크리닝 결과 N개 종목을 비교 분석할 때 필수 도구.

    Args:
        codes: 종목코드 리스트 (최대 30개, 예: ["005930", "000660", "005380"])
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."

    stocks = await naver_get_multi_stocks(codes)
    if not stocks:
        return "종목 정보를 가져올 수 없습니다."

    lines = [f"종목 정보 ({len(stocks)}개):", ""]
    lines.append("코드 | 종목명 | 현재가 | 전일대비 | 등락률 | 거래량")
    lines.append("---|---|---|---|---|---")
    for s in stocks:
        change = s["change"]
        change_str = f"{change:+,}" if change != 0 else "0"
        lines.append(
            f"{s['code']} | {s['name']} | {s['price']:,} | "
            f"{change_str} | {s['change_rate']} | {s['volume']:,}"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
async def get_multi_chart_stats(codes: list[str], days: int = 260) -> str:
    """차트통계벌크 — 여러 종목의 차트 통계(최고가/최저가/현재가/낙폭)를 한 번에 병렬 조회.

    ⭐ 스크리닝 필수 도구. 개별 get_chart 를 N번 호출하지 말고 이것 한 번으로 해결.

    각 종목의 지정 기간 내:
      - high/high_date: 최고가 + 그날 날짜
      - low/low_date: 최저가 + 그날 날짜
      - current_price: 현재가
      - drawdown_pct: 현재가가 최고가 대비 얼마나 내렸는지 (음수)
      - recovery_pct: 현재가가 최저가에서 얼마나 올랐는지 (양수)
      - period_return_pct: 기간 시작 대비 수익률
      - avg_volume: 평균 거래량

    활용 예시:
      - "52주 고점 대비 30% 이상 하락한 종목 찾기" → days=260, drawdown_pct < -30 필터
      - "52주 신고가 근접 종목" → days=260, drawdown_pct > -5 필터
      - "가격 박스권 횡보 종목" → drawdown_pct와 recovery_pct 모두 작은 종목

    Args:
        codes: 종목코드 리스트 (최대 100개)
        days: 과거 조회 일수 (기본 260 = 52주)
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."
    if days < 10:
        days = 10
    if days > 500:
        days = 500

    stats = await naver_get_multi_chart_stats(codes, days=days)
    if not stats:
        return "차트 통계를 가져올 수 없습니다."

    lines = [f"차트 통계 ({len(stats)}개 종목, 최근 {days}일):", ""]
    lines.append("코드 | 현재가 | 최고가(날짜) | 최저가(날짜) | 고점대비낙폭 | 기간수익률")
    lines.append("---|---|---|---|---|---")
    for s in stats:
        lines.append(
            f"{s['code']} | {s['current_price']:,} | "
            f"{s['high']:,}({s['high_date']}) | "
            f"{s['low']:,}({s['low_date']}) | "
            f"{s['drawdown_pct']:+.1f}% | "
            f"{s['period_return_pct']:+.1f}%"
        )
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
