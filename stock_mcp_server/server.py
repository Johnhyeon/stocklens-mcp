"""한국 주식 데이터 MCP 서버.

네이버 증권에서 차트, 수급, 재무 데이터를 가져와
Claude에서 자연어로 분석할 수 있게 해줍니다.
"""

import functools
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP
from stock_mcp_server.naver import (
    search_stock as naver_search_stock,
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
    scan_stocks_to_snapshot as naver_scan_snapshot,
)
from stock_mcp_server._excel import (
    get_snapshot_dir,
    generate_filename,
    save_dataframe_to_excel,
    load_excel,
    apply_filters,
)
from stock_mcp_server._metrics import (
    track_metrics,
    load_metrics,
    summarize_metrics,
    get_metrics_file,
)
from stock_mcp_server._indicators import (
    compute_indicators,
    AVAILABLE_INDICATORS,
)
import asyncio
import json
import pandas as pd


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
    "StockLens",
    instructions="""StockLens — 한국 주식 데이터를 네이버 증권에서 실시간 조회합니다.
종목코드(예: 005930)나 종목명(예: 삼성전자)으로 검색할 수 있습니다.
차트 데이터, 투자자 수급, 재무지표, 시장 지수를 제공합니다.

## 🚨 종목코드 규칙 (절대 원칙)

사용자가 6자리 코드가 아닌 **종목명**을 주면, **반드시 아래 순서를 지켜라**:

1. 먼저 `search` 또는 `search_stock` 도구로 종목명 조회
2. 검색 결과가 여러 개면 사용자에게 확인 요청
3. 검색 결과 없으면 "종목명을 찾을 수 없습니다. 6자리 코드를 알려주세요" 요청
4. **종목 코드 추측 절대 금지** — 학습 지식으로 "아마 XXXXXX일 것" 식 추론 금지
5. 종목 코드는 상장폐지·합병·재할당으로 바뀔 수 있음. 반드시 실시간 조회로 검증.

이 규칙을 어기면 잘못된 종목 분석으로 사용자를 오도할 수 있다.

## 차트 시각화 규칙

### 🚨 주가 시계열 차트 **선행 조건** (절대 원칙)

가격 시계열을 차트로 렌더링할 때 **반드시 `get_chart` 도구 호출**로 OHLCV 원본 수집.

**금지 패턴:**
- `get_flow`의 [참고] 종가·거래량 컬럼을 차트 소스로 전용 금지
- `get_multi_chart_stats`의 집계값으로 시계열 차트 대체 금지
- `get_indicators`의 `candle` 키 (최신 1봉만)로 시계열 차트 구성 금지
- `get_price`의 스냅샷으로 차트 구성 금지

**올바른 패턴:**
- 일봉 차트 → `get_chart(code, "day", count=120~260)`
- 주봉 차트 → `get_chart(code, "week", count=52~150)`
- 월봉 차트 → `get_chart(code, "month", count=24~60)`

OHL 없이 close만으로 line chart 그리지 말 것. 원본 OHLCV가 없으면 차트 포기하고 텍스트 분석으로 대체하는 것이 허구 차트보다 나음.

### 기본 기간
- **차트는 반드시 120일 이상 불러와서 그린다.** 60일 같은 짧은 기간으로 호출하지 말 것.
- `get_chart` 호출 시 `count` 파라미터를 명시적으로 120 이상으로 설정할 것.
- 사용자가 "3개월" 같이 짧은 기간을 요청해도 최소 120일 데이터는 불러와야 기술적 분석이 의미 있음.
- 최소 권장: 120, 기본 권장: 120~180, 상한: 500.

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

### 캔들 스페이싱 (⚠️ 반드시 준수)

**이 공식을 무조건 사용할 것. 다른 공식 쓰지 말 것:**

```javascript
const availableW = W - PAD_L - PAD_R;
const step = availableW / candles.length;
const bodyW = Math.max(1, step - 1);
// 거래량 바도 동일한 bodyW 사용
```

**절대 쓰면 안 되는 패턴 (Claude가 자주 실수하는 것):**

```javascript
// ❌ 금지 1: 비율 기반 — 간격이 너무 벌어져서 이질감 발생
const bW = sw * 0.7;
const bW = sw * 0.62;
const bW = step * 0.8;

// ❌ 금지 2: 최소값 강제 — step이 작을 때 캔들 겹침
const bW = Math.max(3, sw * 0.62);
const bW = Math.max(2, ...);

// ❌ 금지 3: 고정값
const CANDLE_W = 100;
const bW = 12;
```

**반드시 써야 하는 패턴:**

```javascript
// ✅ 올바른 공식 — 이것만 사용
const bodyW = Math.max(1, step - 1);
```

**이 공식의 의미:**
- `step - 1`: 캔들 간 1px 공백 (양쪽 0.5px씩)
- `Math.max(1, ...)`: step이 1px 이하일 때도 최소 1px 보장
- 비율이 아니라 **절대값 1px 공백**이 핵심

**예시 값:**
- 120일 일봉 + 폭 800px → step ≈ 6px, bodyW ≈ 5px (자연스러움)
- 60일 일봉 + 폭 800px → step ≈ 12px, bodyW ≈ 11px

### 전체 캔들 차트 렌더링 참고 코드

```javascript
// 1. 가용 공간 계산
const availableW = W - PAD_L - PAD_R;
const step = availableW / candles.length;
const bodyW = Math.max(1, step - 1);   // ← 이 공식 그대로 사용
const wickW = 1;                        // 심지는 1px 고정

// 2. 캔들 그리기
candles.forEach((c, i) => {
  const cx = PAD_L + (i + 0.5) * step;
  const up = c.close >= c.open;
  const color = up ? '#E24B4A' : '#1D9E75';  // 한국식: 양봉 빨강, 음봉 파랑

  // 심지 (high-low)
  line(cx, py(c.high), cx, py(c.low), strokeWidth=wickW, stroke=color);

  // 몸통
  const bodyTop = py(Math.max(c.open, c.close));
  const bodyH = Math.max(1, Math.abs(py(c.open) - py(c.close)));
  rect(cx - bodyW/2, bodyTop, bodyW, bodyH, fill=color);
});

// 3. 거래량 패널 (필수, 같은 bodyW 사용)
const maxVol = Math.max(...candles.map(c => c.volume));
candles.forEach((c, i) => {
  const cx = PAD_L + (i + 0.5) * step;
  const up = c.close >= c.open;
  const color = up ? '#E24B4A' : '#1D9E75';
  const h = (c.volume / maxVol) * (volPanelH - 4);
  rect(cx - bodyW/2, volPanelBottom - h, bodyW, h, fill=color);
});
```
""",
)


