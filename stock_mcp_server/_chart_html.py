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

import base64
import json
from datetime import datetime

import pandas as pd

from stock_mcp_server._indicators import compute_support_resistance, compute_indicators


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


def _compute_sr_for_chart(ohlcv: list[dict]) -> list[dict]:
    """S/R 영역을 차트에 표시하기 위한 단순화된 레벨 리스트.

    compute_support_resistance 결과를 차트 오버레이용 포맷으로 가공.
    반환: [{"low": N, "high": N, "kind": "support"|"resistance", "strength": "weak"|"medium"|"strong", "touches": N, "label": ""}, ...]
    """
    if not ohlcv or len(ohlcv) < 30:
        return []
    df = pd.DataFrame(ohlcv).sort_values("date").reset_index(drop=True)
    sr = compute_support_resistance(df)
    levels = []
    for lvl in sr.get("support_levels", []) + sr.get("resistance_levels", []):
        pr = lvl.get("price_range", [0, 0])
        levels.append({
            "low": pr[0],
            "high": pr[1],
            "kind": lvl["kind"],
            "strength": lvl["strength"],
            "touches": lvl["touches"],
            "label": "",
        })
    return levels


def _normalize_custom_sr(custom_sr: list[dict] | None) -> list[dict]:
    """Claude가 넘긴 custom S/R을 차트 렌더용 표준 포맷으로 변환.

    입력 포맷 (둘 다 지원):
    - 단일 가격: {"price": 1520, "kind": "resistance", "label": "전고점"}
    - 범위:      {"low": 1270, "high": 1300, "kind": "support", "label": "주요 지지대"}

    선택 필드: "strength" (weak/medium/strong, 기본 medium)
    """
    if not custom_sr:
        return []
    result = []
    for item in custom_sr:
        kind = item.get("kind")
        if kind not in ("support", "resistance"):
            continue
        if "low" in item and "high" in item:
            low = int(item["low"])
            high = int(item["high"])
            if low > high:
                low, high = high, low
        elif "price" in item:
            p = float(item["price"])
            low = int(p * 0.997)
            high = int(p * 1.003)
        else:
            continue
        result.append({
            "low": low,
            "high": high,
            "kind": kind,
            "strength": item.get("strength", "medium"),
            "touches": int(item.get("touches", 0)),
            "label": str(item.get("label", "")),
        })
    return result


