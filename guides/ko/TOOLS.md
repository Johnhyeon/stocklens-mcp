# StockLens 도구 레퍼런스

총 **56개 도구** — 한국 주식 35(시장 캘린더 포함) + 미국 주식 21.

[🇺🇸 English](../en/TOOLS.md) | [USAGE](USAGE.md) | [INSTALL](INSTALL.md)

---

## 🇰🇷 한국 주식 (35)

데이터 소스: 네이버 증권 (공개 데이터, API 키 불필요).

### 기본 조회 (10)

#### `get_market_clock`
한국장/미국장 현재 상태, 주말/휴장 여부, 최근 거래일, 다음 개장일을 한 번에 조회.
- 파라미터 없음
- 종목 분석 전에 데이터 기준시각과 장 상태를 확인할 때 사용
- KRX와 NYSE/NASDAQ의 장전/정규장/시간외/장마감 상태를 함께 반환

⚠️ 종목 현재가, 차트, 수급을 해석하기 전에 기준 거래일을 확인할 때 먼저 호출.

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

#### `watchlist`
'내 종목'(관심종목) 보기·추가·삭제. **DartLens·TelegramLens와 같은 목록**을 씁니다.
- `action` (`list|add|remove`), `query` (str, 종목명 또는 코드)
- 예: `"디오 관심종목에 넣어줘"`, `"내 종목 뭐뭐 있지?"`

#### `stocklens_status`
자가진단 — 버전·라이선스·국내/미국 장 상태·최근 성공·실패·캐시 상태를 한 번에.
- 파라미터 없음
- 다른 도구가 막힐 때 원인 확인용

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

### 스크리닝 (8)

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

#### `screen_by_flow`
거래대금·거래량 상위 중 **외국인·기관이 며칠 연속 순매수**한 종목만 추립니다.
- `top_n`, `market`, `foreign_days`, `inst_days`, `exclude_etf` (bool), `sort_by`
- 예: `"외국인 5일 연속 순매수 종목"`

---

### 벌크 조회 (4)

#### `get_multi_stocks`
여러 종목 기본 정보 병렬 조회 (현재가·거래량).
- `codes` (list, 최대 30)
- 개별 `get_price` N회보다 훨씬 빠름.

#### `get_multi_chart_stats`
여러 종목 차트 통계 (52주 고점/저점/낙폭·수익률·평균 거래량).
- `codes` (list, 최대 100), `days` (int, 기본 260)
- 반환: `current_price`, `high`, `high_date`, `low`, `low_date`, `drawdown_pct`, `recovery_pct`, `period_return_pct`, `avg_volume`

#### `get_flow_batch`
여러 종목 수급(기관·외국인 순매매) 병렬 조회.
- `codes` (list), `days` (int)
- 종목마다 `get_flow`를 반복하는 것보다 훨씬 가볍습니다.

#### `get_financial_batch`
여러 종목 핵심 재무(PER·PBR·ROE·영업이익률·부채비율·배당률)를 한 표로.
- `codes` (list)
- ⚠️ 비교 목적이면 `get_financial`을 반복하지 말고 이걸 쓰세요 (토큰 대폭 절감).

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

### 분석·공시 (5)

#### `get_consensus`
증권사 컨센서스 (목표주가·투자의견 분포·실적 추정치).
- `code` (str)
- 예: `"삼성전자 애널리스트 목표가"`

#### `get_reports`
증권사 리포트 목록 + 본문 요약 + PDF 링크.
- `code` (str), `limit` (int)
- 예: `"LG에너지솔루션 최근 리포트"`

#### `get_report_content`
리포트 **한 건**의 PDF 본문. 목표주가 산출 근거·실적 추정까지 읽습니다.
- `nid` (str) — `get_reports` 결과에 표시되는 리포트 번호
- `mode` (str) — `summary` 앞부분(기본) / `full` 전문 / `link` 링크만
- 예: `"이 리포트 자세히 봐줘"`, `"목표가 근거가 뭐야"`, `"전문 다 보여줘"`
- 이미지로 만들어진 PDF는 읽을 수 없어 원문 링크로 안내합니다.

#### `get_disclosure`
DART 공시 목록 (제목·날짜·출처).
- `code` (str), `limit`

#### `get_event_reaction`
특정 날짜(공시일 등) **전후 주가·거래량·수급 반응**을 정렬해 보여줍니다.
- `code`, `event_date` (YYYY-MM-DD), `before` (int), `after` (int)
- 예: `"이 공시 난 날 주가가 어떻게 반응했어?"`
- 거래정지 등으로 잴 수 없는 구간은 0%가 아니라 "분석 불가"로 표시합니다.

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

## 🇺🇸 미국 주식 (21)

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

### US 고유 정보 (11)

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

#### `export_us_to_excel`
미국 주식 장기 데이터를 Excel 파일로 저장 (대화 토큰 소비 없음).
- `ticker`, `period`, `interval`, `filename`

---

## 🕐 결과 메타 (`RESULT_META_JSON` / `_meta`) - 규약 v3

대부분의 도구는 응답 끝에 `RESULT_META_JSON_START…END` 블록을(JSON 도구는 payload 안
`_meta` 키로) 붙입니다. `meta_v`가 규약 버전이고 현재 **3**입니다.

**v3에서 늘어난 필드는 전부 선택적입니다.** 해당 개념이 없는 도구에는 키 자체가
생기지 않고, v2만 아는 소비자는 그대로 무시해도 됩니다. 기존 키
(`as_of`·`data_as_of`·`data_basis`·`data_completeness`·`warnings`·`entity`)의 의미는
바뀌지 않았습니다.

### `data_completeness` 는 **요청한 범위** 기준입니다