async def _search_impl(query: str) -> str:
    """공통 구현 — search와 search_stock 도구에서 공유."""
    results = await naver_search_stock(query)
    if not results:
        return f"'{query}'에 대한 검색 결과가 없습니다. 6자리 코드를 직접 알려주세요."

    lines = [f"검색 결과 ({len(results)}건):"]
    for r in results:
        market = r.get("market", "")
        suffix = f" [{market}]" if market else ""
        lines.append(f"  - {r['name']} ({r['code']}){suffix}")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("search")
async def search(query: str) -> str:
    """종목검색 — 종목명 또는 종목코드로 한국 주식 종목을 조회합니다.
    "삼성전자 종목코드", "반도체 관련주", "005930 뭐야" 같은 질문에 사용합니다.

    ⚠️ 사용자가 종목명만 주고 코드를 모를 때 **반드시 이 도구를 먼저 호출**.
    종목 코드를 추측하지 말 것. 상장폐지·재할당으로 코드가 바뀔 수 있음.

    search_stock으로도 동일하게 호출 가능 (별명).

    Args:
        query: 검색할 종목명 또는 코드 (예: "삼성전자", "005930", "알멕")
    """
    return await _search_impl(query)


@mcp.tool()
@safe_tool
@track_metrics("search_stock")
async def search_stock(query: str) -> str:
    """종목코드조회 (stock lookup) — 한국 주식 종목명/코드 조회 전용 도구.

    `search`와 동일 기능. 도구 디스커버리에서 "stock"/"ticker"/"종목" 키워드로
    빠르게 매칭되도록 명확한 이름을 갖는 별명입니다.

    ⚠️ 종목명만 있고 6자리 코드를 모를 때 **이 도구를 먼저 호출**해야 합니다.
    코드 추측(guessing) 금지. 다른 도구(get_price, get_chart 등)에 잘못된 코드를
    넣으면 엉뚱한 종목이 조회됩니다.

    Args:
        query: 종목명(한/영) 또는 6자리 코드. 예: "알멕", "Samsung", "005930"

    Returns:
        매칭된 종목 리스트. 여러 개면 사용자에게 확인 요청 필요.
    """
    return await _search_impl(query)