def render_chart_html(
    code: str,
    name: str,
    ohlcv: list[dict],
    *,
    timeframe: str = "day",
    title: str = "",
    show_sr: bool = True,
    custom_sr: list[dict] | None = None,
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

    # S/R 레벨: custom_sr이 있으면 그것 사용 (Claude 판단 우선),
    # 없고 show_sr=True면 자동 계산, 아니면 빈 리스트
    if custom_sr:
        sr_levels = _normalize_custom_sr(custom_sr)
    elif show_sr:
        sr_levels = _compute_sr_for_chart(rows)
    else:
        sr_levels = []
    sr_js = json.dumps(sr_levels, ensure_ascii=False)

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

    # 확장 지표 (Phase/RSI/거래량/52주 위치) — 단일 호출로 배치 계산
    try:
        quick = compute_indicators(rows, ["ma_phase", "rsi", "volume", "position"])
    except Exception:
        quick = {}
    ma_phase_label = (quick.get("ma_phase") or {}).get("phase_label", "")
    rsi_info = quick.get("rsi") or {}
    rsi_val = rsi_info.get("value")
    rsi_state = rsi_info.get("state", "")
    vol_info = quick.get("volume") or {}
    vol_ratio = vol_info.get("ratio_vs_20d")
    pos_info = quick.get("position") or {}
    pct_from_high = pos_info.get("pct_from_high_52w")

    def _sign_class(v):
        if v is None:
            return "neutral"
        if v > 0:
            return "up"
        if v < 0:
            return "down"
        return "neutral"

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
    --bg-panel: #f8fafc;
    --bg-elevated: #ffffff;
    --text-primary: #0f172a;
    --text-secondary: #64748b;
    --text-muted: #94a3b8;
    --border: #e2e8f0;
    --border-strong: #cbd5e1;
    --grid: rgba(148, 163, 184, 0.18);
    --up: #ef4444;
    --up-bg: rgba(239, 68, 68, 0.08);
    --down: #2563eb;
    --down-bg: rgba(37, 99, 235, 0.08);
    --ma5: #eab308;
    --ma20: #06b6d4;
    --ma60: #a855f7;
    --accent: #6366f1;
    --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.04);
    --shadow-md: 0 4px 12px rgba(15, 23, 42, 0.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0a0e1a;
      --bg-panel: #131b2e;
      --bg-elevated: #1a2238;
      --text-primary: #f1f5f9;
      --text-secondary: #cbd5e1;
      --text-muted: #64748b;
      --border: #1f2937;
      --border-strong: #334155;
      --grid: rgba(148, 163, 184, 0.1);
      --up: #f87171;
      --up-bg: rgba(248, 113, 113, 0.10);
      --down: #60a5fa;
      --down-bg: rgba(96, 165, 250, 0.10);
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.2);
      --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.3);
    }}
  }}
  /* 수동 테마 토글 (data-theme가 prefers-color-scheme보다 우선) */
  html[data-theme="light"] {{
    --bg: #ffffff;
    --bg-panel: #f8fafc;
    --bg-elevated: #ffffff;
    --text-primary: #0f172a;
    --text-secondary: #64748b;
    --text-muted: #94a3b8;
    --border: #e2e8f0;
    --border-strong: #cbd5e1;
    --grid: rgba(148, 163, 184, 0.18);
    --up: #ef4444;
    --up-bg: rgba(239, 68, 68, 0.08);
    --down: #2563eb;
    --down-bg: rgba(37, 99, 235, 0.08);
  }}
  html[data-theme="dark"] {{
    --bg: #0a0e1a;
    --bg-panel: #131b2e;
    --bg-elevated: #1a2238;
    --text-primary: #f1f5f9;
    --text-secondary: #cbd5e1;
    --text-muted: #64748b;
    --border: #1f2937;
    --border-strong: #334155;
    --grid: rgba(148, 163, 184, 0.1);
    --up: #f87171;
    --up-bg: rgba(248, 113, 113, 0.10);
    --down: #60a5fa;
    --down-bg: rgba(96, 165, 250, 0.10);
  }}
  .theme-toggle {{
    position: relative;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 50%;
    width: 34px; height: 34px;
    cursor: pointer;
    font-size: 15px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.15s, border-color 0.15s;
    color: var(--text-primary);
    margin-left: 10px;
  }}
  .theme-toggle:hover {{
    transform: rotate(20deg);
    border-color: var(--accent);
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Malgun Gothic',
                 'Pretendard', 'Apple SD Gothic Neo', sans-serif;
    font-size: 13px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    font-feature-settings: 'tnum';
  }}
  .wrap {{
    max-width: 1120px;
    margin: 0 auto;
    padding: 24px 16px 32px;
    animation: fadeIn 0.35s ease-out;
  }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(4px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  .header {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 18px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
    position: relative;
  }}
  .header::after {{
    content: '';
    position: absolute;
    bottom: -1px; left: 0;
    width: 64px; height: 2px;
    background: linear-gradient(90deg, var(--accent), var(--ma20));
    border-radius: 2px;
  }}
  .header h1 {{
    margin: 0;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }}
  .header .code {{
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
  }}
  .header .price {{
    margin-left: auto;
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }}
  .header .change {{
    font-size: 14px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 6px;
  }}
  .change.up {{ color: var(--up); background: var(--up-bg); }}
  .change.down {{ color: var(--down); background: var(--down-bg); }}

  .main-grid {{
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 16px;
    align-items: start;
  }}
  @media (max-width: 960px) {{
    .main-grid {{ grid-template-columns: 1fr; }}
  }}
  .side-panel {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .stats {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }}
  .stat {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
    position: relative;
    overflow: hidden;
    transition: transform 0.15s ease, border-color 0.15s ease;
  }}
  .stat::before {{
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
    background: var(--accent);
    opacity: 0.55;
  }}
  .stat.up::before {{ background: var(--up); opacity: 0.7; }}
  .stat.down::before {{ background: var(--down); opacity: 0.7; }}
  .stat.neutral::before {{ background: var(--text-muted); }}
  .stat:hover {{
    transform: translateY(-1px);
    border-color: var(--border-strong);
  }}
  .stat-label {{
    font-size: 11px;
    color: var(--text-secondary);
    margin-bottom: 4px;
    font-weight: 500;
    letter-spacing: 0.02em;
  }}
  .stat-value {{
    font-size: 14px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }}

  .chart-container {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
    position: relative;
    box-shadow: var(--shadow-sm);
    min-width: 0;
  }}
  .sr-panel {{
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
  }}
  .sr-panel h3 {{
    margin: 0 0 10px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }}
  .sr-list {{
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }}
  .sr-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 4px 6px;
    border-radius: 5px;
    transition: background 0.12s;
  }}
  .sr-item:hover {{ background: var(--bg-elevated); }}
  .sr-item .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    flex-shrink: 0;
  }}
  .sr-item.resistance .dot {{ background: var(--up); box-shadow: 0 0 5px var(--up); }}
  .sr-item.support .dot {{ background: var(--down); box-shadow: 0 0 5px var(--down); }}
  .sr-item .price {{
    flex: 1;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }}
  .sr-item .label {{
    color: var(--text-secondary);
    font-size: 11px;
  }}
  .current-price-divider {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 6px 0;
    font-size: 11px;
    font-weight: 600;
    color: var(--accent);
  }}
  .current-price-divider::before,
  .current-price-divider::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: var(--accent);
    opacity: 0.5;
  }}
  svg {{
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }}
  @media (prefers-color-scheme: dark) {{
    svg path {{
      filter: drop-shadow(0 0 1.5px currentColor);
    }}
    svg rect[fill] {{
      filter: drop-shadow(0 0 0.5px rgba(255,255,255,0.1));
    }}
  }}
  .legend {{
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    justify-content: center;
    margin-top: 12px;
    font-size: 11.5px;
    color: var(--text-secondary);
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 9px;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    border-radius: 12px;
    transition: background 0.15s;
  }}
  .legend-item:hover {{ background: var(--bg-panel); }}
  .legend-dot {{
    width: 12px;
    height: 2.5px;
    border-radius: 2px;
  }}

  .tooltip {{
    position: absolute;
    pointer-events: none;
    background: var(--bg-elevated);
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 11.5px;
    line-height: 1.65;
    color: var(--text-primary);
    box-shadow: var(--shadow-md);
    backdrop-filter: blur(8px);
    min-width: 150px;
    z-index: 10;
    display: none;
    transition: opacity 0.12s;
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
    <button class="theme-toggle" id="themeToggle" title="라이트/다크 전환">🌓</button>
  </div>

  <div class="main-grid">
    <div class="chart-container">
      <svg id="chart" viewBox="0 0 1000 680" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="legend">
        <span class="legend-item"><span class="legend-dot" style="background:var(--up);"></span>양봉</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--down);"></span>음봉</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--ma5);"></span>MA5</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--ma20);"></span>MA20</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--ma60);"></span>MA60</span>
      </div>
      <div class="tooltip" id="tooltip"></div>
    </div>

    <aside class="side-panel">
      <div class="stats">
        <div class="stat up">
          <div class="stat-label">기간 최고가</div>
          <div class="stat-value" style="color:var(--up);">{_fmt_price(high_val)}</div>
        </div>
        <div class="stat down">
          <div class="stat-label">기간 최저가</div>
          <div class="stat-value" style="color:var(--down);">{_fmt_price(low_val)}</div>
        </div>
        <div class="stat {_sign_class(ma20_diff_pct)}">
          <div class="stat-label">vs MA20</div>
          <div class="stat-value">{ma20_diff_pct:+.2f}%</div>
        </div>
        <div class="stat {_sign_class(ma60_diff_pct)}">
          <div class="stat-label">vs MA60</div>
          <div class="stat-value">{ma60_diff_pct:+.2f}%</div>
        </div>
        {f'''<div class="stat neutral">
          <div class="stat-label">이평선 Phase</div>
          <div class="stat-value" style="font-size:12.5px;">{ma_phase_label}</div>
        </div>''' if ma_phase_label else ''}
        {f'''<div class="stat {"up" if rsi_val and rsi_val >= 70 else "down" if rsi_val and rsi_val <= 30 else "neutral"}">
          <div class="stat-label">RSI · {rsi_state}</div>
          <div class="stat-value">{rsi_val:.1f}</div>
        </div>''' if rsi_val is not None else ''}
        {f'''<div class="stat {"up" if vol_ratio and vol_ratio >= 1.5 else "neutral"}">
          <div class="stat-label">거래량 비</div>
          <div class="stat-value">×{vol_ratio:.2f}</div>
        </div>''' if vol_ratio is not None else ''}
        {f'''<div class="stat {_sign_class(pct_from_high)}">
          <div class="stat-label">52주 고점</div>
          <div class="stat-value">{pct_from_high:+.2f}%</div>
        </div>''' if pct_from_high is not None else ''}
      </div>

      <div class="sr-panel">
        <h3>지지 · 저항</h3>
        <div class="sr-list" id="srList"></div>
      </div>
    </aside>
  </div>

  <div class="footer">
    네이버 증권 데이터 · 생성: {generated_at} · <b>StockLens</b>
  </div>
