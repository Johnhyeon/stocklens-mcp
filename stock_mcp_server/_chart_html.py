"""차트 HTML 생성 (서버 사이드 렌더링).

Claude에게 HTML 생성을 맡기면 매번 다른 스타일이 나오고,
스페이싱 규칙을 무시하는 문제가 있다. 이 모듈은 Python에서
완성된 HTML 문자열을 즉시 생성한다.

설계 원칙:
- 단일 HTML 파일 (외부 CDN 없음, 오프라인 OK)
- SVG 기반 (Canvas보다 선명, 반응형 용이)
- 캔들 75% + 거래량 22% 레이아웃
- 라이트/다크 모드 자동 감지
- 마우스 오버 툴팁
- 고점/저점 마커
- MA5/20/60 이동평균선
"""

from __future__ import annotations

import json
from datetime import datetime


def _calc_ma(closes: list[float], period: int) -> list[float | None]:
    """단순 이동평균 (None으로 시작부, 데이터 부족 시)."""
    result: list[float | None] = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            avg = sum(closes[i - period + 1 : i + 1]) / period
            result.append(round(avg, 1))
    return result


def _fmt_price(value: float | int) -> str:
    """숫자 → '1,234,000' 형태 문자열."""
    return f"{int(value):,}"


def _fmt_date(date_str: str) -> str:
    """'20260411' → '26.04.11'"""
    if len(date_str) >= 8:
        return f"{date_str[2:4]}.{date_str[4:6]}.{date_str[6:8]}"
    return date_str


