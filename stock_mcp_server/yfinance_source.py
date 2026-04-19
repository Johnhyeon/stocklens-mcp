"""yfinance를 통해 미국 주식 데이터를 수집하는 모듈.

yfinance는 동기 라이브러리이므로 asyncio.to_thread로 감싸 이벤트 루프를
블로킹하지 않도록 한다. 네이버 HTTP 스크레이핑과 달리 yfinance가 내부적으로
HTTP 세션·재시도·에러를 관리하므로 _http.fetch 경로는 사용하지 않는다.

결과는 _cache.cached_us() (NYSE 시간 기준)로 TTL 캐싱한다.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf

from stock_mcp_server._cache import cached_us


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}([-.][A-Z])?$")


def normalize_ticker(ticker: str) -> str:
    """yfinance가 기대하는 형식으로 정규화. BRK.B → BRK-B, 대문자화."""
    return ticker.strip().upper().replace(".", "-")


def is_us_ticker(symbol: str) -> bool:
    """1~5자 알파벳(+선택적 .X/-X 접미사)이면 US로 간주. KR은 6자리 영숫자."""
    return bool(_TICKER_RE.match(symbol.strip().upper()))


def _clean(value: Any) -> Any:
    """NaN/inf/pandas 타입을 JSON-safe 값으로 변환."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return value.item()
        except Exception:
            return None
    return value


def _df_to_records(df: pd.DataFrame | None, *, reset_index: bool = True) -> list[dict]:
    if df is None or df.empty:
        return []
    if reset_index:
        df = df.reset_index()
    records = df.to_dict(orient="records")
    return [{k: _clean(v) for k, v in row.items()} for row in records]


async def _in_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# --- info 캐시 ---
# ticker.info는 수십 개 필드를 한 번에 가져오는 비싼 호출이다.
# price/info/financial tool이 공유하게 한 번만 요청한다.