</div>

<script>
const DATA = {candles_js};
const SR_LEVELS = {sr_js};
const N = DATA.length;

// ────────────────────────────────────────
// 테마 토글 (라이트/다크)
// ────────────────────────────────────────
(function initTheme() {{
  const toggle = document.getElementById('themeToggle');
  if (!toggle) return;
  const STORAGE_KEY = 'stocklens-theme';
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') {{
    document.documentElement.setAttribute('data-theme', stored);
  }}
  const iconFor = (t) => t === 'dark' ? '☀️' : t === 'light' ? '🌙' : '🌓';
  toggle.textContent = iconFor(stored);
  toggle.addEventListener('click', () => {{
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem(STORAGE_KEY, next);
    toggle.textContent = iconFor(next);
    // 차트 재렌더 (CSS 변수 기반 색상 재적용)
    setTimeout(render, 50);
  }});
}})();

// ────────────────────────────────────────
// 레이아웃 상수 (viewBox 1000 × 680 기준)
// ────────────────────────────────────────
const W = 1000, H = 680;
const PAD_L = 14;
const PAD_R = 110;    // 가격축 + 이평선/현재가 라벨 공간
const PAD_T = 16;
const PAD_B = 40;     // 날짜 공간
const GAP = 12;

const candleH = Math.round((H - PAD_T - PAD_B - GAP) * 0.78);
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

  // ─── 지지/저항 영역 (캔들 뒤에 깔림) ───
  const STRENGTH_OPACITY = {{ strong: 0.20, medium: 0.13, weak: 0.07 }};
  SR_LEVELS.forEach(lvl => {{
    const yHigh = py(lvl.high);
    const yLow = py(lvl.low);
    const bandH = Math.max(2, yLow - yHigh);
    const fill = lvl.kind === 'resistance' ? UP : DN;
    const op = STRENGTH_OPACITY[lvl.strength] || 0.08;
    svg.appendChild(el('rect', {{
      x: PAD_L, y: yHigh,
      width: chartW, height: bandH,
      fill: fill, opacity: op,
    }}));
    // 우측 라벨 (custom label 우선, 없으면 자동 생성)
    const label = el('text', {{
      x: PAD_L + chartW - 4,
      y: yHigh + Math.min(bandH - 2, 11),
      'font-size': 9, 'font-weight': 600,
      fill: fill, opacity: 0.85,
      'text-anchor': 'end',
    }});
    let labelText;
    if (lvl.label) {{
      labelText = lvl.label;
    }} else {{
      labelText = (lvl.kind === 'resistance' ? '저항' : '지지');
      if (lvl.touches) labelText += ' ' + lvl.touches + '회';
    }}
    label.textContent = labelText;
    svg.appendChild(label);
  }});

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

  // ─── 현재가 수평 강조선 + 우측 pill 라벨 ───
  const curPrice = DATA[N - 1].c;
  const curY = py(curPrice);
  const curColor = getColor('--accent');
  // dashed horizontal line 전체 차트 가로
  svg.appendChild(el('line', {{
    x1: PAD_L, x2: PAD_L + chartW,
    y1: curY, y2: curY,
    stroke: curColor, 'stroke-width': 1,
    'stroke-dasharray': '4 3', opacity: 0.7,
  }}));
  // 우측 pill 배경
  const pillText = curPrice.toLocaleString();
  const pillW = 8 + pillText.length * 7;
  svg.appendChild(el('rect', {{
    x: PAD_L + chartW + 3, y: curY - 9,
    width: pillW, height: 18,
    rx: 4, ry: 4,
    fill: curColor, opacity: 0.95,
  }}));
  const pillLabel = el('text', {{
    x: PAD_L + chartW + 3 + pillW / 2, y: curY + 4,
    'font-size': 10.5, 'font-weight': 700,
    fill: '#fff', 'text-anchor': 'middle',
  }});
  pillLabel.textContent = pillText;
  svg.appendChild(pillLabel);

  // ─── 이평선 우측 라벨 (겹침 방지: y 위치 정렬 후 간격 보장) ───
  const maLabels = [];
  const ma5Last = MA5[N - 1], ma20Last = MA20[N - 1], ma60Last = MA60[N - 1];
  if (ma5Last !== null) maLabels.push({{ name: 'MA5', val: ma5Last, color: MA5_C }});
  if (ma20Last !== null) maLabels.push({{ name: 'MA20', val: ma20Last, color: MA20_C }});
  if (ma60Last !== null) maLabels.push({{ name: 'MA60', val: ma60Last, color: MA60_C }});
  // 현재가와의 근접 회피를 위해 y 정렬
  maLabels.sort((a, b) => py(a.val) - py(b.val));
  const minGap = 14;
  let prevY = -999;
  maLabels.forEach(m => {{
    let y = py(m.val);
    if (y < prevY + minGap) y = prevY + minGap;
    // 현재가 pill과 충돌 회피
    if (Math.abs(y - curY) < 10) y = curY + 14;
    prevY = y;
    const labelX = PAD_L + chartW + 4;
    // dot
    svg.appendChild(el('circle', {{
      cx: labelX + 4, cy: y, r: 3,
      fill: m.color,
    }}));
    // text
    const t = el('text', {{
      x: labelX + 11, y: y + 3.5,
      'font-size': 10, fill: m.color, 'font-weight': 600,
    }});
    t.textContent = m.name + ' ' + Math.round(m.val).toLocaleString();
    svg.appendChild(t);
  }});

  // ─── 사이드 패널 S/R 리스트 ───
  const srListEl = document.getElementById('srList');
  if (srListEl && SR_LEVELS.length) {{
    srListEl.innerHTML = '';
    // 가격 내림차순 정렬 (높은 저항 → 낮은 지지)
    const sorted = [...SR_LEVELS].sort((a, b) => (b.low + b.high) / 2 - (a.low + a.high) / 2);
    let dividerInserted = false;
    sorted.forEach(lvl => {{
      const mid = (lvl.low + lvl.high) / 2;
      // 현재가를 지나치면 divider 한 번 삽입
      if (!dividerInserted && mid < curPrice) {{
        const div = document.createElement('div');
        div.className = 'current-price-divider';
        div.textContent = '현재가 ' + curPrice.toLocaleString();
        srListEl.appendChild(div);
        dividerInserted = true;
      }}
      const item = document.createElement('div');
      item.className = 'sr-item ' + lvl.kind;
      const priceText = (lvl.low === lvl.high || Math.abs(lvl.high - lvl.low) < 2)
        ? lvl.low.toLocaleString()
        : lvl.low.toLocaleString() + '~' + lvl.high.toLocaleString();
      const labelText = lvl.label || (lvl.touches ? lvl.touches + '회 터치' : '');
      item.innerHTML = '<span class="dot"></span>' +
        '<span class="price">' + priceText + '</span>' +
        (labelText ? '<span class="label">' + labelText + '</span>' : '');
      srListEl.appendChild(item);
    }});
    // divider가 아직 안 찍혔으면(모든 S/R이 현재가 위) 마지막에라도 표시
    if (!dividerInserted) {{
      const div = document.createElement('div');
      div.className = 'current-price-divider';
      div.textContent = '현재가 ' + curPrice.toLocaleString();
      srListEl.appendChild(div);
    }}
  }}

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


