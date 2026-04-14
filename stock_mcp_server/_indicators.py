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


# ─────────────────────────────────────────────────────────────
# 이동평균 & Phase
# ─────────────────────────────────────────────────────────────

_MA_PERIODS = (5, 20, 60, 120, 240)


def compute_ma(df: pd.DataFrame, periods: tuple[int, ...] = _MA_PERIODS) -> dict:
    """각 기간의 이동평균 최신값."""
    result = {}
    for p in periods:
        if len(df) >= p:
            result[f"ma{p}"] = round(float(df["close"].rolling(p).mean().iloc[-1]), 2)
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

    return {"phase": phase, "pairs": pairs}


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
            return {"type": "golden", "days_ago": i, "short": short, "long": long}
        if prev >= 0 > curr:
            return {"type": "dead", "days_ago": i, "short": short, "long": long}

    return {"type": None, "days_ago": None, "short": short, "long": long}


# ─────────────────────────────────────────────────────────────
# RSI / MACD / Bollinger / Stochastic / OBV
# ─────────────────────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> dict:
    if len(df) < period + 1:
        return {"value": None, "state": None}

    if _HAS_PANDAS_TA:
        rsi = ta.rsi(df["close"], length=period)
    else:
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = -delta.clip(upper=0).rolling(period).mean()
        rs = gain / loss
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

    cross = {"type": None, "days_ago": None}
    for i in range(1, min(30, len(hist) - 1)):
        prev = hist.iloc[-i - 1]
        curr = hist.iloc[-i]
        if pd.isna(prev) or pd.isna(curr):
            continue
        if prev <= 0 < curr:
            cross = {"type": "golden", "days_ago": i}
            break
        if prev >= 0 > curr:
            cross = {"type": "dead", "days_ago": i}
            break

    return {
        "macd": round(float(macd_line.iloc[-1]), 2) if not pd.isna(macd_line.iloc[-1]) else None,
        "signal": round(float(signal_line.iloc[-1]), 2) if not pd.isna(signal_line.iloc[-1]) else None,
        "histogram": round(float(hist.iloc[-1]), 2) if not pd.isna(hist.iloc[-1]) else None,
        "cross": cross,
    }


def compute_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> dict:
    if len(df) < period:
        return {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth": None}

    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std()
    upper = mid + std * sd
    lower = mid - std * sd

    price = df["close"].iloc[-1]
    u, m, l = upper.iloc[-1], mid.iloc[-1], lower.iloc[-1]
    if pd.isna(u) or pd.isna(l) or u == l:
        return {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth": None}

    percent_b = (price - l) / (u - l)
    bandwidth = (u - l) / m * 100

    return {
        "upper": round(float(u), 2),
        "middle": round(float(m), 2),
        "lower": round(float(l), 2),
        "percent_b": round(float(percent_b), 3),
        "bandwidth": round(float(bandwidth), 2),
        "period": period,
    }


def compute_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> dict:
    if len(df) < k_period + d_period:
        return {"k": None, "d": None, "state": None}

    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()

    kv, dv = k.iloc[-1], d.iloc[-1]
    if pd.isna(kv) or pd.isna(dv):
        return {"k": None, "d": None, "state": None}

    state = "과매수" if kv >= 80 else "과매도" if kv <= 20 else "중립"
    return {
        "k": round(float(kv), 2),
        "d": round(float(dv), 2),
        "state": state,
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
            "price_range": [int(price_low), int(price_high)],
            "avg_price": int(avg_price),
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
        "current_price": int(current_price),
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
            "price_range": [int(bin_low), int(bin_high)],
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
            "low": int(va_low),
            "high": int(va_high),
            "coverage_pct": round(cumsum, 1),
        },
        "current_price": int(current),
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
        "upper": int(upper),
        "lower": int(lower),
        "midline": int(midline),
        "current": int(current),
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

_INDICATOR_MAP = {
    "ma": lambda df: compute_ma(df),
    "ma_phase": lambda df: compute_ma_phase(df),
    "ma_slope": lambda df: {
        "ma20_slope_pct": compute_ma_slope(df, 20, 10),
        "ma60_slope_pct": compute_ma_slope(df, 60, 20),
        "ma120_slope_pct": compute_ma_slope(df, 120, 20),
    },
    "ma_cross": lambda df: {
        "ma20_60": compute_ma_cross(df, 20, 60, 30),
        "ma60_120": compute_ma_cross(df, 60, 120, 60),
    },
    "rsi": lambda df: compute_rsi(df),
    "macd": lambda df: compute_macd(df),
    "bollinger": lambda df: compute_bollinger(df),
    "stochastic": lambda df: compute_stochastic(df),
    "obv": lambda df: compute_obv(df),
    "volume": lambda df: compute_volume(df),
    "position": lambda df: compute_position(df),
    "candle": lambda df: compute_candle(df),
    "support_resistance": lambda df: compute_support_resistance(df),
    "volume_profile": lambda df: compute_volume_profile(df),
    "price_channel": lambda df: compute_price_channel(df),
}


AVAILABLE_INDICATORS = list(_INDICATOR_MAP.keys())


def compute_indicators(ohlcv: list[dict], include: list[str]) -> dict:
    """OHLCV와 요청 지표 키 리스트로 종합 지표 dict 생성."""
    df = _to_df(ohlcv)
    if df.empty:
        return {"error": "OHLCV 데이터가 비어 있습니다"}

    result = {}
    for key in include:
        fn = _INDICATOR_MAP.get(key)
        if fn is None:
            result[key] = {"error": f"지원하지 않는 지표: {key}"}
            continue
        try:
            result[key] = fn(df)
        except Exception as e:
            result[key] = {"error": f"{type(e).__name__}: {e}"}

    return result