@mcp.tool()
@safe_tool
@track_metrics("get_chart")
async def get_chart(code: str, timeframe: str = "day", count: int = 120) -> str:
    """캔들차트 OHLCV — 종목의 시계열 캔들 데이터(시가/고가/저가/종가/거래량, candlestick OHLCV).
    "삼성전자 일봉", "차트 보여줘", "3개월 주봉", "월봉 데이터", "캔들 시각화",
    "candlestick chart", "price history" 같은 질문에 사용합니다.

    ⭐ **차트 시각화의 유일한 진입점.** 아티팩트/플롯으로 차트를 그릴 땐 이 도구만 사용하고,
    **캔들 + 거래량만 렌더링**하세요. 지지·저항은 캔들 위에서 눈으로 판단합니다.
    이평선·RSI·MACD·볼린저 등 보조지표는 **사용자가 명시적으로 요청할 때만** 추가하고,
    그때도 get_indicators로 숫자만 받아 텍스트로 요약 — 한 차트에 여러 지표를 선으로
    얹어 "덕지덕지" 만들지 말 것.

    **기본값 120일 (약 6개월 거래일)**.

    Args:
        code: 종목코드 6자리 (예: "005930")
        timeframe: "day"(일봉), "week"(주봉), "month"(월봉)
        count: 가져올 봉 개수 (기본 120, 최대 500)
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
@track_metrics("get_price")
async def get_price(code: str) -> str:
    """현재가 — 종목의 현재 시세 스냅샷 (오늘 하루치 OHLC + 거래량).
    "삼성전자 지금 얼마", "현재가", "오늘 시세", "주가 알려줘" 같은 질문에 사용합니다.

    ⚠️ **단일 시점 스냅샷**. 과거 시계열 아님. 차트/히스토리 필요 시 get_chart 사용.

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
@track_metrics("get_flow")
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
    lines.append("날짜 | [주] 기관 순매매 | [주] 외국인 순매매 | [참고] 종가 | [참고] 거래량")
    lines.append("---|---|---|---|---")
    for row in data:
        lines.append(
            f"{row['date']} | {row['institutional']:,} | {row['foreign']:,} | "
            f"{row['close']:,} | {row['volume']:,}"
        )

    # 합계
    total_inst = sum(r["institutional"] for r in data)
    total_frgn = sum(r["foreign"] for r in data)
    lines.append("")
    lines.append(f"합계 | {total_inst:,} | {total_frgn:,} | - | -")
    lines.append("")
    lines.append(
        "※ [주] 필드는 이 도구의 주 목적 (수급 분석). "
        "[참고] 종가·거래량은 편의 제공이며, **가격 차트·시계열 분석 소스로 사용 금지**. "
        "차트는 get_chart, 현재가는 get_price 사용."
    )

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_financial")
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
@track_metrics("get_index")
async def get_index() -> str:
    """시장지수 — KOSPI, KOSDAQ 지수 현재값을 가져옵니다.
    "코스피", "코스닥", "시장 지수", "오늘 시장 어때" 같은 질문에 사용합니다.
    """
    data = await get_market_index()
    if not data:
        return "시장 지수를 가져올 수 없습니다."

    lines = ["시장 지수:"]
    for item in data:
        value = item.get("value", "-")
        if isinstance(value, float):
            value = f"{value:,.2f}"
        change = item.get("change_raw") or ""
        rate = item.get("change_rate")
        rate_str = f" ({rate:+.2f}%)" if rate is not None else ""
        lines.append(f"  {item['index']}: {value}{rate_str} {change}".rstrip())
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("list_themes")
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
@track_metrics("get_theme_stocks")
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
@track_metrics("list_sectors")
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
@track_metrics("get_sector_stocks")
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
@track_metrics("get_volume_ranking")
async def get_volume_ranking(
    market: str = "ALL",
    count: int = 50,
    sort_by: str = "volume",
) -> str:
    """거래량/거래대금 순위 — 상위 종목을 가져옵니다.

    "거래량 많은 종목" → sort_by="volume" (기본, 주수 기준)
    "거래대금 많은 종목"/"거래 규모 큰 종목" → sort_by="trade_value" (원 기준)

    대형 고단가 종목(삼전·하이닉스 등)은 거래량(주수)이 작아도 거래대금은 클 수 있어
    스크리닝 시에는 sort_by="trade_value"가 더 적합한 경우가 많습니다.

    Args:
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL)
        count: 가져올 종목 수 (기본 50, 최대 500)
        sort_by: "volume"(거래량 주수) / "trade_value"(거래대금 원)
    """
    count = min(count, 500)
    ranks = await naver_get_volume_ranking(market=market, count=count, sort_by=sort_by)
    if not ranks:
        return f"{market} 거래량 순위를 가져올 수 없습니다."

    sort_label = "거래대금" if sort_by == "trade_value" else "거래량"
    lines = [f"{sort_label} 상위 ({market}, {len(ranks)}개, 정렬={sort_by}):", ""]
    lines.append("순위 | 코드 | 종목명 | 현재가 | 등락률 | 거래량(주) | 거래대금(원)")
    lines.append("---|---|---|---|---|---|---")
    for r in ranks:
        tv = r.get("trade_value_krw", 0)
        # 거래대금 억원 단위 표시 (가독성)
        tv_billion = tv / 100_000_000
        lines.append(
            f"{r['rank']} | {r['code']} | {r['name']} | {r['price']:,} | "
            f"{r['change_rate']} | {r['volume']:,} | {tv_billion:,.1f}억"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_change_ranking")
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
@track_metrics("get_market_cap_ranking")
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
@track_metrics("get_multi_stocks")
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
@track_metrics("get_multi_chart_stats")
async def get_multi_chart_stats(codes: list[str], days: int = 260) -> str:
    """차트통계벌크 — 여러 종목의 **집계 통계**(최고가/최저가/현재가/낙폭)를 한 번에 병렬 조회.

    ⚠️ **시계열 도구 아님.** 기간 내 요약값(집계)만 반환. 캔들 차트·시계열 시각화는
    get_chart 별도 호출 필수. 이름에 "차트"가 들어 있지만 시계열 OHLCV를 주지 않음.

    ⭐ 스크리닝 필수 도구. 개별 get_chart 를 N번 호출하지 말고 이것 한 번으로 해결.

    각 종목의 지정 기간 내 (모두 집계값):
      - high/high_date: 최고가 + 그날 날짜
      - low/low_date: 최저가 + 그날 날짜
      - current_price: 현재가 (오늘 종가)
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

    lines = [f"차트 통계 ({len(stats)}개 종목, 최근 {days}일 집계):", ""]
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
    lines.append("")
    lines.append(
        "※ 기간 **집계값**만 반환. 시계열 OHLCV 아님. "
        "**캔들 차트·시계열 시각화는 get_chart 별도 호출** 필수."
    )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_indicators")
async def get_indicators(
    code: str,
    days: int = 260,
    include: list[str] | None = None,
    timeframe: str = "day",
) -> str:
    """기술지표 — 단일 종목의 이평선·RSI·MACD·볼린저·스토캐스틱 등 종합 판정.

    ⚠️ **스크리닝·판정 전용 도구. 차트 시각화용 아님.**
    시각화(아티팩트/플롯)는 `get_chart`로 캔들+거래량만 그리고, 지지·저항은
    캔들 위에서 눈으로 판단하면 됩니다. 이 도구는 플레이북 조건 필터·상태 판정처럼
    **숫자 비교가 필요할 때만** 호출하세요.

    OHLCV 원본 대신 계산·판정 결과만 JSON으로 반환해 토큰을 절약합니다.

    Args:
        code: 종목코드 (예: "005930")
        days: 조회 일수 (기본 260, 최소 30, 최대 500)
        include: 계산할 지표 키 리스트. 기본 ["ma", "ma_phase", "volume", "candle"].
            스냅샷 지표: ma / ma_phase / ma_slope / ma_cross / rsi / macd / bollinger /
                       stochastic / obv / volume / position / candle
            구조 분석: support_resistance / volume_profile / price_channel
            (구조 분석은 days=500~750 등 긴 lookback 권장)
        timeframe: "day"(일봉) / "week"(주봉) / "month"(월봉). 분봉은 현재 미지원.

    ma_phase 값:
        0 완전역배열 / 1 단기상승꼬임 / 2 꼬임 / 3 단기하락꼬임 / 4 완전정배열

    support_resistance 반환:
        피벗 자동 추출 + 클러스터링 + 터치 횟수·일자·강도(weak/medium/strong).
        2~3년치 일봉 권장. 근거 없는 S/R 추정 제거 목적.

    volume_profile 반환:
        가격대별 누적 거래량, POC(최대 매물 집중), Value Area(70% 구간).

    price_channel 반환:
        Donchian 채널. Upper=N봉 고가, Lower=N봉 저가, 현재 위치 %.

    반환: JSON 문자열.
    """
    if not code:
        return "종목코드가 필요합니다."
    if include is None:
        include = ["ma", "ma_phase", "volume", "candle"]
    unknown = [k for k in include if k not in AVAILABLE_INDICATORS]
    if unknown:
        return (
            f"지원하지 않는 지표: {unknown}\n"
            f"사용 가능: {AVAILABLE_INDICATORS}"
        )
    days = max(30, min(days, 500))

    ohlcv = await get_ohlcv(code, timeframe=timeframe, count=days)
    if not ohlcv:
        return f"차트 데이터를 가져올 수 없습니다: {code}"

    result = compute_indicators(ohlcv, include)
    payload = {
        "code": code,
        "timeframe": timeframe,
        "days": days,
        "indicators": result,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
@safe_tool
@track_metrics("get_indicators_bulk")
async def get_indicators_bulk(
    codes: list[str],
    days: int = 260,
    include: list[str] | None = None,
    timeframe: str = "day",
) -> str:
    """기술지표벌크 — 여러 종목의 지표를 병렬 계산. 스크리닝 핵심 도구.

    ⚠️ **시계열/캔들 차트 아님.** 집계 판정값만 반환. 캔들 시각화는 get_chart 사용.

    최대 100개 종목의 이평선 Phase·RSI·MACD 등을 한 번에 판정합니다.
    get_indicators를 N번 부르지 말고 이걸로 일괄 처리.

    Args:
        codes: 종목코드 리스트 (최대 100개)
        days: 조회 일수 (기본 260)
        include: 지표 키 리스트 (기본 ["ma_phase", "volume"]). get_indicators 참조.
        timeframe: "day" / "week" / "month"

    반환: 코드별 지표 결과 JSON.
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."
    if include is None:
        include = ["ma_phase", "volume"]
    unknown = [k for k in include if k not in AVAILABLE_INDICATORS]
    if unknown:
        return (
            f"지원하지 않는 지표: {unknown}\n"
            f"사용 가능: {AVAILABLE_INDICATORS}"
        )
    codes = codes[:100]
    days = max(30, min(days, 500))

    async def one(code: str) -> tuple[str, dict]:
        try:
            ohlcv = await get_ohlcv(code, timeframe=timeframe, count=days)
            if not ohlcv:
                return code, {"error": "OHLCV 없음"}
            return code, compute_indicators(ohlcv, include)
        except Exception as e:
            return code, {"error": f"{type(e).__name__}: {e}"}

    results = await asyncio.gather(*[one(c) for c in codes])
    payload = {
        "timeframe": timeframe,
        "days": days,
        "include": include,
        "count": len(results),
        "results": {code: data for code, data in results},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
@safe_tool
@track_metrics("export_to_excel")
async def export_to_excel(
    data_type: str,
    code: str = "",
    days: int = 180,
    filename: str = "",
) -> str:
    """엑셀내보내기 — 단일 종목의 데이터를 Excel 파일로 저장합니다.

    Gemini/GPT 같은 다른 AI에 파일 업로드로 넘기거나,
    엑셀에서 직접 분석/차트 작성할 때 사용합니다.

    Args:
        data_type: "chart"(일봉 OHLCV) / "flow"(투자자별 수급) / "financial"(재무지표)
        code: 종목코드 6자리 (예: "005930")
        days: chart/flow의 경우 과거 일수 (기본 180)
        filename: 파일명 (비우면 자동 생성)

    Returns:
        저장된 파일 경로
    """
    if not code:
        return "종목코드가 필요합니다."

    if data_type == "chart":
        data = await get_ohlcv(code, "day", days)
        if not data:
            return f"차트 데이터를 가져올 수 없습니다: {code}"
        df = pd.DataFrame(data)
        prefix = f"chart_{code}"
        sheet = "OHLCV"

    elif data_type == "flow":
        data = await get_investor_flow(code, days)
        if not data:
            return f"수급 데이터를 가져올 수 없습니다: {code}"
        df = pd.DataFrame(data)
        prefix = f"flow_{code}"
        sheet = "Investor Flow"

    elif data_type == "financial":
        data = await get_financials(code)
        if not data:
            return f"재무지표를 가져올 수 없습니다: {code}"
        # dict → DataFrame 변환
        rows = [{"항목": k, "값": str(v)} for k, v in data.items() if k not in ("code",)]
        df = pd.DataFrame(rows)
        prefix = f"financial_{code}"
        sheet = "Financial"

    else:
        return f"지원하지 않는 data_type: {data_type}. 'chart', 'flow', 'financial' 중 선택."

    fname = filename or generate_filename(prefix)
    if not fname.endswith(".xlsx"):
        fname += ".xlsx"
    file_path = get_snapshot_dir() / fname
    saved = save_dataframe_to_excel(df, file_path, sheet_name=sheet)

    return (
        f"✓ Excel 파일 저장 완료\n"
        f"경로: {saved}\n"
        f"행 수: {len(df)}\n"
        f"컬럼: {', '.join(df.columns)}\n\n"
        f"💡 이 파일을 Gemini/ChatGPT에 업로드하면 다른 AI에서도 분석할 수 있어요."
    )


@mcp.tool()
@safe_tool
@track_metrics("scan_to_excel")
async def scan_to_excel(
    codes: list[str],
    days: int = 260,
    include_financial: bool = True,
    filename: str = "",
) -> str:
    """시장스캔 — 여러 종목의 기본정보+차트통계+재무지표를 한 번에 수집해 Excel로 저장.

    ⭐ 로컬 캐시 패턴: 한 번 스캔해두면 이후 query_excel로 즉시 반복 조회 가능.
    DB 없이도 HTS 같은 빠른 분석 경험을 제공합니다.

    사용 흐름:
      1. scan_to_excel(KOSPI 시총 100개 코드) → 파일 저장 (한 번)
      2. query_excel(파일경로, 조건) → 즉시 필터링 (반복)
      3. query_excel(파일경로, 다른 조건) → 즉시

    Args:
        codes: 종목코드 리스트 (최대 500개)
        days: 차트 통계 과거 일수 (기본 260 = 52주)
        include_financial: 재무지표(PER/PBR) 포함 여부
        filename: 파일명 (비우면 자동 생성)

    Returns:
        저장된 파일 경로 + 컬럼 목록
    """
    if not codes:
        return "종목코드 리스트가 비어 있습니다."

    rows = await naver_scan_snapshot(codes, days=days, include_financial=include_financial)
    if not rows:
        return "데이터를 수집하지 못했습니다."

    df = pd.DataFrame(rows)
    fname = filename or generate_filename(f"snapshot_{len(df)}stocks")
    if not fname.endswith(".xlsx"):
        fname += ".xlsx"
    file_path = get_snapshot_dir() / fname

    saved = save_dataframe_to_excel(
        df,
        file_path,
        sheet_name="Snapshot",
        metadata={
            "days": days,
            "include_financial": include_financial,
            "requested_codes": len(codes),
        },
    )

    return (
        f"✓ 스냅샷 저장 완료 ({len(df)}개 종목)\n"
        f"경로: {saved}\n"
        f"컬럼: {', '.join(df.columns)}\n\n"
        f"💡 이 파일을 query_excel 도구로 조건 필터링하면 즉시 분석 가능합니다.\n"
        f"예시: query_excel(file_path='{saved}', filters={{'per_max': 10, 'drawdown_pct_max': -30}})"
    )


@mcp.tool()
@safe_tool
@track_metrics("query_excel")
async def query_excel(
    file_path: str,
    filters: dict | None = None,
    sort_by: str = "",
    descending: bool = True,
    limit: int = 30,
) -> str:
    """엑셀쿼리 — 저장된 Excel 스냅샷에서 조건에 맞는 종목을 즉시 필터링.

    scan_to_excel로 만든 파일을 조회할 때 사용. HTTP 호출 없이 로컬 파일에서
    필터링하므로 매우 빠릅니다. 같은 파일에 여러 조건을 번갈아 쿼리 가능.

    필터 형식 (두 가지 다 지원):
        간단: {"per_max": 10, "pbr_max": 1.5, "drawdown_pct_max": -30}
        상세: {"per": {"max": 10, "min": 0}, "drawdown_pct": {"max": -30}}

    Args:
        file_path: scan_to_excel로 만든 파일 경로
        filters: 필터 조건 (컬럼명_max / 컬럼명_min 형식)
        sort_by: 정렬 기준 컬럼 (예: "market_cap", "drawdown_pct")
        descending: 내림차순 여부 (기본 True)
        limit: 반환 최대 개수 (기본 30)

    Returns:
        필터링된 종목 리스트 (마크다운 테이블)
    """
    try:
        df = load_excel(file_path, sheet_name="Snapshot")
    except FileNotFoundError:
        return f"파일을 찾을 수 없습니다: {file_path}"
    except Exception as e:
        return f"파일 로드 실패: {type(e).__name__}: {e}"

    if filters:
        df = apply_filters(df, filters)

    if sort_by and sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=not descending)

    df = df.head(limit)

    if df.empty:
        return f"조건에 맞는 종목이 없습니다. (원본: {file_path})"

    # 주요 컬럼만 표시
    display_cols = [
        c for c in ["code", "name", "current_price", "drawdown_pct", "per", "pbr", "volume"]
        if c in df.columns
    ]
    if display_cols:
        df_display = df[display_cols]
    else:
        df_display = df

    lines = [f"쿼리 결과 ({len(df)}개 종목, 파일: {Path(file_path).name}):", ""]
    lines.append("| " + " | ".join(df_display.columns) + " |")
    lines.append("|" + "|".join(["---"] * len(df_display.columns)) + "|")
    for _, row in df_display.iterrows():
        values = []
        for col in df_display.columns:
            v = row[col]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if col in ("current_price", "volume", "avg_volume", "high", "low"):
                    values.append(f"{int(v):,}")
                elif col.endswith("_pct"):
                    values.append(f"{v:+.2f}%")
                else:
                    values.append(f"{v:.2f}" if isinstance(v, float) else str(v))
            else:
                values.append(str(v))
        lines.append("| " + " | ".join(values) + " |")

    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_metrics_summary")
async def get_metrics_summary(days: int = 1) -> str:
    """사용량통계 — 최근 N일간 MCP 도구 사용량을 집계해서 보여줍니다.

    디버깅/최적화용. 도구별로:
    - 호출 횟수
    - 평균/p50/p95 실행 시간
    - 평균 토큰 소모량
    - 캐시 히트율
    - 에러 발생 횟수
    를 보여줍니다.

    로그 파일 위치: ~/Downloads/kstock/logs/metrics_YYYYMMDD.jsonl

    Args:
        days: 조회할 일수 (기본 1, 오늘만. 최대 30)
    """
    days = max(1, min(days, 30))

    records = load_metrics(days=days)
    if not records:
        return (
            f"최근 {days}일간 기록이 없습니다.\n"
            f"로그 파일: {get_metrics_file()}"
        )

    summary = summarize_metrics(records)

    sorted_tools = sorted(
        summary.items(),
        key=lambda x: x[1]["call_count"],
        reverse=True,
    )

    lines = [
        f"📊 MCP 사용량 통계 (최근 {days}일, 총 {len(records)}회 호출)",
        "",
        "도구 | 호출 | 평균시간 | p95시간 | 평균토큰 | 캐시율 | 에러",
        "---|---|---|---|---|---|---",
    ]

    total_tokens = 0
    total_calls = 0

    for tool, s in sorted_tools:
        total_tokens += s["total_output_tokens"]
        total_calls += s["call_count"]
        lines.append(
            f"{tool} | "
            f"{s['call_count']} | "
            f"{s['avg_duration_ms']}ms | "
            f"{s['p95_duration_ms']}ms | "
            f"{s['avg_tokens']:,} | "
            f"{s['cache_hit_rate']}% | "
            f"{s['errors']}"
        )

    lines.append("")
    lines.append(f"**총 소모 토큰: {total_tokens:,}**")
    lines.append(f"**총 호출: {total_calls}회**")
    lines.append(f"**평균 호출당 토큰: {total_tokens // max(total_calls, 1):,}**")
    lines.append("")
    lines.append(f"로그 파일: {get_metrics_file()}")

    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
