# StockLens 도구 레퍼런스

총 **47개 도구** — 한국 주식 27 + 미국 주식 20. v0.3.0 기준.

[🇺🇸 English](../en/TOOLS.md) | [USAGE](USAGE.md) | [INSTALL](INSTALL.md)

---

## 🇰🇷 한국 주식 (27)

데이터 소스: 네이버 증권 (공개 데이터, API 키 불필요).

### 기본 조회 (6)

#### `search` / `search_stock`
종목명·코드로 검색 (같은 도구, 이름만 다름).

- `query` (str): 종목명(한/영) 또는 6자리 코드
- 예: `"삼성전자 종목코드"`, `"오이솔루션 검색"`

⚠️ 종목명만 알고 코드 모를 때 **반드시 먼저 호출** (다른 도구에 코드 추측으로 넣지 말 것).

#### `get_price`
현재가 + 시가·고가·저가·거래량 스냅샷.
- `code` (str): 6자리 종목코드
- 예: `"삼성전자 지금 얼마?"`

#### `get_chart`
OHLCV 시계열 (일/주/월봉).
- `code` (str), `timeframe` (`day|week|month`, 기본 `day`), `count` (int, 기본 120, 최대 500)
- 예: `"SK하이닉스 120일 일봉"`, `"카카오 주봉 60개"`

#### `get_flow`
투자자별 수급 (외국인/기관 순매매, 일별).
- `code` (str), `days` (int, 기본 20, 최대 60)
- 예: `"카카오 20일 외국인 수급"`
- 참고: 네이버 증권은 개인 순매매 컬럼 미제공.

#### `get_financial`
재무지표 (PER, PBR, 시가총액, EPS, BPS).
- `code` (str)
- 예: `"네이버 PER PBR"`, `"현대차 재무"`

#### `get_index`
KOSPI / KOSDAQ 지수 현재값.
- 파라미터 없음
- 예: `"오늘 코스피 어때?"`

---

### 기술지표 (2)

#### `get_indicators`
단일 종목의 15종 기술지표 판정 (이평선·RSI·MACD·볼린저·스토캐스틱·OBV·지지저항 등).
- `code` (str), `days` (int, 기본 260), `include` (list, 기본 4종), `timeframe` (`day|week|month`)
- 사용 가능: `ma`, `ma_phase`, `ma_slope`, `ma_cross`, `rsi`, `macd`, `bollinger`, `stochastic`, `obv`, `volume`, `position`, `candle`, `support_resistance`, `volume_profile`, `price_channel`
- 예: `"삼성전자 RSI MACD 판정"`

⚠️ 판정·스크리닝용. 차트 시각화는 `get_chart`.

#### `get_indicators_bulk`
여러 종목 지표를 병렬 계산 (스크리닝 핵심).
- `codes` (list, 최대 50), `days`, `include`
- 예: `"시총 30개 종목 RSI + MACD 한번에"`

---

### 스크리닝 (7)

#### `list_themes`
네이버 증권 테마 목록 (등락률 순, 페이지당 40개).
- `page` (int, 기본 1, 최대 7)

#### `get_theme_stocks`
테마 내 종목 리스트.
- `theme_name` (str, 부분 매칭), `count` (int, 기본 30, 최대 50), `include_reason` (bool)
- 예: `"AI 반도체 테마 종목"`

#### `list_sectors`
업종 목록 (약 79개, 등락률 순).

#### `get_sector_stocks`
업종 내 종목.
- `sector_name` (str, 부분 매칭), `count`
- 예: `"통신장비 업종"`

#### `get_volume_ranking` / `get_change_ranking` / `get_market_cap_ranking`
거래량·등락률·시가총액 상위 종목.
- `market` (`KOSPI|KOSDAQ|ALL`), `count` (int, 기본 50, 최대 500), `direction` (`change_ranking`만, `up|down`)
- 예: `"오늘 거래량 TOP 50"`, `"코스닥 하락률 20위"`

---

### 벌크 조회 (2)

