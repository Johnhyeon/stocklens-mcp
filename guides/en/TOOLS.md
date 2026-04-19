# StockLens Tool Reference

**47 tools total** — Korean 27 + US 20. As of v0.3.0.

[🇰🇷 한국어](../ko/TOOLS.md) | [USAGE](USAGE.md) | [INSTALL](INSTALL.md)

---

## 🇰🇷 Korean Stocks (27)

Data source: Naver Finance (public data, no API key).

### Basic Lookup (6)

#### `search` / `search_stock`
Find stocks by name or code (same tool, two names).

- `query` (str): company name (Korean/English) or 6-digit code
- Examples: `"Samsung Electronics code"`, `"search SK Hynix"`

⚠️ If you only know the name, **always call this first** — never guess 6-digit codes.

#### `get_price`
Current price + open/high/low/volume snapshot.
- `code` (str): 6-digit Korean ticker
- Example: `"Samsung current price?"`

#### `get_chart`
OHLCV time series (daily/weekly/monthly).
- `code` (str), `timeframe` (`day|week|month`, default `day`), `count` (int, default 120, max 500)
- Example: `"SK Hynix 120-day candles"`, `"Kakao 60 weekly bars"`

#### `get_flow`
Investor flow (foreign/institutional net buying, daily).
- `code` (str), `days` (int, default 20, max 60)
- Note: Naver does not provide retail investor flow column.

#### `get_financial`
Financial metrics (PER, PBR, market cap, EPS, BPS).

#### `get_index`
KOSPI / KOSDAQ index snapshot. No parameters.

---

### Technical Indicators (2)

#### `get_indicators`
15 indicators for a single stock (MA/RSI/MACD/Bollinger/Stochastic/OBV/support-resistance, etc.).
- `code` (str), `days` (int, default 260), `include` (list), `timeframe` (`day|week|month`)
- Available: `ma`, `ma_phase`, `ma_slope`, `ma_cross`, `rsi`, `macd`, `bollinger`, `stochastic`, `obv`, `volume`, `position`, `candle`, `support_resistance`, `volume_profile`, `price_channel`

⚠️ For scoring/screening. Use `get_chart` for chart visualization.

#### `get_indicators_bulk`
Parallel indicators across multiple stocks (core screening tool).
- `codes` (list, max 50), `days`, `include`

---

### Screening (7)

#### `list_themes`
Naver theme list sorted by change %.
- `page` (int, default 1, max 7)

#### `get_theme_stocks`
Stocks within a theme.
- `theme_name` (str, partial match), `count` (int, default 30, max 50), `include_reason` (bool)

#### `list_sectors`
Sector list (~79 sectors).

#### `get_sector_stocks`
Stocks within a sector.
- `sector_name` (str, partial match), `count`

#### `get_volume_ranking` / `get_change_ranking` / `get_market_cap_ranking`
Top stocks by volume / change / market cap.
- `market` (`KOSPI|KOSDAQ|ALL`), `count` (int, default 50, max 500), `direction` (for `change_ranking`: `up|down`)

---

### Bulk Query (2)

#### `get_multi_stocks`
Basic info for many stocks in parallel (current price, volume).
- `codes` (list, max 30)
- Much faster than N individual `get_price` calls.

#### `get_multi_chart_stats`
Chart stats for many stocks (52-week high/low/drawdown/return/avg volume).
- `codes` (list, max 100), `days` (int, default 260)

---

### ETF (2)

#### `get_etf_list`
1,000+ Korean ETFs with category filter and sorting.
- `category` (one of 7), `sort_by` (`market_cap|volume|return_*|dividend_yield`), `limit`

#### `get_etf_info`
Single ETF detail (underlying index, total expense ratio, top holdings, 1/3/6/12M returns).

---

### Analysis & Disclosure (3)

#### `get_consensus`
Sell-side consensus (price target, recommendation distribution, earnings estimates).

#### `get_reports`
Sell-side reports with summaries + PDF links.
- `code`, `limit` (int)

#### `get_disclosure`
DART disclosure list (title, date, source).
- `code`, `limit`

---

### Excel Export (3)

#### `export_to_excel`
Save a single stock's data to Excel (for uploading to Gemini/GPT).
- `data_type` (`chart|flow|financial`), `code`, `days`, `filename`

#### `scan_to_excel`
Snapshot many stocks to Excel. Scan once (10~20s) → repeat queries via `query_excel` (sub-ms).
- `codes` (list, max 500), `days`, `include_financial` (bool), `filename`

#### `query_excel`
Filter / sort a saved snapshot.
- `file_path`, `filters` (dict), `sort_by`, `descending`, `limit`

---

### Diagnostics (1)

#### `get_metrics_summary`
Tool usage + token statistics.
- `days` (int, default 1, max 30)
- Log location: `~/Downloads/kstock/logs/metrics_YYYYMMDD.jsonl`