돌려준 것이 다 왔는지가 아니라, **당신이 물어본 범위를 다 채웠는지**를 말합니다.
60일을 요청해 20일이 왔으면 그 20일이 온전해도 `partial`입니다.

| 값 | 뜻 |
|---|---|
| `complete` | 요청한 범위를 다 채웠다 |
| `partial` | 일부만 왔다. 없는 부분을 추정으로 메우지 말 것 |
| `none` | 해당 없음. 조회 실패가 아니라 데이터가 없는 것 |

`coverage.coverage_complete=false` 인데 `data_completeness=complete` 인 조합은
구조적으로 나올 수 없습니다(값 검증에서 막습니다).

### `coverage` - 요청한 범위와 실제로 돌려준 범위

```json
"coverage": {
  "requested": {"unit": "day", "value": 60},
  "effective": {"unit": "day", "value": 20},
  "returned_count": 20,
  "total_count": null,
  "truncated": true,
  "coverage_complete": false,
  "reason": "server_cap"
}
```

`reason` 은 열거값입니다: `server_cap`(도구 상한) / `pagination`(원천이 페이지로 끊음)
/ `source_limit`(원천이 전체 건수를 안 알려줌) / `incomplete_tail`(마지막 봉이 진행 중)
/ `mixed_periods`(기준 기간 혼재) / `unknown`.

**예: `get_flow_batch(codes=[...], days=60)`**
이 도구의 상한은 20일입니다. 60일을 요청하면 20일치가 오고, 본문 첫 줄에 그 사실이
적히며 메타는 위와 같이 `requested=60 / effective=20 / partial` 이 됩니다.
`per_entity_returned_count` 로 종목별 실제 행 수가, `short_entities` 로 요청 기간보다
짧게 온 종목 목록이 함께 옵니다. **한 종목이라도 짧으면 그 배치는 `partial`**
(`reason=source_limit`)입니다 - 20일을 요청해 3일만 온 종목이 섞였는데 전체를
"다 받았다"고 말할 수는 없습니다.

**예: `get_disclosure(code=...)`**
네이버는 전체 건수를 알려주지 않으므로 항상 `coverage_complete=false`,
`total_count=null`, `reason=source_limit` 입니다. 여기서 "최근 공시에 아무것도 없다"를
결론지을 수 없습니다. 빠짐없이 보려면 DartLens `list_disclosures` 를 쓰세요.

### `bar_state` - 마지막 봉이 끝났는가

```json
"bar_state": {
  "timeframe": "week",
  "last_bar_date": "2026-08-26",
  "last_bar_complete": false,
  "last_completed_bar_date": "2026-08-21",
  "calculation_includes_incomplete": true
}
```

`data_basis=in_progress_bar` 는 **장중일 때만** 붙습니다. 수요일 저녁이면 장은 닫혔지만
그 주 주봉은 금요일까지 남아 있습니다. 그 주봉으로 계산한 이평·RSI 는 금요일에 값이
바뀝니다. `last_bar_complete=false` 면 `coverage.reason=incomplete_tail` 과 `partial` 이
함께 붙습니다. **마감 여부를 판별하지 못했을 때(`null`)도 `partial`**
(`reason=unknown`)입니다 - 모르는 것을 확정치로 내보내지 않습니다. 휴장일을 반영하므로, 추석으로 목·금이 쉬는 주는 수요일 마감으로 그
주봉이 끝납니다. 거래소 달력을 확인하지 못하면 추측하지 않고 `null`(모름)입니다.

대상: `get_chart` · `get_indicators` · `get_indicators_bulk` · `get_multi_chart_stats`

### `price_adjustment` - 이 가격이 무엇으로 조정된 값인가

```json
"price_adjustment": {
  "status": "unknown",
  "corporate_actions_checked": false,
  "cross_event_comparison_safe": false
}
```

네이버는 조정 기준을 명시하지 않으므로 현재 값은 `unknown` 입니다. 액면분할·유상증자
전후 구간을 이어 붙여 비교하면 안 된다는 뜻입니다. `status` 는
`raw`/`split_adjusted`/`total_return_adjusted`/`unknown` 중 하나입니다.

### `period_coverage` - 종목마다 기준 기간이 같은가

`get_financial_batch` 전용입니다. `consistency` 가 `mixed` 면 종목마다 확정 분기가
달라 PER·ROE 를 그대로 나란히 놓을 수 없습니다. **`mixed` 와 `unknown`(기준을 하나도
못 읽음) 둘 다 `partial`** 이고, `coverage.reason` 은 각각 `mixed_periods` /
`unknown` 입니다.

```json
"period_coverage": {
  "consistency": "mixed",
  "periods": {"005930": "2026.06", "096770": "2026.03"},
  "oldest": "2026.03",
  "newest": "2026.06"
}
```

### `indicator_coverage` - 봉이 모자라 계산되지 않은 지표

`get_indicators` · `get_indicators_bulk`. 주봉 104개로 `ma120` 을 부르면 값이 빠지는데,
빠진 자리는 "그런 신호가 없다"가 아니라 "아직 계산할 이력이 없다"는 뜻입니다.

```json
"indicator_coverage": {
  "available_bars": 104,
  "required_bars": {"ma5": 5, "ma20": 20, "ma60": 60, "ma120": 120, "ma240": 240},
  "insufficient": ["ma120", "ma240"]
}
```

배치에서는 **가장 짧은 종목**을 기준으로 잡습니다(과대 주장 방지).

`bar_state` 도 마찬가지로 **종목별로 계산한 뒤 합칩니다.** 한 종목이라도 미완성 봉을
물고 있으면 그 배치의 계산에는 미완성 봉이 섞인 것이고, `last_completed_bar_date` 는
종목들이 실제로 가진 확정 봉 중 가장 최근 것입니다.

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