#### `get_multi_stocks`
여러 종목 기본 정보 병렬 조회 (현재가·거래량).
- `codes` (list, 최대 30)
- 개별 `get_price` N회보다 훨씬 빠름.

#### `get_multi_chart_stats`
여러 종목 차트 통계 (52주 고점/저점/낙폭·수익률·평균 거래량).
- `codes` (list, 최대 100), `days` (int, 기본 260)
- 반환: `current_price`, `high`, `high_date`, `low`, `low_date`, `drawdown_pct`, `recovery_pct`, `period_return_pct`, `avg_volume`

---

### ETF (2)

#### `get_etf_list`
ETF 1,000+ 목록 + 카테고리 필터·정렬.
- `category` (7개 중 선택), `sort_by` (`market_cap|volume|return_1m|return_3m|return_6m|return_1y|dividend_yield`), `limit`

#### `get_etf_info`
개별 ETF 상세 (기초지수·총보수·구성종목·수익률 1/3/6/12M).
- `code` (str)
- 예: `"TIGER 200 상세"`

---

### 분석·공시 (3)

#### `get_consensus`
증권사 컨센서스 (목표주가·투자의견 분포·실적 추정치).
- `code` (str)
- 예: `"삼성전자 애널리스트 목표가"`

#### `get_reports`
증권사 리포트 목록 + 본문 요약 + PDF 링크.
- `code` (str), `limit` (int)
- 예: `"LG에너지솔루션 최근 리포트"`

#### `get_disclosure`
DART 공시 목록 (제목·날짜·출처).
- `code` (str), `limit`

---

### Excel 출력 (3)

#### `export_to_excel`
단일 종목 데이터 Excel 저장 (Gemini/GPT에 파일로 넘길 때).
- `data_type` (`chart|flow|financial`), `code`, `days`, `filename`

#### `scan_to_excel`
여러 종목 스냅샷 Excel 생성. 한 번 스캔(10~20초) → 이후 `query_excel`로 반복 쿼리(ms 단위).
- `codes` (list, 최대 500), `days`, `include_financial` (bool), `filename`

#### `query_excel`
저장된 스냅샷에서 조건 필터링.
- `file_path`, `filters` (dict), `sort_by`, `descending`, `limit`
- 예: `"그 스냅샷에서 PER 10 이하, 낙폭 -30% 이상"`

---

### 디버깅 (1)

#### `get_metrics_summary`
도구 사용량·토큰 통계.
- `days` (int, 기본 1, 최대 30)
- 로그: `~/Downloads/kstock/logs/metrics_YYYYMMDD.jsonl`

---

## 🇺🇸 미국 주식 (20)

데이터 소스: **Yahoo Finance (yfinance)**. API 키 불필요, 최대 15분 지연.
티커 자동 감지 (1~5자 알파벳 + `.`/`-` 특수 = US). BRK.B 등 dot 티커는 `BRK-B`로 내부 변환.

### 탐색·시장 (4)

#### `get_us_search`
종목명 → 티커 검색.
- `query` (str): 회사명(한/영) 또는 티커
- 예: `"Apple 티커"`, `"NVIDIA 검색"`
⚠️ 티커 모를 때 반드시 먼저 호출.

#### `get_us_market`
주요 지수 스냅샷 (S&P 500, Dow, Nasdaq, Russell 2000, VIX).
- 파라미터 없음

#### `get_us_screener`
10종 프리셋 스크리너.
- `preset`: `day_gainers`, `day_losers`, `most_actives`, `most_shorted_stocks`, `aggressive_small_caps`, `growth_technology_stocks`, `undervalued_growth_stocks`, `undervalued_large_caps`, `small_cap_gainers`, `conservative_foreign_funds`
- `count` (int)

#### `get_us_sector`
섹터별 overview + top 기업.
- `sector_key`: `technology`, `healthcare`, `financial-services`, `consumer-cyclical`, `consumer-defensive`, `communication-services`, `industrials`, `energy`, `basic-materials`, `utilities`, `real-estate`
- `top_n` (int)