---

## 🇺🇸 US Stocks (20)

Data source: **Yahoo Finance (yfinance)**. No API key, up to 15-minute delay.
Ticker auto-detected: 1~5 alphabetic characters (with optional `.`/`-`) = US. BRK.B is normalized to `BRK-B` internally.

### Discovery & Market (4)

#### `get_us_search`
Company name → ticker lookup.
- `query` (str)
- Examples: `"Apple ticker"`, `"search NVIDIA"`
⚠️ Call this first if you only know the name.

#### `get_us_market`
Major index snapshot (S&P 500, Dow, Nasdaq, Russell 2000, VIX). No parameters.

#### `get_us_screener`
10 Yahoo predefined screeners.
- `preset`: `day_gainers`, `day_losers`, `most_actives`, `most_shorted_stocks`, `aggressive_small_caps`, `growth_technology_stocks`, `undervalued_growth_stocks`, `undervalued_large_caps`, `small_cap_gainers`, `conservative_foreign_funds`
- `count` (int)

#### `get_us_sector`
Sector overview + top companies.
- `sector_key`: `technology`, `healthcare`, `financial-services`, `consumer-cyclical`, `consumer-defensive`, `communication-services`, `industrials`, `energy`, `basic-materials`, `utilities`, `real-estate`
- `top_n` (int)

---

### Basic Data (6)

#### `get_us_price`
Price + daily change + 52-week range + beta + market cap + market state.
- `ticker` (str): e.g. `"AAPL"`, `"TSLA"`, `"BRK.B"`

#### `get_us_info`
Company profile (sector, industry, market cap, business summary).

#### `get_us_chart`
OHLCV time series. **Capped at 500 rows (auto-truncated, token protection)**.
- `ticker`, `period` (`1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max`, default `3mo`), `interval` (`1m/5m/15m/30m/1h/1d/1wk/1mo`, default `1d`), `prepost` (bool, pre/post-market)

#### `get_us_financials`
Valuation (P/E, Forward P/E, PEG, P/B) + Profitability (ROE, margins) + Dividend ratios.

#### `get_us_financial_statement`
Three core financial statements.
- `ticker`, `statement_type` (`income|balance|cash_flow`), `period` (`annual|quarterly`)
- Key rows only (Total Revenue, Net Income, Total Assets, Free Cash Flow, etc.)

#### `get_us_multi_price`
Batch price snapshot, parallel (~1~2s for 30 tickers).
- `tickers` (list, max 30)

---

### US-specific Data (10)

#### `get_us_earnings`
Next earnings date + recent EPS surprise history (last 8 quarters).

#### `get_us_analyst`
Price targets (mean/median/high/low) + buy/hold/sell distribution + upgrade/downgrade history + EPS/revenue estimates.

#### `get_us_dividends`
Dividend history + ex-date + yield + payout ratio + 5-year avg.
- `ticker`, `limit` (int, default 12)

#### `get_us_options`
Options chain (calls/puts, IV, OI).
- `ticker`, `expiration` (date, defaults to nearest), `strikes_around_spot` (int, default 10)
- ⚠️ No Greeks (Δ/Γ/Θ) — yfinance doesn't provide them.

#### `get_us_insider`
Form 4 insider transactions + 6-month net purchases summary + current insider roster.

#### `get_us_holders`
Institutional holders (13F) + mutual funds + breakdown (insiders % / institutions %).

#### `get_us_short`
Short interest metrics (% of float, days to cover).
- ⚠️ FINRA bi-monthly filings, so 2~4 weeks stale. Response includes `date_short_interest` marker.

#### `get_us_filings`
SEC filings list (10-K, 10-Q, 8-K) + EDGAR URLs.
- `ticker`, `limit` (int, default 15)

#### `get_us_news`
Latest news headlines.
- `ticker`, `limit` (int, default 10)

#### `get_us_etf_info`
ETF-specific detail (top holdings, sector weightings, asset allocation, expense ratio, YTD return).
- `ticker`: SPY, QQQ, SCHD, VTI, etc.

---

## 📁 File Storage

Excel snapshots + metric logs:
- Windows: `%USERPROFILE%\Downloads\kstock\`
- macOS/Linux: `~/Downloads/kstock/`

Stored locally only. Nothing leaves your machine.

---

## ⚠️ Known Limitations

- **Naver HTML structure changes** may break some fields — re-verified each release
- **Yahoo Finance 15-minute delay** — no real-time quotes, dark pool, or Level 2
- **No options Greeks** — yfinance doesn't provide them
- **BRK.B SEC filings** — yfinance data gap, query `BRK-A` instead
- **Short interest is 2~4 weeks stale** — FINRA filing schedule

Full quality verification: [QUALITY.md](../../QUALITY.md)