# ─────────────────────────────────────────────────────────────
# 멀티 타임프레임 (여러 차트를 한 HTML에 배치)
# ─────────────────────────────────────────────────────────────

_MULTI_COUNT_DEFAULTS = {"day": 120, "week": 52, "month": 24}


def render_multi_chart_html(
    code: str,
    name: str,
    frames: list[dict],
    *,
    show_sr: bool = True,
    custom_sr: list[dict] | None = None,
) -> str:
    """여러 타임프레임 차트를 한 HTML에 세로로 배치.

    각 차트는 기존 render_chart_html을 base64 encoded iframe으로 embed하여
    스타일/스크립트 충돌 없이 독립 렌더링.

    Args:
        code: 종목코드 (예: "005930")
        name: 종목명
        frames: [{"timeframe": "day"|"week"|"month", "ohlcv": [...]}, ...]
        show_sr: 각 프레임에 지지/저항 오버레이 표시 여부 (custom_sr 없을 때 자동 계산)
        custom_sr: Claude 판단 S/R (모든 프레임에 동일 적용)

    Returns:
        완성된 멀티프레임 HTML (단독 파일로 저장 가능)
    """
    if not frames:
        raise ValueError("frames 비어 있음")

    tf_label = {"day": "일봉", "week": "주봉", "month": "월봉"}
    sections = []
    for frame in frames:
        tf = frame["timeframe"]
        ohlcv = frame["ohlcv"]
        if not ohlcv:
            continue
        inner_html = render_chart_html(
            code, name, ohlcv,
            timeframe=tf, show_sr=show_sr, custom_sr=custom_sr,
        )
        encoded = base64.b64encode(inner_html.encode("utf-8")).decode("ascii")
        label = tf_label.get(tf, tf)
        sections.append(f"""
  <section class="frame">
    <div class="frame-tag">{label} · {len(ohlcv)}봉</div>
    <iframe class="chart-frame" src="data:text/html;base64,{encoded}"
            loading="lazy" title="{name} {label}"></iframe>
  </section>""")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    tf_summary = " · ".join(tf_label.get(f["timeframe"], f["timeframe"]) for f in frames)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}({code}) 멀티 타임프레임</title>
