"""기술지표 계산 모듈.

OHLCV 리스트를 받아 이동평균·RSI·MACD·볼린저·스토캐스틱·OBV 및 파생 상태 판정을 반환한다.
MCP 도구에서 호출하는 순수 계산 레이어. 네트워크·캐시 의존성 없음.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# pandas_ta는 선택적 의존성으로 취급 — 없으면 자체 구현으로 폴백.
try:
    import pandas_ta as ta  # type: ignore
    _HAS_PANDAS_TA = True
except Exception:
    _HAS_PANDAS_TA = False


def _to_df(ohlcv: list[dict]) -> pd.DataFrame:
    """OHLCV dict 리스트를 오름차순 날짜 인덱스의 DataFrame으로 변환."""
    if not ohlcv:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ohlcv)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def round_to_tick(price) -> int | None:
    """KRX 호가가격단위로 라운딩 (2023.1.25 시행 기준).

    이평선·BB·매물대 등 계산 결과를 실거래 가능 가격으로 표시.
    None/NaN 입력은 None 반환.
    """
    if price is None or (isinstance(price, float) and pd.isna(price)):
        return None
    p = float(price)
    if p < 2000:
        tick = 1
    elif p < 5000:
        tick = 5
    elif p < 20000:
        tick = 10
    elif p < 50000:
        tick = 50
    elif p < 200000:
        tick = 100
    elif p < 500000:
        tick = 500
    else:
        tick = 1000
    return int(round(p / tick) * tick)


# ─────────────────────────────────────────────────────────────
# 이동평균 & Phase
# ─────────────────────────────────────────────────────────────

_MA_PERIODS = (5, 20, 60, 120, 240)

# 한국어 트레이딩 전문용어 라벨.
# 서버가 직접 반환해야 LLM 출력에서 "꼭임"/"꿰임" 같은 토크나이저 오류 방지.
_MA_PHASE_LABELS = {
    0: "완전역배열",
    1: "단기상승꼬임",
    2: "꼬임",
    3: "단기하락꼬임",
    4: "완전정배열",
}

_CROSS_LABELS = {
    "golden": "골든크로스",
    "dead": "데드크로스",
}


def compute_ma(df: pd.DataFrame, periods: tuple[int, ...] = _MA_PERIODS) -> dict:
    """각 기간의 이동평균 최신값 (호가단위 라운딩)."""
    result = {}
    for p in periods:
        if len(df) >= p:
            result[f"ma{p}"] = round_to_tick(df["close"].rolling(p).mean().iloc[-1])
        else:
            result[f"ma{p}"] = None
    return result


def compute_ma_slope(df: pd.DataFrame, period: int = 120, lookback: int = 20) -> float | None:
    """이평선 기울기 (lookback일 전 대비 % 변화)."""
    if len(df) < period + lookback:
        return None
    ma = df["close"].rolling(period).mean()
    now = ma.iloc[-1]
    past = ma.iloc[-1 - lookback]
    if past is None or past == 0 or pd.isna(past) or pd.isna(now):
        return None
    return round(float((now - past) / past * 100), 2)


def compute_ma_phase(df: pd.DataFrame) -> dict:
    """5-단계 이평선 Phase 판정 (5/20/60/120 기준).

    phase 0: 완전 역배열 (ma5 < ma20 < ma60 < ma120)
    phase 1: 단기 상승 꼬임 (ma5 > ma20 이지만 ma60/ma120은 역배열)
    phase 2: 꼬임 (그 외 혼재)
    phase 3: 단기 하락 꼬임 (ma5 < ma20 이지만 ma60/ma120은 정배열)
    phase 4: 완전 정배열 (ma5 > ma20 > ma60 > ma120)
    """
    ma = {p: df["close"].rolling(p).mean().iloc[-1] for p in (5, 20, 60, 120) if len(df) >= p}
    if len(ma) < 4:
        return {"phase": None, "detail": "데이터 부족", "pairs": {}}

    m5, m20, m60, m120 = ma[5], ma[20], ma[60], ma[120]

    bullish = m5 > m20 > m60 > m120
    bearish = m5 < m20 < m60 < m120

    if bullish:
        phase = 4
    elif bearish:
        phase = 0
    else:
        short_bull = m5 > m20
        long_bull = m60 > m120
        if short_bull and not long_bull:
            phase = 1
        elif not short_bull and long_bull:
            phase = 3
        else:
            phase = 2

    pairs = {
        "5_vs_20": "상승" if m5 > m20 else "하락",
        "20_vs_60": "상승" if m20 > m60 else "하락",
        "60_vs_120": "상승" if m60 > m120 else "하락",
        "price_vs_ma20": round(float((df["close"].iloc[-1] - m20) / m20 * 100), 2),
        "price_vs_ma120": round(float((df["close"].iloc[-1] - m120) / m120 * 100), 2),
    }

    return {"phase": phase, "phase_label": _MA_PHASE_LABELS[phase], "pairs": pairs}


def compute_ma_cross(df: pd.DataFrame, short: int = 20, long: int = 60, within_days: int = 30) -> dict:
    """지정 기간 내 short/long 이평선의 크로스 이벤트."""
    if len(df) < long + within_days:
        return {"type": None, "days_ago": None}

    ma_s = df["close"].rolling(short).mean()
    ma_l = df["close"].rolling(long).mean()
    diff = ma_s - ma_l

    for i in range(1, within_days + 1):
        if i + 1 >= len(diff):
            break
        prev, curr = diff.iloc[-i - 1], diff.iloc[-i]
        if pd.isna(prev) or pd.isna(curr):
            continue
        if prev <= 0 < curr:
            return {"type": "golden", "type_label": _CROSS_LABELS["golden"], "days_ago": i, "short": short, "long": long}
        if prev >= 0 > curr:
            return {"type": "dead", "type_label": _CROSS_LABELS["dead"], "days_ago": i, "short": short, "long": long}

    return {"type": None, "type_label": None, "days_ago": None, "short": short, "long": long}


# ─────────────────────────────────────────────────────────────
# RSI / MACD / Bollinger / Stochastic / OBV
# ─────────────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> dict:
    if len(df) < period + 1:
        return {"value": None, "state": None}

    if _HAS_PANDAS_TA:
        rsi = ta.rsi(df["close"], length=period)
    else:
        # Wilder's smoothing (alpha = 1/period). 네이버를 포함한 표준 차트와 일치.
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    v = rsi.iloc[-1]
    if pd.isna(v):
        return {"value": None, "state": None}
    v = round(float(v), 2)
    state = "과매수" if v >= 70 else "과매도" if v <= 30 else "중립"
    return {"value": v, "state": state, "period": period}


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    if len(df) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None, "cross": None}

    if _HAS_PANDAS_TA:
        macd_df = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
        if macd_df is None or macd_df.empty:
            return {"macd": None, "signal": None, "histogram": None, "cross": None}
        macd_line = macd_df.iloc[:, 0]
        signal_line = macd_df.iloc[:, 2]
        hist = macd_df.iloc[:, 1]
    else:
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line

    cross = {"type": None, "type_label": None, "days_ago": None}
    for i in range(1, min(30, len(hist) - 1)):
        prev = hist.iloc[-i - 1]
        curr = hist.iloc[-i]
        if pd.isna(prev) or pd.isna(curr):
            continue
        if prev <= 0 < curr:
            cross = {"type": "golden", "type_label": _CROSS_LABELS["golden"], "days_ago": i}
            break
        if prev >= 0 > curr:
            cross = {"type": "dead", "type_label": _CROSS_LABELS["dead"], "days_ago": i}
            break

    return {
        "macd": round(float(macd_line.iloc[-1]), 2) if not pd.isna(macd_line.iloc[-1]) else None,
        "signal": round(float(signal_line.iloc[-1]), 2) if not pd.isna(signal_line.iloc[-1]) else None,
        "histogram": round(float(hist.iloc[-1]), 2) if not pd.isna(hist.iloc[-1]) else None,
        "cross": cross,
    }


def compute_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> dict:
    """볼린저 밴드. population std (ddof=0) 사용 — 네이버/대다수 차트와 일치."""
    if len(df) < period:
        return {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth": None}

    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd

    price = df["close"].iloc[-1]
    u, m, l = upper.iloc[-1], mid.iloc[-1], lower.iloc[-1]
    if pd.isna(u) or pd.isna(l) or u == l:
        return {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth": None}

    percent_b = (price - l) / (u - l)
    bandwidth = (u - l) / m * 100

    if percent_b > 1.0:
        position = "상단 돌파"
    elif percent_b >= 0.8:
        position = "상단 근접"
    elif percent_b >= 0.2:
        position = "밴드 내"
    elif percent_b >= 0.0:
        position = "하단 근접"
    else:
        position = "하단 이탈"

    return {
        "upper": round_to_tick(u),
        "middle": round_to_tick(m),
        "lower": round_to_tick(l),
        "percent_b": round(float(percent_b), 3),
        "bandwidth": round(float(bandwidth), 2),
        "position": position,
        "period": period,
        "std": std,
    }


def compute_stochastic(
    df: pd.DataFrame,
    k_period: int = 12,
    slow_k_period: int = 5,
    d_period: int = 3,
) -> dict:
    """Slow Stochastic — 네이버 증권 기본값 (12, 5, 3).

    raw_K = 100 * (close - low_n) / (high_n - low_n)   # n=k_period
    slow_K = SMA(raw_K, slow_k_period)
    slow_D = SMA(slow_K, d_period)
    """
    need = k_period + slow_k_period + d_period
    if len(df) < need:
        return {"k": None, "d": None, "state": None}

    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    raw_k = 100 * (df["close"] - low_min) / (high_max - low_min)
    slow_k = raw_k.rolling(slow_k_period).mean()
    slow_d = slow_k.rolling(d_period).mean()

    kv, dv = slow_k.iloc[-1], slow_d.iloc[-1]
    if pd.isna(kv) or pd.isna(dv):
        return {"k": None, "d": None, "state": None}

    state = "과매수" if kv >= 80 else "과매도" if kv <= 20 else "중립"
    return {
        "k": round(float(kv), 2),
        "d": round(float(dv), 2),
        "state": state,
        "params": {"k": k_period, "slow_k": slow_k_period, "d": d_period},
    }


def compute_obv(df: pd.DataFrame, window: int = 20) -> dict:
    if len(df) < window + 1:
        return {"obv": None, "change_pct": None}

    direction = np.sign(df["close"].diff().fillna(0))
    obv = (direction * df["volume"]).cumsum()
    current = obv.iloc[-1]
    past = obv.iloc[-1 - window] if len(obv) > window else None

    if past is None or past == 0 or pd.isna(past):
        return {"obv": int(current), "change_pct": None}

    change_pct = (current - past) / abs(past) * 100
    return {
        "obv": int(current),
        "change_pct": round(float(change_pct), 2),
        "window": window,
    }


# ─────────────────────────────────────────────────────────────
# 거래량 / 위치 / 캔들
# ─────────────────────────────────────────────────────────────

def compute_volume(df: pd.DataFrame) -> dict:
    if len(df) < 20:
        return {"today": None, "avg_20d": None, "ratio_vs_20d": None, "trade_value_krw": None}

    today_vol = int(df["volume"].iloc[-1])
    avg_20d = float(df["volume"].rolling(20).mean().iloc[-1])
    ratio = today_vol / avg_20d if avg_20d > 0 else None
    trade_value = int(df["close"].iloc[-1]) * today_vol

    rank_52w = None
    if len(df) >= 252:
        window = df["volume"].iloc[-252:]
        rank_52w = int((window > today_vol).sum()) + 1

    return {
        "today": today_vol,
        "avg_20d": int(avg_20d),
        "ratio_vs_20d": round(float(ratio), 2) if ratio else None,
        "trade_value_krw": trade_value,
        "rank_52w": rank_52w,
    }


def compute_position(df: pd.DataFrame) -> dict:
    if len(df) < 2:
        return {}

    lookback = df.iloc[-252:] if len(df) >= 252 else df
    high_52w = float(lookback["high"].max())
    low_52w = float(lookback["low"].min())
    high_idx = lookback["high"].idxmax()
    low_idx = lookback["low"].idxmin()
    high_date = str(lookback.loc[high_idx, "date"]) if "date" in lookback.columns else None
    low_date = str(lookback.loc[low_idx, "date"]) if "date" in lookback.columns else None
    price = float(df["close"].iloc[-1])

    return {
        "price": int(price),
        "high_52w": int(high_52w),
        "low_52w": int(low_52w),
        "pct_from_high_52w": round((price - high_52w) / high_52w * 100, 2),
        "pct_from_low_52w": round((price - low_52w) / low_52w * 100, 2),
        "high_date": high_date,
        "low_date": low_date,
        "days_since_high": int(len(lookback) - 1 - lookback.index.get_loc(high_idx)),
        "days_since_low": int(len(lookback) - 1 - lookback.index.get_loc(low_idx)),
    }


# ─────────────────────────────────────────────────────────────
# 지지/저항 · 매물대 · 가격채널 (구조 분석)
# ─────────────────────────────────────────────────────────────

def _find_pivots(df: pd.DataFrame, window: int = 10) -> tuple[list, list]:
    """Rolling window로 국소 고점·저점 피벗 탐지.

    인덱스 i 의 high가 [i-window, i+window] 구간 max와 같으면 pivot_high.
    low도 동일.

    Returns:
        (pivot_highs, pivot_lows) — 각 원소: (date, price, volume)
    """
    highs = []
    lows = []
    n = len(df)
    if n < window * 2 + 1:
        return highs, lows

    for i in range(window, n - window):
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]
        window_slice = df.iloc[i - window : i + window + 1]
        if hi == window_slice["high"].max():
            highs.append((
                str(df["date"].iloc[i]) if "date" in df.columns else str(i),
                float(hi),
                int(df["volume"].iloc[i]),
            ))
        if lo == window_slice["low"].min():
            lows.append((
                str(df["date"].iloc[i]) if "date" in df.columns else str(i),
                float(lo),
                int(df["volume"].iloc[i]),
            ))
    return highs, lows


def _cluster_pivots(pivots: list[tuple], tolerance_pct: float = 1.5) -> list[list]:
    """근접 피벗들을 같은 S/R 대역으로 묶음."""
    if not pivots:
        return []
    sorted_pv = sorted(pivots, key=lambda x: x[1])
    clusters = [[sorted_pv[0]]]
    for p in sorted_pv[1:]:
        last = clusters[-1]
        avg = sum(x[1] for x in last) / len(last)
        if avg > 0 and abs(p[1] - avg) / avg * 100 <= tolerance_pct:
            last.append(p)
        else:
            clusters.append([p])
    return clusters


def compute_support_resistance(
    df: pd.DataFrame,
    window: int = 10,
    tolerance_pct: float = 1.5,
    min_touches: int = 2,
) -> dict:
    """지지·저항 자동 추출.

    1. rolling window로 국소 고점/저점 피벗 탐지
    2. 피벗들을 ±tolerance_pct 내면 같은 대역으로 클러스터링
    3. min_touches 이상 터치된 클러스터만 레벨로 인정
    4. 터치 횟수로 강도 등급 (weak/medium/strong)
    """
    if len(df) < window * 2 + 1:
        return {
            "error": f"데이터 부족 (최소 {window * 2 + 1}봉 필요)",
            "support_levels": [],
            "resistance_levels": [],
        }

    highs, lows = _find_pivots(df, window)
    resistance_clusters = _cluster_pivots(highs, tolerance_pct)
    support_clusters = _cluster_pivots(lows, tolerance_pct)

    current_price = float(df["close"].iloc[-1])

    def format_cluster(cluster: list, kind: str) -> dict | None:
        if len(cluster) < min_touches:
            return None
        prices = [c[1] for c in cluster]
        dates = sorted([c[0] for c in cluster])
        volumes = [c[2] for c in cluster]
        price_low = float(min(prices))
        price_high = float(max(prices))
        avg_price = sum(prices) / len(prices)
        touches = len(cluster)
        strength = "strong" if touches >= 4 else "medium" if touches >= 3 else "weak"
        return {
            "kind": kind,
            "price_range": [round_to_tick(price_low), round_to_tick(price_high)],
            "avg_price": round_to_tick(avg_price),
            "touches": touches,
            "touch_dates": dates,
            "avg_volume_at_touch": int(sum(volumes) / len(volumes)),
            "strength": strength,
            "pct_from_current": round((avg_price - current_price) / current_price * 100, 2),
        }

    supports = [s for s in (format_cluster(c, "support") for c in support_clusters) if s]
    resistances = [r for r in (format_cluster(c, "resistance") for c in resistance_clusters) if r]

    # 현재가 기준 가까운 것부터
    supports.sort(key=lambda x: abs(current_price - x["avg_price"]))
    resistances.sort(key=lambda x: abs(current_price - x["avg_price"]))

    return {
        "support_levels": supports,
        "resistance_levels": resistances,
        "current_price": round_to_tick(current_price),
        "lookback_candles": len(df),
        "params": {
            "window": window,
            "tolerance_pct": tolerance_pct,
            "min_touches": min_touches,
        },
    }


def compute_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    """가격대별 누적 거래량 분포 (매물대 분석).

    POC (Point of Control) = 최대 매물 집중 가격대
    Value Area = 전체 거래량의 70% 포함 가격 구간
    """
    if len(df) < 20:
        return {"error": "데이터 부족 (최소 20봉 필요)"}

    prices = df["close"].astype(float)
    volumes = df["volume"].astype(float)
    p_min, p_max = float(prices.min()), float(prices.max())

    if p_max <= p_min:
        return {"error": "가격 범위 없음"}

    bin_width = (p_max - p_min) / bins
    profile = []
    for i in range(bins):
        bin_low = p_min + i * bin_width
        bin_high = bin_low + bin_width
        if i == bins - 1:
            mask = (prices >= bin_low) & (prices <= bin_high)
        else:
            mask = (prices >= bin_low) & (prices < bin_high)
        vol_sum = float(volumes[mask].sum())
        profile.append({
            "price_range": [round_to_tick(bin_low), round_to_tick(bin_high)],
            "volume": int(vol_sum),
        })

    total = sum(p["volume"] for p in profile)
    for p in profile:
        p["volume_pct"] = round(p["volume"] / total * 100, 2) if total > 0 else 0

    poc = max(profile, key=lambda x: x["volume"])

    sorted_profile = sorted(profile, key=lambda x: x["volume"], reverse=True)
    cumsum = 0.0
    va_bins = []
    for p in sorted_profile:
        cumsum += p["volume_pct"]
        va_bins.append(p)
        if cumsum >= 70:
            break

    va_low = min(p["price_range"][0] for p in va_bins)
    va_high = max(p["price_range"][1] for p in va_bins)
    current = float(df["close"].iloc[-1])

    return {
        "profile": profile,
        "poc": {
            "price_range": poc["price_range"],
            "volume_pct": poc["volume_pct"],
        },
        "value_area": {
            "low": round_to_tick(va_low),
            "high": round_to_tick(va_high),
            "coverage_pct": round(cumsum, 1),
        },
        "current_price": round_to_tick(current),
        "current_in_value_area": va_low <= current <= va_high,
        "total_volume": int(total),
        "lookback_candles": len(df),
        "bins": bins,
    }


def compute_price_channel(df: pd.DataFrame, period: int = 20) -> dict:
    """Donchian Channel — N봉 고가/저가 기반 가격 채널.

    Upper = N봉 최고가, Lower = N봉 최저가, Mid = (U+L)/2
    position_pct: 현재가가 채널 내 어느 위치 (0=하단, 100=상단)
    width_pct: 채널 폭 (변동성 지표)
    """
    if len(df) < period:
        return {"error": f"데이터 부족 (최소 {period}봉 필요)"}

    recent = df.iloc[-period:]
    upper = float(recent["high"].max())
    lower = float(recent["low"].min())
    midline = (upper + lower) / 2
    current = float(df["close"].iloc[-1])

    width_pct = (upper - lower) / midline * 100 if midline > 0 else 0
    position = (current - lower) / (upper - lower) if upper > lower else 0.5

    if position >= 0.9:
        state = "상단근접"
    elif position <= 0.1:
        state = "하단근접"
    elif position >= 0.6:
        state = "상단부"
    elif position <= 0.4:
        state = "하단부"
    else:
        state = "중단"

    upper_idx = recent["high"].idxmax()
    lower_idx = recent["low"].idxmin()
    upper_date = str(df.loc[upper_idx, "date"]) if "date" in df.columns else None
    lower_date = str(df.loc[lower_idx, "date"]) if "date" in df.columns else None

    return {
        "upper": round_to_tick(upper),
        "lower": round_to_tick(lower),
        "midline": round_to_tick(midline),
        "current": round_to_tick(current),
        "position_pct": round(position * 100, 1),
        "width_pct": round(width_pct, 2),
        "state": state,
        "upper_date": upper_date,
        "lower_date": lower_date,
        "period": period,
    }


def compute_candle(df: pd.DataFrame) -> dict:
    if len(df) < 2:
        return {}

    last = df.iloc[-1]
    prev = df.iloc[-2]

    body = last["close"] - last["open"]
    rng = last["high"] - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    gap = last["open"] - prev["close"]
    gap_pct = gap / prev["close"] * 100 if prev["close"] else 0

    return {
        "date": str(last.get("date", "")),
        "open": int(last["open"]),
        "high": int(last["high"]),
        "low": int(last["low"]),
        "close": int(last["close"]),
        "body_pct": round(float(body / last["open"] * 100), 2) if last["open"] else 0,
        "range_pct": round(float(rng / last["open"] * 100), 2) if last["open"] else 0,
        "upper_wick_pct": round(float(upper_wick / last["open"] * 100), 2) if last["open"] else 0,
        "lower_wick_pct": round(float(lower_wick / last["open"] * 100), 2) if last["open"] else 0,
        "gap_pct": round(float(gap_pct), 2),
        "color": "양봉" if body > 0 else "음봉" if body < 0 else "도지",
    }


# ─────────────────────────────────────────────────────────────
# 메인 엔트리
# ─────────────────────────────────────────────────────────────

# 각 lambda는 **kwargs를 받아 compute_* 함수의 파라미터를 그대로 노출.
# ma_slope/ma_cross 같은 합성 지표는 kwargs 무시 (내부 고정값 사용).
_INDICATOR_MAP = {
    "ma": lambda df, **kw: compute_ma(df, **kw),
    "ma_phase": lambda df, **kw: compute_ma_phase(df, **kw),
    "ma_slope": lambda df, **kw: {
        "ma20_slope_pct": compute_ma_slope(df, 20, 10),
        "ma60_slope_pct": compute_ma_slope(df, 60, 20),
        "ma120_slope_pct": compute_ma_slope(df, 120, 20),
    },
    "ma_cross": lambda df, **kw: {
        "ma20_60": compute_ma_cross(df, 20, 60, 30),
        "ma60_120": compute_ma_cross(df, 60, 120, 60),
    },
    "rsi": lambda df, **kw: compute_rsi(df, **kw),
    "macd": lambda df, **kw: compute_macd(df, **kw),
    "bollinger": lambda df, **kw: compute_bollinger(df, **kw),
    "stochastic": lambda df, **kw: compute_stochastic(df, **kw),
    "obv": lambda df, **kw: compute_obv(df, **kw),
    "volume": lambda df, **kw: compute_volume(df, **kw),
    "position": lambda df, **kw: compute_position(df, **kw),
    "candle": lambda df, **kw: compute_candle(df, **kw),
    "support_resistance": lambda df, **kw: compute_support_resistance(df, **kw),
    "volume_profile": lambda df, **kw: compute_volume_profile(df, **kw),
    "price_channel": lambda df, **kw: compute_price_channel(df, **kw),
}


AVAILABLE_INDICATORS = list(_INDICATOR_MAP.keys())


def compute_indicators(
    ohlcv: list[dict],
    include: list[str],
    params: dict | None = None,
) -> dict:
    """OHLCV와 요청 지표 키 리스트로 종합 지표 dict 생성.

    Args:
        ohlcv: OHLCV dict 리스트.
        include: 계산할 지표 키 리스트.
        params: 지표별 파라미터 오버라이드 dict. 예:
            {"rsi": {"period": 21}, "bollinger": {"std": 2.5}}
            지표 키별 dict가 그대로 compute_* 함수 kwargs로 전달됨.
    """
    df = _to_df(ohlcv)
    if df.empty:
        return {"error": "OHLCV 데이터가 비어 있습니다"}

    params = params or {}
    result = {}
    for key in include:
        fn = _INDICATOR_MAP.get(key)
        if fn is None:
            result[key] = {"error": f"지원하지 않는 지표: {key}"}
            continue
        kwargs = params.get(key) or {}
        try:
            result[key] = fn(df, **kwargs)
        except TypeError as e:
            result[key] = {"error": f"잘못된 파라미터: {e}"}
        except Exception as e:
            result[key] = {"error": f"{type(e).__name__}: {e}"}

    return result