---

### 기본 데이터 (6)

#### `get_us_price`
현재가 + 전일대비 + 52주 고저 + 베타 + 시가총액 + 마켓 상태.
- `ticker` (str): 예 `"AAPL"`, `"TSLA"`, `"BRK.B"`

#### `get_us_info`
기업 정보 (섹터·산업·시총·사업 요약).

#### `get_us_chart`
OHLCV 시계열. **500행 상한 (자동 축약, 토큰 보호)**.
- `ticker`, `period` (`1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max`, 기본 `3mo`), `interval` (`1m/5m/15m/30m/1h/1d/1wk/1mo`, 기본 `1d`), `prepost` (bool, 프리/포스트 마켓)

#### `get_us_financials`
Valuation (P/E, Forward P/E, PEG, P/B) + Profitability (ROE, margin) + Dividend 비율.

#### `get_us_financial_statement`
재무제표 3종.
- `ticker`, `statement_type` (`income|balance|cash_flow`), `period` (`annual|quarterly`)
- 핵심 row만 추출 (Total Revenue, Net Income, Total Assets, Free Cash Flow 등)

#### `get_us_multi_price`
여러 티커 일괄 조회 (병렬, 30개 1~2초).
- `tickers` (list, 최대 30)

---

### US 고유 정보 (10)

#### `get_us_earnings`
다음 실적 발표일 + 최근 EPS 서프라이즈 8분기.

#### `get_us_analyst`
애널리스트 목표가(평균/중앙값/최고/최저) + buy/hold/sell 분포 + 업·다운그레이드 이력 + EPS/매출 추정치.

#### `get_us_dividends`
배당 이력 + ex-date + 수익률 + 배당성향 + 5년 평균.
- `ticker`, `limit` (int, 기본 12)

#### `get_us_options`
옵션 체인 (calls/puts, IV, OI).
- `ticker`, `expiration` (date, 미지정 시 최근접), `strikes_around_spot` (int, 기본 10)
- ⚠️ Greeks(Δ·Γ·Θ) 미포함. yfinance 제공 안 함.

#### `get_us_insider`
Form 4 내부자 거래 + 최근 6개월 순매수 요약 + 현재 내부자 명단.

#### `get_us_holders`
기관 보유(13F) + 뮤추얼 펀드 + breakdown(insiders %/institutions %).

#### `get_us_short`
공매도 지표 (% of float, days to cover).
- ⚠️ FINRA bi-monthly 공시라 2~4주 stale. 응답에 `date_short_interest` 표시.

#### `get_us_filings`
SEC 공시 목록 (10-K, 10-Q, 8-K) + EDGAR URL.
- `ticker`, `limit` (int, 기본 15)

#### `get_us_news`
최근 뉴스 헤드라인.
- `ticker`, `limit` (int, 기본 10)

#### `get_us_etf_info`
ETF 전용 상세 (top holdings, 섹터 비중, 자산 배분, 보수율, YTD 수익률).
- `ticker`: SPY, QQQ, SCHD, VTI 등

---

## 📁 저장 파일 위치

Excel 스냅샷·메트릭 로그:
- Windows: `%USERPROFILE%\Downloads\kstock\`
- macOS/Linux: `~/Downloads/kstock/`

사용자 PC에만 저장, 외부 전송 없음.

---

## ⚠️ 알려진 제약

- **네이버 증권 HTML 구조 변경 시** 일부 필드 파싱 실패 가능 — 릴리즈마다 재검증
- **Yahoo Finance 15분 지연** — 실시간 호가·다크풀·Level 2 미지원
- **옵션 Greeks 미제공** — yfinance 자체 미지원
- **BRK.B SEC 공시** — yfinance gap, `BRK-A`로 조회
- **공매도 2~4주 stale** — FINRA 공시 스케줄 제약

자세한 품질 검증 내역: [QUALITY.md](../../QUALITY.md)