<style>
  :root {{
    --bg: #ffffff;
    --bg-panel: #f8fafc;
    --text-primary: #0f172a;
    --text-secondary: #64748b;
    --border: #e2e8f0;
    --accent: #6366f1;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0a0e1a;
      --bg-panel: #131b2e;
      --text-primary: #f1f5f9;
      --text-secondary: #cbd5e1;
      --border: #1f2937;
      --accent: #818cf8;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Malgun Gothic',
                 'Pretendard', 'Apple SD Gothic Neo', sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{
    max-width: 1120px; margin: 0 auto; padding: 28px 16px 36px;
    animation: fadeIn 0.4s ease-out;
  }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  header {{
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
    padding-bottom: 18px; margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
    position: relative;
  }}
  header::after {{
    content: '';
    position: absolute;
    bottom: -1px; left: 0;
    width: 88px; height: 2px;
    background: linear-gradient(90deg, var(--accent), #06b6d4);
    border-radius: 2px;
  }}
  header h1 {{
    margin: 0; font-size: 26px; font-weight: 700;
    letter-spacing: -0.02em;
  }}
  header .meta {{
    color: var(--text-secondary); font-size: 13px;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
  }}
  header .tf {{
    color: var(--accent); font-weight: 600; font-size: 12px;
    padding: 4px 11px;
    background: rgba(99, 102, 241, 0.1);
    border-radius: 12px;
  }}
  .frame {{ margin: 32px 0; }}
  .frame-tag {{
    display: inline-block;
    font-size: 12px; font-weight: 600; letter-spacing: 0.02em;
    color: var(--text-primary);
    padding: 5px 12px; margin-bottom: 10px;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 14px;
  }}
  .chart-frame {{
    width: 100%; height: 820px; border: none;
    border-radius: 12px;
    background: var(--bg);
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
  }}
  .footer {{
    margin-top: 36px;
    font-size: 11px; color: var(--text-secondary);
    text-align: center;
    letter-spacing: 0.02em;
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>{name}</h1>
    <span class="meta">{code}</span>
    <span class="tf">{tf_summary}</span>
  </header>
{''.join(sections)}
  <div class="footer">네이버 증권 · 생성 {generated_at} · <b>StockLens</b></div>
</div>
</body>
</html>
"""