def render_chart_html(
    code: str,
    name: str,
    ohlcv: list[dict],
    *,
    timeframe: str = "day",
    title: str = "",
) -> str:
    """OHLCV 데이터로 캔들 차트 HTML을 생성합니다.

    Args:
        code: 종목코드 (예: "005930")
        name: 종목명 (예: "삼성전자")
        ohlcv: [{date, open, high, low, close, volume}, ...]
               날짜 오름차순 정렬 (오래된 → 최신)
        timeframe: "day" / "week" / "month"
        title: 차트 상단 제목 (비우면 자동 생성)

    Returns:
        완성된 HTML 문자열 (단독 파일로 저장 가능)
    """
    if not ohlcv:
        raise ValueError("ohlcv 데이터가 비어 있습니다.")

    # 날짜 오름차순 정렬 보장
    rows = sorted(ohlcv, key=lambda r: r["date"])
    n = len(rows)

    # JSON으로 JavaScript에 전달
    candles_js = json.dumps(
        [
            {
                "d": r["date"],
                "o": r["open"],
                "h": r["high"],
                "l": r["low"],
                "c": r["close"],
                "v": r["volume"],
            }
            for r in rows
        ],
        ensure_ascii=False,
    )

    # 통계 계산
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    volumes = [r["volume"] for r in rows]

    high_val = max(highs)
    high_idx = highs.index(high_val)
    low_val = min(lows)
    low_idx = lows.index(low_val)
    current_price = closes[-1]
    current_open = rows[-1]["open"]
    change = current_price - closes[-2] if len(closes) >= 2 else 0
    change_pct = (change / closes[-2] * 100) if len(closes) >= 2 and closes[-2] else 0

    # MA 계산 (JavaScript에서도 하지만 요약용으로 미리 계산)
    ma20 = _calc_ma(closes, 20)
    ma60 = _calc_ma(closes, 60)

    # 제목
    tf_label = {"day": "일봉", "week": "주봉", "month": "월봉"}.get(timeframe, timeframe)
    page_title = title or f"{name}({code}) {tf_label} {n}일"

    # 지표 요약
    ma20_last = ma20[-1] if ma20[-1] is not None else current_price
    ma60_last = ma60[-1] if ma60[-1] is not None else current_price
    ma20_diff_pct = (current_price - ma20_last) / ma20_last * 100 if ma20_last else 0
    ma60_diff_pct = (current_price - ma60_last) / ma60_last * 100 if ma60_last else 0

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 색상 (한국식: 양봉 빨강, 음봉 파랑)
    # HTML 템플릿 (f-string 내부에서 중괄호 이스케이프 필요)
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
<style>
  :root {{
    --bg: #ffffff;
    --bg-panel: #f8f9fb;
    --text-primary: #0f172a;
    --text-secondary: #64748b;
    --border: #e2e8f0;
    --grid: rgba(148, 163, 184, 0.15);
    --up: #e5414a;
    --down: #3a7bd5;
    --ma5: #f59e0b;
    --ma20: #16a34a;
    --ma60: #8b5cf6;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b1120;
      --bg-panel: #111827;
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --border: #1f2937;
      --grid: rgba(148, 163, 184, 0.12);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Malgun Gothic', sans-serif;
    font-size: 13px;
    line-height: 1.5;
  }}
  .wrap {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 20px 16px;
  }}
  .header {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 18px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--border);
  }}
  .header h1 {{
    margin: 0;
    font-size: 20px;
    font-weight: 700;
  }}
  .header .code {{
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
  }}
  .header .price {{
    margin-left: auto;
    font-size: 24px;
    font-weight: 700;
  }}
  .header .change {{
    font-size: 14px;
    font-weight: 600;
  }}
  .change.up {{ color: var(--up); }}
  .change.down {{ color: var(--down); }}

  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }}
  .stat {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
  }}
  .stat-label {{
    font-size: 11px;
    color: var(--text-secondary);
    margin-bottom: 4px;
  }}
  .stat-value {{
    font-size: 14px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}

  .chart-container {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    position: relative;
  }}
  svg {{
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }}
  .legend {{
    display: flex;
    gap: 16px;
    justify-content: center;
    margin-top: 10px;
    font-size: 12px;
    color: var(--text-secondary);
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 5px;
  }}
  .legend-dot {{
    width: 10px;
    height: 2px;
    border-radius: 1px;
  }}

  .tooltip {{
    position: absolute;
    pointer-events: none;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 11px;
    font-size: 11px;
    line-height: 1.6;
    color: var(--text-primary);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    min-width: 140px;
    z-index: 10;
    display: none;
  }}
  .tooltip-row {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }}
  .tooltip-label {{
    color: var(--text-secondary);
  }}
  .tooltip-value {{
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }}

  .footer {{
    margin-top: 16px;
    font-size: 11px;
    color: var(--text-secondary);
    text-align: center;
  }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>{name}</h1>
    <span class="code">{code} · {tf_label}</span>
    <span class="price">{_fmt_price(current_price)}원</span>
    <span class="change {'up' if change >= 0 else 'down'}">
      {'+' if change >= 0 else ''}{_fmt_price(change)} ({change_pct:+.2f}%)
    </span>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">기간 최고가</div>
      <div class="stat-value" style="color:var(--up);">{_fmt_price(high_val)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">기간 최저가</div>
      <div class="stat-value" style="color:var(--down);">{_fmt_price(low_val)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">현재가 vs MA20</div>
      <div class="stat-value">{ma20_diff_pct:+.2f}%</div>
    </div>
    <div class="stat">
      <div class="stat-label">현재가 vs MA60</div>
      <div class="stat-value">{ma60_diff_pct:+.2f}%</div>
    </div>
  </div>

  <div class="chart-container">
    <svg id="chart" viewBox="0 0 900 520" preserveAspectRatio="xMidYMid meet"></svg>
    <div class="legend">
      <span class="legend-item"><span class="legend-dot" style="background:var(--up);"></span>양봉</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--down);"></span>음봉</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--ma5);"></span>MA5</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--ma20);"></span>MA20</span>
      <span class="legend-item"><span class="legend-dot" style="background:var(--ma60);"></span>MA60</span>
    </div>
    <div class="tooltip" id="tooltip"></div>
  </div>

  <div class="footer">
    네이버 증권 데이터 · 생성: {generated_at} · naver-stock-mcp
  </div>
</div>

<script>
const DATA = {candles_js};
const N = DATA.length;

// ────────────────────────────────────────
// 레이아웃 상수 (viewBox 900 × 520 기준)
// ────────────────────────────────────────
const W = 900, H = 520;
const PAD_L = 12;
const PAD_R = 72;     // 가격축 공간
const PAD_T = 14;
const PAD_B = 36;     // 날짜 공간
const GAP = 10;

const candleH = Math.round((H - PAD_T - PAD_B - GAP) * 0.76);
const volH = (H - PAD_T - PAD_B - GAP) - candleH;

const candleY0 = PAD_T;
const candleY1 = candleY0 + candleH;
const volY0 = candleY1 + GAP;
const volY1 = volY0 + volH;

const chartW = W - PAD_L - PAD_R;
const slotW = chartW / N;
const bodyW = Math.max(1, slotW - 1);  // ← 핵심: step - 1 공식

// ────────────────────────────────────────
// 이동평균 계산
// ────────────────────────────────────────
function ma(period) {{
  return DATA.map((_, i) => {{
    if (i < period - 1) return null;
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += DATA[j].c;
    return sum / period;
  }});
}}
const MA5 = ma(5);
const MA20 = ma(20);
const MA60 = ma(60);

// ────────────────────────────────────────
// 가격/거래량 범위
// ────────────────────────────────────────
const pMax = Math.max(...DATA.map(d => d.h)) * 1.015;
const pMin = Math.min(...DATA.map(d => d.l)) * 0.985;
const pRange = pMax - pMin || 1;
const vMax = Math.max(...DATA.map(d => d.v));

function px(i) {{ return PAD_L + (i + 0.5) * slotW; }}
function py(p) {{ return candleY0 + ((pMax - p) / pRange) * candleH; }}
function vy(v) {{ return volY1 - (v / vMax) * volH; }}

// ────────────────────────────────────────
// SVG 렌더링
// ────────────────────────────────────────
const svg = document.getElementById('chart');
const SVG_NS = 'http://www.w3.org/2000/svg';

function el(tag, attrs) {{
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}}
function getColor(cssVar) {{
  return getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
}}

function render() {{
  svg.innerHTML = '';
  const UP = getColor('--up'), DN = getColor('--down');
  const GRID = getColor('--grid'), SUB = getColor('--text-secondary');
  const MA5_C = getColor('--ma5'), MA20_C = getColor('--ma20'), MA60_C = getColor('--ma60');

  // 배경 패널
  svg.appendChild(el('rect', {{x: PAD_L, y: candleY0, width: chartW, height: candleH, fill: 'none'}}));

  // ─── 가격 그리드 (5칸) ───
  for (let i = 0; i <= 4; i++) {{
    const p = pMin + (pRange * i / 4);
    const y = py(p);
    svg.appendChild(el('line', {{
      x1: PAD_L, x2: PAD_L + chartW, y1: y, y2: y,
      stroke: GRID, 'stroke-width': 0.5,
    }}));
    // 가격 라벨 (오른쪽)
    const label = el('text', {{
      x: PAD_L + chartW + 6, y: y + 4,
      'font-size': 10, fill: SUB,
    }});
    label.textContent = Math.round(p).toLocaleString();
    svg.appendChild(label);
  }}

  // ─── 거래량 그리드 (2칸) ───
  for (let i = 1; i <= 2; i++) {{
    const y = volY1 - (volH * i / 3);
    svg.appendChild(el('line', {{
      x1: PAD_L, x2: PAD_L + chartW, y1: y, y2: y,
      stroke: GRID, 'stroke-width': 0.5,
    }}));
  }}

  // ─── 캔들 렌더링 ───
  DATA.forEach((c, i) => {{
    const x = px(i);
    const up = c.c >= c.o;
    const color = up ? UP : DN;

    // 심지
    svg.appendChild(el('line', {{
      x1: x, x2: x, y1: py(c.h), y2: py(c.l),
      stroke: color, 'stroke-width': 1,
    }}));

    // 몸통
    const bodyTop = py(Math.max(c.o, c.c));
    const bodyHpx = Math.max(1, Math.abs(py(c.o) - py(c.c)));
    svg.appendChild(el('rect', {{
      x: x - bodyW / 2, y: bodyTop,
      width: bodyW, height: bodyHpx,
      fill: color,
    }}));
  }});

  // ─── 이동평균선 ───
  function drawMA(arr, color) {{
    let d = '';
    let started = false;
    arr.forEach((v, i) => {{
      if (v === null) return;
      const x = px(i), y = py(v);
      d += (started ? ' L ' : 'M ') + x + ' ' + y;
      started = true;
    }});
    if (d) {{
      svg.appendChild(el('path', {{
        d: d, stroke: color, 'stroke-width': 1.3, fill: 'none',
      }}));
    }}
  }}
  drawMA(MA5, MA5_C);
  drawMA(MA20, MA20_C);
  drawMA(MA60, MA60_C);

  // ─── 거래량 바 ───
  DATA.forEach((c, i) => {{
    const x = px(i);
    const up = c.c >= c.o;
    const color = up ? UP : DN;
    const top = vy(c.v);
    svg.appendChild(el('rect', {{
      x: x - bodyW / 2, y: top,
      width: bodyW, height: volY1 - top,
      fill: color, opacity: 0.75,
    }}));
  }});

  // ─── 날짜 라벨 (5개 정도) ───
  const labelCount = Math.min(6, N);
  const step = Math.max(1, Math.floor(N / labelCount));
  for (let i = 0; i < N; i += step) {{
    if (i === 0 || i === N - 1 || (i % step === 0 && N - i >= step / 2)) {{
      const d = DATA[i].d;
      const dateLabel = d.length >= 8 ? d.slice(2,4) + '.' + d.slice(4,6) + '.' + d.slice(6,8) : d;
      const text = el('text', {{
        x: px(i), y: volY1 + 20,
        'font-size': 10, fill: SUB,
        'text-anchor': 'middle',
      }});
      text.textContent = dateLabel;
      svg.appendChild(text);
    }}
  }}

  // ─── 고점/저점 마커 ───
  function marker(idx, label, color, above) {{
    const d = DATA[idx];
    const x = px(idx);
    const y = above ? py(d.h) - 6 : py(d.l) + 14;
    const text = el('text', {{
      x: x, y: y,
      'font-size': 9.5, fill: color, 'font-weight': 'bold',
      'text-anchor': 'middle',
    }});
    text.textContent = label;
    svg.appendChild(text);
  }}
  marker({high_idx}, '▲ 최고', UP, true);
  marker({low_idx}, '▼ 최저', DN, false);

  // ─── 마우스 이벤트 (툴팁) ───
  const tooltip = document.getElementById('tooltip');
  svg.addEventListener('mousemove', (e) => {{
    const rect = svg.getBoundingClientRect();
    const sx = (e.clientX - rect.left) * (W / rect.width);
    if (sx < PAD_L || sx > PAD_L + chartW) {{
      tooltip.style.display = 'none';
      return;
    }}
    const idx = Math.floor((sx - PAD_L) / slotW);
    if (idx < 0 || idx >= N) return;
    const c = DATA[idx];
    const up = c.c >= c.o;
    const diff = c.c - c.o;
    const diffPct = (diff / c.o * 100).toFixed(2);
    const d = c.d.length >= 8 ? c.d.slice(0,4) + '.' + c.d.slice(4,6) + '.' + c.d.slice(6,8) : c.d;

    tooltip.innerHTML = `
      <div style="font-weight:700;margin-bottom:5px;">${{d}}</div>
      <div class="tooltip-row"><span class="tooltip-label">시가</span><span class="tooltip-value">${{c.o.toLocaleString()}}</span></div>
      <div class="tooltip-row"><span class="tooltip-label">고가</span><span class="tooltip-value">${{c.h.toLocaleString()}}</span></div>
      <div class="tooltip-row"><span class="tooltip-label">저가</span><span class="tooltip-value">${{c.l.toLocaleString()}}</span></div>
      <div class="tooltip-row"><span class="tooltip-label">종가</span><span class="tooltip-value" style="color:${{up ? 'var(--up)' : 'var(--down)'}};">${{c.c.toLocaleString()}} (${{up ? '+' : ''}}${{diffPct}}%)</span></div>
      <div class="tooltip-row"><span class="tooltip-label">거래량</span><span class="tooltip-value">${{c.v.toLocaleString()}}</span></div>
    `;
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX - rect.left + 14) + 'px';
    tooltip.style.top = (e.clientY - rect.top + 10) + 'px';
  }});
  svg.addEventListener('mouseleave', () => {{
    tooltip.style.display = 'none';
  }});
}}

render();
window.addEventListener('resize', render);
</script>
</body>
</html>
"""
    return html