@cached_us(ttl_market=60, ttl_closed=3600)
async def get_info_raw(ticker: str) -> dict | None:
    """ticker.info 원본 dict. 존재하지 않는 티커면 None."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None
        return dict(info)

    return await _in_thread(_sync)


# --- Phase 1 MVP ---

async def get_price(ticker: str) -> dict | None:
    info = await get_info_raw(ticker)
    if info is None:
        return None
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev = info.get("regularMarketPreviousClose") or info.get("previousClose")
    change = None
    change_pct = None
    if price is not None and prev:
        change = price - prev
        change_pct = (change / prev) * 100 if prev else None
    return {
        "ticker": info.get("symbol"),
        "name": info.get("longName") or info.get("shortName"),
        "price": _clean(price),
        "change": _clean(change),
        "change_percent": _clean(change_pct),
        "previous_close": _clean(prev),
        "open": _clean(info.get("regularMarketOpen") or info.get("open")),
        "day_high": _clean(info.get("dayHigh") or info.get("regularMarketDayHigh")),
        "day_low": _clean(info.get("dayLow") or info.get("regularMarketDayLow")),
        "volume": _clean(info.get("regularMarketVolume") or info.get("volume")),
        "avg_volume": _clean(info.get("averageVolume")),
        "52w_high": _clean(info.get("fiftyTwoWeekHigh")),
        "52w_low": _clean(info.get("fiftyTwoWeekLow")),
        "beta": _clean(info.get("beta")),
        "market_cap": _clean(info.get("marketCap")),
        "currency": info.get("currency"),
        "market_state": info.get("marketState"),  # REGULAR / PRE / POST / CLOSED
        "exchange": info.get("exchange"),
    }


async def get_info(ticker: str) -> dict | None:
    info = await get_info_raw(ticker)
    if info is None:
        return None
    return {
        "ticker": info.get("symbol"),
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        "website": info.get("website"),
        "market_cap": _clean(info.get("marketCap")),
        "enterprise_value": _clean(info.get("enterpriseValue")),
        "shares_outstanding": _clean(info.get("sharesOutstanding")),
        "float_shares": _clean(info.get("floatShares")),
        "employees": _clean(info.get("fullTimeEmployees")),
        "exchange": info.get("exchange"),
        "quote_type": info.get("quoteType"),
        "business_summary": info.get("longBusinessSummary"),
    }


@cached_us(ttl_market=300, ttl_closed=3600)
async def get_history(
    ticker: str,
    period: str = "1mo",
    interval: str = "1d",
    prepost: bool = False,
) -> list[dict]:
    """OHLCV 이력.

    Args:
        period: '1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'
        interval: '1m','2m','5m','15m','30m','1h','1d','1wk','1mo'
        prepost: 프리/포스트 마켓 포함 (intraday interval에만 의미)
    """
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        df = t.history(period=period, interval=interval, prepost=prepost, auto_adjust=False)
        if df is None or df.empty:
            return []
        # Date/Datetime index -> column
        df = df.reset_index()
        # 컬럼명을 소문자·언더스코어로 정규화
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        return _df_to_records(df, reset_index=False)

    return await _in_thread(_sync)


async def get_financial_info(ticker: str) -> dict | None:
    info = await get_info_raw(ticker)
    if info is None:
        return None
    return {
        "ticker": info.get("symbol"),
        "trailing_pe": _clean(info.get("trailingPE")),
        "forward_pe": _clean(info.get("forwardPE")),
        "peg_ratio": _clean(info.get("trailingPegRatio")),
        "price_to_book": _clean(info.get("priceToBook")),
        "price_to_sales": _clean(info.get("priceToSalesTrailing12Months")),
        "eps_trailing": _clean(info.get("trailingEps")),
        "eps_forward": _clean(info.get("forwardEps")),
        "revenue_per_share": _clean(info.get("revenuePerShare")),
        "book_value": _clean(info.get("bookValue")),
        "return_on_equity": _clean(info.get("returnOnEquity")),
        "return_on_assets": _clean(info.get("returnOnAssets")),
        "profit_margin": _clean(info.get("profitMargins")),
        "operating_margin": _clean(info.get("operatingMargins")),
        "debt_to_equity": _clean(info.get("debtToEquity")),
        "current_ratio": _clean(info.get("currentRatio")),
        "dividend_yield": _clean(info.get("dividendYield")),
        "payout_ratio": _clean(info.get("payoutRatio")),
        "revenue_growth": _clean(info.get("revenueGrowth")),
        "earnings_growth": _clean(info.get("earningsGrowth")),
    }


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_earnings(ticker: str) -> dict | None:
    """실적 발표 일정 + 최근 서프라이즈 이력."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        upcoming: list[dict] = []
        history: list[dict] = []
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                ed = ed.reset_index()
                ed.columns = [str(c).lower().replace(" ", "_") for c in ed.columns]
                now_ts = pd.Timestamp.now(tz=ed["earnings_date"].dt.tz) if "earnings_date" in ed else pd.Timestamp.now(tz="UTC")
                for rec in _df_to_records(ed, reset_index=False):
                    date_str = rec.get("earnings_date")
                    if date_str and date_str >= now_ts.isoformat()[:10]:
                        upcoming.append(rec)
                    else:
                        history.append(rec)
        except Exception:
            pass

        return {
            "ticker": info.get("symbol"),
            "upcoming": upcoming[:4],
            "history": history[:8],  # 최근 8분기
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=1800, ttl_closed=86400)
async def get_analyst_ratings(ticker: str) -> dict | None:
    """애널리스트 목표주가 + buy/hold/sell 분포 + 최근 업·다운그레이드."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        targets: dict = {}
        try:
            apt = t.analyst_price_targets
            if isinstance(apt, dict):
                targets = {k: _clean(v) for k, v in apt.items()}
        except Exception:
            pass

        recommendations: list[dict] = []
        try:
            rec = t.recommendations
            if rec is not None and not rec.empty:
                recommendations = _df_to_records(rec)[:6]
        except Exception:
            pass

        updown: list[dict] = []
        try:
            ud = t.upgrades_downgrades
            if ud is not None and not ud.empty:
                updown = _df_to_records(ud)[:20]
        except Exception:
            pass

        return {
            "ticker": info.get("symbol"),
            "current_price": _clean(info.get("currentPrice") or info.get("regularMarketPrice")),
            "price_targets": targets,  # {current, high, low, mean, median}
            "recommendation_mean": _clean(info.get("recommendationMean")),
            "recommendation_key": info.get("recommendationKey"),  # strong_buy/buy/hold/sell/strong_sell
            "analyst_count": _clean(info.get("numberOfAnalystOpinions")),
            "recommendations_by_month": recommendations,
            "recent_upgrades_downgrades": updown,
        }

    return await _in_thread(_sync)


# --- Phase 2 ---

@cached_us(ttl_market=120, ttl_closed=3600)
async def get_options(
    ticker: str,
    expiration: str | None = None,
    strikes_around_spot: int = 10,
) -> dict | None:
    """옵션 체인 (calls/puts). 기본: 최근접 만기 + 현재가 근처 strike 20개."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None
        expirations = list(t.options or ())
        if not expirations:
            return {
                "ticker": info.get("symbol"),
                "expirations": [],
                "selected_expiration": None,
                "calls": [],
                "puts": [],
            }
        exp = expiration or expirations[0]
        if exp not in expirations:
            exp = expirations[0]

        chain = t.option_chain(exp)
        spot = info.get("currentPrice") or info.get("regularMarketPrice")

        def _slice(df: pd.DataFrame) -> list[dict]:
            if df is None or df.empty:
                return []
            df = df.copy()
            if spot is not None and "strike" in df.columns:
                df["_dist"] = (df["strike"] - spot).abs()
                df = df.sort_values("_dist").head(strikes_around_spot * 2).sort_values("strike")
                df = df.drop(columns=["_dist"])
            else:
                df = df.head(strikes_around_spot * 2)
            return _df_to_records(df, reset_index=False)

        return {
            "ticker": info.get("symbol"),
            "spot": _clean(spot),
            "expirations": expirations,
            "selected_expiration": exp,
            "calls": _slice(chain.calls),
            "puts": _slice(chain.puts),
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_insider(ticker: str) -> dict | None:
    """Form 4 내부자 거래 + 최근 6개월 매수/매도 요약."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        transactions: list[dict] = []
        try:
            tx = t.insider_transactions
            if tx is not None and not tx.empty:
                tx.columns = [str(c).lower().replace(" ", "_") for c in tx.columns]
                transactions = _df_to_records(tx)[:20]
        except Exception:
            pass

        purchases_summary: dict = {}
        try:
            ip = t.insider_purchases
            if ip is not None and not ip.empty:
                # 이상한 형태: label 컬럼 + Shares/Trans 컬럼
                label_col = ip.columns[0]
                for idx, row in ip.iterrows():
                    label = str(row.get(label_col, idx))
                    purchases_summary[label] = {
                        "shares": _clean(row.get("Shares")),
                        "trans": _clean(row.get("Trans")),
                    }
        except Exception:
            pass

        return {
            "ticker": info.get("symbol"),
            "purchases_last_6m": purchases_summary,
            "recent_transactions": transactions,
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_holders(ticker: str) -> dict | None:
    """기관 투자자 (13F) + 뮤추얼 펀드 보유 현황."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        institutional: list[dict] = []
        try:
            ih = t.institutional_holders
            if ih is not None and not ih.empty:
                ih.columns = [str(c).lower().replace(" ", "_") for c in ih.columns]
                institutional = _df_to_records(ih)[:10]
        except Exception:
            pass

        mutualfund: list[dict] = []
        try:
            mf = t.mutualfund_holders
            if mf is not None and not mf.empty:
                mf.columns = [str(c).lower().replace(" ", "_") for c in mf.columns]
                mutualfund = _df_to_records(mf)[:10]
        except Exception:
            pass

        return {
            "ticker": info.get("symbol"),
            "held_pct_institutions": _clean(info.get("heldPercentInstitutions")),
            "held_pct_insiders": _clean(info.get("heldPercentInsiders")),
            "institutional_holders": institutional,
            "mutualfund_holders": mutualfund,
        }

    return await _in_thread(_sync)


async def get_short_interest(ticker: str) -> dict | None:
    """공매도 지표. 2~4주 stale (FINRA bi-monthly 공시)."""
    info = await get_info_raw(ticker)
    if info is None:
        return None
    return {
        "ticker": info.get("symbol"),
        "shares_short": _clean(info.get("sharesShort")),
        "short_ratio": _clean(info.get("shortRatio")),  # days to cover
        "short_percent_of_float": _clean(info.get("shortPercentOfFloat")),
        "shares_short_prior_month": _clean(info.get("sharesShortPriorMonth")),
        "date_short_interest": _clean(info.get("dateShortInterest")),  # unix ts
        "prior_month_date": _clean(info.get("sharesShortPreviousMonthDate")),
        "float_shares": _clean(info.get("floatShares")),
    }


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_sec_filings(ticker: str, limit: int = 15) -> dict | None:
    """SEC 공시 목록 (10-K, 10-Q, 8-K + EDGAR URL)."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        filings: list[dict] = []
        try:
            sec = t.sec_filings or []
            for f in sec[:limit]:
                filings.append({
                    "date": _clean(f.get("date")),
                    "type": f.get("type"),
                    "title": f.get("title"),
                    "edgar_url": f.get("edgarUrl"),
                    "exhibits": f.get("exhibits", {}),
                })
        except Exception:
            pass

        return {"ticker": info.get("symbol"), "filings": filings}

    return await _in_thread(_sync)


# --- Phase 3 확장 ---

# 10종 predefined screener (yf.PREDEFINED_SCREENER_QUERIES 키)
PREDEFINED_SCREENERS = [
    "day_gainers", "day_losers", "most_actives", "most_shorted_stocks",
    "aggressive_small_caps", "growth_technology_stocks",
    "undervalued_growth_stocks", "undervalued_large_caps",
    "small_cap_gainers", "conservative_foreign_funds",
]

# 재무제표 주요 row 화이트리스트 (수십 row 중 투자자 관심 높은 항목만)
_INCOME_ROWS = [
    "Total Revenue", "Cost Of Revenue", "Gross Profit",
    "Operating Income", "Operating Expense", "Research And Development",
    "EBITDA", "EBIT", "Net Income", "Diluted EPS", "Basic EPS",
    "Interest Expense", "Tax Provision",
]
_BALANCE_ROWS = [
    "Total Assets", "Total Liabilities Net Minority Interest",
    "Stockholders Equity", "Total Debt", "Net Debt",
    "Cash And Cash Equivalents", "Working Capital",
    "Ordinary Shares Number", "Total Capitalization", "Retained Earnings",
]
_CASHFLOW_ROWS = [
    "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
    "Free Cash Flow", "Capital Expenditure",
    "Repurchase Of Capital Stock", "Cash Dividends Paid",
    "Changes In Cash", "End Cash Position",
]


@cached_us(ttl_market=3600, ttl_closed=86400)
async def search(query: str, limit: int = 8) -> list[dict]:
    """종목명 또는 티커로 미국 주식 검색. yf.Search 활용."""

    def _sync():
        try:
            s = yf.Search(query, max_results=max(limit, 5))
            quotes = (s.response or {}).get("quotes") or []
        except Exception:
            return []
        out = []
        for q in quotes[:limit]:
            if q.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            out.append({
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname"),
                "exchange": q.get("exchDisp") or q.get("exchange"),
                "type": q.get("typeDisp") or q.get("quoteType"),
                "sector": q.get("sectorDisp") or q.get("sector"),
                "industry": q.get("industryDisp") or q.get("industry"),
            })
        return out

    return await _in_thread(_sync)


@cached_us(ttl_market=60, ttl_closed=3600)
async def get_market_summary() -> dict:
    """미국 주요 지수 (S&P 500, Dow, Nasdaq 등)."""

    def _sync():
        try:
            summary = yf.Market("US").summary or {}
        except Exception:
            return {}
        # Key mapping (Yahoo 내부 키 → 가독 이름)
        # CMX는 COMEX 금 선물이라 고정 라벨이 어긋날 수 있어, shortName 우선 사용
        label_map = {
            "SNP": "S&P 500", "DJI": "Dow Jones", "NIM": "NASDAQ",
            "WCB": "Russell 2000", "CXI": "VIX",
        }
        indices = []
        for key, data in summary.items():
            if not isinstance(data, dict):
                continue
            name = data.get("shortName") or data.get("longName")
            indices.append({
                "key": key,
                "label": label_map.get(key, name or key),
                "symbol": data.get("symbol"),
                "name": name,
                "price": _clean(data.get("regularMarketPrice")),
                "change": _clean(data.get("regularMarketChange")),
                "change_percent": _clean(data.get("regularMarketChangePercent")),
                "previous_close": _clean(data.get("regularMarketPreviousClose")),
                "market_state": data.get("marketState"),
            })
        return {"indices": indices}

    return await _in_thread(_sync)


@cached_us(ttl_market=120, ttl_closed=1800)
async def screen(preset: str, count: int = 20) -> dict:
    """Yahoo predefined 스크리너 (day_gainers 등)."""
    if preset not in PREDEFINED_SCREENERS:
        return {"error": f"Unknown preset. Available: {PREDEFINED_SCREENERS}"}

    def _sync():
        try:
            r = yf.screen(preset, count=count)
        except Exception as e:
            return {"error": str(e)}
        quotes = r.get("quotes") or []
        out = []
        for q in quotes:
            out.append({
                "symbol": q.get("symbol"),
                "name": q.get("longName") or q.get("shortName"),
                "price": _clean(q.get("regularMarketPrice")),
                "change": _clean(q.get("regularMarketChange")),
                "change_percent": _clean(q.get("regularMarketChangePercent")),
                "volume": _clean(q.get("regularMarketVolume")),
                "market_cap": _clean(q.get("marketCap")),
                "pe": _clean(q.get("trailingPE")),
                "exchange": q.get("fullExchangeName") or q.get("exchange"),
            })
        return {
            "preset": preset,
            "title": r.get("title"),
            "description": r.get("description"),
            "total": r.get("total"),
            "quotes": out,
        }

    return await _in_thread(_sync)


def _extract_statement_rows(df: pd.DataFrame, whitelist: list[str]) -> list[dict]:
    """재무제표 DataFrame에서 화이트리스트 row만 뽑아 기간별 값 리스트로 변환."""
    if df is None or df.empty:
        return []
    cols = [str(c)[:10] for c in df.columns]  # 기간 (YYYY-MM-DD)
    out = []
    for row_name in whitelist:
        if row_name in df.index:
            values = df.loc[row_name].tolist()
            out.append({
                "item": row_name,
                "periods": cols,
                "values": [_clean(v) for v in values],
            })
    return out


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_financial_statement(
    ticker: str,
    statement_type: str = "income",
    period: str = "annual",
) -> dict | None:
    """재무제표 3종 (income/balance/cash_flow), annual/quarterly."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        prop_map = {
            ("income", "annual"): ("income_stmt", _INCOME_ROWS),
            ("income", "quarterly"): ("quarterly_income_stmt", _INCOME_ROWS),
            ("balance", "annual"): ("balance_sheet", _BALANCE_ROWS),
            ("balance", "quarterly"): ("quarterly_balance_sheet", _BALANCE_ROWS),
            ("cash_flow", "annual"): ("cash_flow", _CASHFLOW_ROWS),
            ("cash_flow", "quarterly"): ("quarterly_cash_flow", _CASHFLOW_ROWS),
        }
        key = (statement_type, period)
        if key not in prop_map:
            return {
                "error": "Invalid statement_type/period. "
                "statement_type ∈ {income,balance,cash_flow}, period ∈ {annual,quarterly}."
            }
        attr, whitelist = prop_map[key]
        try:
            df = getattr(t, attr)
        except Exception:
            df = None

        return {
            "ticker": info.get("symbol"),
            "statement_type": statement_type,
            "period": period,
            "currency": info.get("financialCurrency") or info.get("currency"),
            "rows": _extract_statement_rows(df, whitelist),
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_sector(sector_key: str, top_n: int = 20) -> dict | None:
    """섹터별 overview + top companies. sector_key 예: technology, healthcare."""
    # Yahoo는 소문자·하이픈을 기대
    sector_key_norm = (sector_key or "").strip().lower().replace(" ", "-").replace("_", "-")

    def _sync():
        if not sector_key_norm:
            return {"error": "빈 섹터 키. 예: technology, healthcare, financial-services."}
        try:
            s = yf.Sector(sector_key_norm)
            overview = s.overview or {}
            tc = s.top_companies
        except Exception as e:
            return {"error": f"섹터 '{sector_key_norm}' 조회 실패: {e}"}

        companies = []
        if tc is not None and not tc.empty:
            tc = tc.reset_index()
            tc.columns = [str(c).lower().replace(" ", "_") for c in tc.columns]
            for rec in _df_to_records(tc, reset_index=False)[:top_n]:
                companies.append(rec)

        # overview가 비어있으면 섹터가 유효하지 않은 것
        if not overview and (tc is None or tc.empty):
            return {"error": f"섹터 '{sector_key_norm}' 데이터 없음. 유효 섹터: technology, healthcare, financial-services, consumer-cyclical, consumer-defensive, communication-services, industrials, energy, basic-materials, utilities, real-estate."}
        return {
            "sector": sector_key_norm,
            "description": overview.get("description"),
            "companies_count": _clean(overview.get("companies_count")),
            "market_cap": _clean(overview.get("market_cap")),
            "market_weight": _clean(overview.get("market_weight")),
            "industries_count": _clean(overview.get("industries_count")),
            "employee_count": _clean(overview.get("employee_count")),
            "top_companies": companies,
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_etf_info(ticker: str) -> dict | None:
    """ETF 전용 상세 정보 (funds_data). ETF가 아니면 None."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None
        if info.get("quoteType") != "ETF":
            return {"error": f"{info.get('symbol')}는 ETF가 아닙니다 (quoteType={info.get('quoteType')})."}

        fd = t.funds_data
        overview = {}
        asset_classes = {}
        sector_weightings = {}
        top_holdings: list[dict] = []
        fund_ops: list[dict] = []

        try:
            overview = fd.fund_overview or {}
        except Exception:
            pass
        try:
            asset_classes = {k: _clean(v) for k, v in (fd.asset_classes or {}).items()}
        except Exception:
            pass
        try:
            sector_weightings = {k: _clean(v) for k, v in (fd.sector_weightings or {}).items()}
        except Exception:
            pass
        try:
            th = fd.top_holdings
            if th is not None and not th.empty:
                th = th.reset_index()
                th.columns = [str(c).lower().replace(" ", "_") for c in th.columns]
                top_holdings = _df_to_records(th, reset_index=False)
        except Exception:
            pass
        try:
            fo = fd.fund_operations
            if fo is not None and not fo.empty:
                fo = fo.reset_index()
                fo.columns = [str(c).lower().replace(" ", "_") for c in fo.columns]
                fund_ops = _df_to_records(fo, reset_index=False)
        except Exception:
            pass

        try:
            description = fd.description
        except Exception:
            description = None

        return {
            "ticker": info.get("symbol"),
            "name": info.get("longName") or info.get("shortName"),
            "description": description,
            "category": overview.get("categoryName"),
            "family": overview.get("family"),
            "legal_type": overview.get("legalType"),
            "expense_ratio": _clean(info.get("netExpenseRatio")),
            "total_assets": _clean(info.get("totalAssets")),
            "ytd_return": _clean(info.get("ytdReturn")),
            "three_year_avg_return": _clean(info.get("threeYearAverageReturn")),
            "asset_classes": asset_classes,
            "sector_weightings": sector_weightings,
            "top_holdings": top_holdings,
            "fund_operations": fund_ops,
        }

    return await _in_thread(_sync)


async def get_multi_prices(tickers: list[str]) -> list[dict]:
    """여러 티커 일괄 가격 스냅샷. 티커별 병렬 호출 (asyncio.gather).

    get_price는 내부적으로 get_info_raw를 쓰므로 캐시 공유. 콜드 상태에서도
    asyncio.to_thread가 ThreadPool로 동시 실행해 30 티커 ~1-2초 수준.
    """
    async def _one(sym: str) -> dict:
        try:
            r = await get_price(sym)
            if r is None:
                return {"ticker": sym, "error": "not found"}
            return r
        except Exception as e:
            return {"ticker": sym, "error": f"{type(e).__name__}: {str(e)[:60]}"}

    return await asyncio.gather(*(_one(t) for t in tickers))


@cached_us(ttl_market=1800, ttl_closed=86400)
async def get_analyst_estimates(ticker: str) -> dict | None:
    """EPS/Revenue estimate + eps_revisions + eps_trend + growth_estimates."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        def _df(attr: str) -> list[dict]:
            try:
                df = getattr(t, attr)
                if df is None or df.empty:
                    return []
                df = df.reset_index()
                df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
                return _df_to_records(df, reset_index=False)
            except Exception:
                return []

        return {
            "ticker": info.get("symbol"),
            "earnings_estimate": _df("earnings_estimate"),
            "revenue_estimate": _df("revenue_estimate"),
            "eps_revisions": _df("eps_revisions"),
            "eps_trend": _df("eps_trend"),
            "growth_estimates": _df("growth_estimates"),
        }

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_major_holders(ticker: str) -> dict | None:
    """주주 비중 요약 (insider/institution %)."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None
        try:
            mh = t.major_holders
            summary: dict = {}
            if mh is not None and not mh.empty:
                # index는 라벨, 첫 컬럼은 값
                for idx, row in mh.iterrows():
                    summary[str(idx)] = _clean(row.iloc[0])
        except Exception:
            summary = {}
        return {"ticker": info.get("symbol"), "summary": summary}

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_insider_roster(ticker: str) -> list[dict]:
    """현재 내부자 명단 + 보유주식수."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        try:
            r = t.insider_roster_holders
            if r is None or r.empty:
                return []
            r.columns = [str(c).lower().replace(" ", "_") for c in r.columns]
            return _df_to_records(r)
        except Exception:
            return []

    return await _in_thread(_sync)


@cached_us(ttl_market=300, ttl_closed=3600)
async def get_news(ticker: str, limit: int = 10) -> dict | None:
    """yfinance 뉴스 헤드라인."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        items: list[dict] = []
        try:
            raw = t.news or []
            for n in raw[:limit]:
                content = n.get("content") or n
                provider = content.get("provider") or {}
                canonical = content.get("canonicalUrl") or {}
                items.append({
                    "title": content.get("title"),
                    "summary": content.get("summary") or content.get("description"),
                    "published": content.get("pubDate"),
                    "provider": provider.get("displayName"),
                    "url": canonical.get("url"),
                })
        except Exception:
            pass
        return {"ticker": norm, "news": items}

    return await _in_thread(_sync)


@cached_us(ttl_market=3600, ttl_closed=86400)
async def get_dividends(ticker: str, limit: int = 20) -> dict | None:
    """배당 이력 + ex-date + yield + payout ratio."""
    norm = normalize_ticker(ticker)

    def _sync():
        t = yf.Ticker(norm)
        info = t.info or {}
        if not info.get("symbol"):
            return None

        history: list[dict] = []
        try:
            div = t.dividends
            if div is not None and not div.empty:
                div = div.tail(limit).reset_index()
                div.columns = [str(c).lower() for c in div.columns]
                history = _df_to_records(div, reset_index=False)
        except Exception:
            pass

        return {
            "ticker": info.get("symbol"),
            "dividend_yield": _clean(info.get("dividendYield")),
            "dividend_rate": _clean(info.get("dividendRate")),
            "ex_dividend_date": _clean(info.get("exDividendDate")),
            "payout_ratio": _clean(info.get("payoutRatio")),
            "five_year_avg_yield": _clean(info.get("fiveYearAvgDividendYield")),
            "last_dividend_value": _clean(info.get("lastDividendValue")),
            "last_dividend_date": _clean(info.get("lastDividendDate")),
            "history": history,
        }

    return await _in_thread(_sync)
