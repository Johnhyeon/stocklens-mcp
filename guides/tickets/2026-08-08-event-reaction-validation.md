# P0 — 사건반응 분석을 fail-closed로 전환

## 티켓 요약

- **컴포넌트:** StockLens MCP / `get_event_reaction`
- **유형:** 정확성 결함, 데이터 완전성, 회귀 테스트
- **우선순위:** P0
- **상태:** Ready for implementation
- **발견일:** 2026-08-08
- **관련 코드:** `stock_mcp_server/event_reaction.py`, `stock_mcp_server/server.py`, `stock_mcp_server/naver.py`
- **관련 테스트:** `tests/test_event_reaction.py`

`get_event_reaction`이 분석할 수 없는 사건창을 정상 수익률·거래량·수급 결과처럼 반환하지 않도록 한다. 데이터 범위 밖 사건, 거래가 없는 구간, 수급 데이터 결측을 명시적으로 구분하고 숫자 해석을 차단한다.

## 발견 근거

실제 자본구조 사건 12건을 StockLens로 조회하고 원시 거래일·거래량을 대조했다.

- 해석 가능: 4건
- 사건일을 보유 데이터의 첫 거래일로 잘못 정렬: 5건
- 전 사건창 거래량 0: 3건
- 최종 해석 제외: 8건

대표 재현:

- 사건일 `2022-11-28`이 보유 OHLCV 시작일 `2024-07-17`보다 오래됐지만, 현재 로직은 `2024-07-17`을 D0로 선택한다. 정렬 오차는 597일이다.
- 사건창 전체 거래량이 0이어도 동일 종가를 이용해 `0.00%` 수익률을 정상 출력한다.
- 과거 사건에 대응하는 투자자 수급 행이 0건이어도 기관·외국인 합계를 실제 순매매 0처럼 출력할 수 있다.

연구 증거: `../../../research/three_lens_value_discovery/sessions/017_capital_structure_realization_ledger/market_reaction.csv`

## 근본 원인

1. `build_event_reaction`은 `row["date"] >= event_date`인 첫 봉을 선택하지만 `event_date < earliest_available_date`를 거절하지 않는다.
2. `get_event_reaction`은 일봉을 최대 500개만 요청한다. 오래된 사건은 데이터 범위 밖일 수 있다.
3. 거래량 0인 봉을 가격 발견이 발생한 거래일로 취급한다.
4. 수급 조회는 최근 20~60일만 가져오며, 사건창과 겹치는 행이 없어도 합계 함수가 숫자 0을 반환한다.
5. 단위 테스트가 정확한 거래일과 주말 다음 거래일 정렬만 다루며 데이터 시작 경계, 거래정지, 수급 결측을 다루지 않는다.

## 사용자 및 사업 영향

- 관계없는 시기의 등락을 특정 공시의 시장반응으로 오인한다.
- 거래정지 또는 유동성 부재를 시장의 무반응으로 오인한다.
- 수급 데이터 부재를 기관·외국인의 중립 포지션으로 오인한다.
- 잘못된 사건 라벨이 보고서, 순위, 백테스트, 스코어링, AI 학습 데이터로 전파된다.
- 날짜와 퍼센트가 정상 형식으로 표시되어 사용자가 오류를 발견하기 어렵다.
- 고객 보고서나 실사 자료에 포함되면 제품 신뢰와 분석 책임에 직접 영향을 준다.

## 검토한 접근법

### A. 검증 가드와 명시적 결측 상태 추가 — 권장, 본 티켓

현재 공급자가 반환한 데이터 안에서 분석 가능성을 먼저 판정한다. 검증을 통과하지 못하면 수익률과 수급 숫자를 만들지 않는다.

- 장점: 작은 변경으로 허위 분석을 즉시 차단한다.
- 단점: 과거 사건의 조회 가능 범위를 늘리지는 않는다.

### B. 사건일 기준 과거 데이터 동적 조회

사건일을 포함하도록 OHLCV와 수급 데이터를 추가 페이지 조회하거나 다른 공급자로 보강한다.

- 장점: 분석 가능한 과거 사건 수가 늘어난다.
- 단점: 공급자별 보존 기간, 거래정지, 비용, 지연, 데이터 정합성 문제가 별도로 생긴다.

### C. 검증과 과거 데이터 확장을 동시에 구현

- 장점: 기능 완성도는 가장 높다.
- 단점: 긴급 정확성 수정과 데이터 확장이 결합되어 회귀 위험과 검증 범위가 커진다.

**결정:** A를 P0로 구현한다. B는 A 배포 후 별도 P1 티켓으로 측정·설계한다. 데이터 확장에 실패해도 A의 fail-closed 규칙은 유지한다.

## 요구사항

### R1. 가격 이력 범위 검증

- OHLCV를 날짜 오름차순으로 정규화한 뒤 `earliest_available_date`와 `latest_available_date`를 계산한다.
- `event_date < earliest_available_date`이면 사건 기준일과 수익률을 계산하지 않는다.
- 상태 코드는 `EVENT_BEFORE_PRICE_HISTORY`로 반환한다.
- 응답에는 사건일, 데이터 시작일, 두 날짜의 달력일 차이를 포함한다.
- 보유 데이터의 첫 봉을 사건 기준일로 자동 대체하지 않는다.

### R2. 거래 가능한 기준일 판정

- 사건일 이후 첫 **양의 거래량** 봉을 `basis_trading_date` 후보로 삼는다.
- 주말·휴일 이월은 정상으로 허용하되 `alignment_reason=next_trading_session`을 기록한다.
- 사건일 이후 조회된 창 전체에 양의 거래량 봉이 없으면 `NO_TRADING_ACTIVITY`로 해석 불가 처리한다.
- 사건일 당일 거래량이 0이고 이후 거래가 재개되면 첫 양의 거래량 날짜를 기준일로 사용하고 `alignment_reason=first_tradable_session_after_event`를 기록한다.
- D+1, D+5, D+10은 거래량이 양수인 관측치만 이용해 산정한다. 필요한 관측치가 부족하면 해당 수익률은 `null`과 `INSUFFICIENT_POST_EVENT_HISTORY`로 반환한다.
- 거래가 없던 봉을 이용해 `0.00%` 수익률을 만들지 않는다.

### R3. 수급 결측과 실제 0 분리

- 사건창과 겹치는 수급 행이 0건이면 `flow.status=unavailable`로 반환한다.
- 이때 `institutional`, `foreign` 합계는 `null`, `days`는 0으로 반환한다.
- 실제 수급 행이 존재하고 합계가 0인 경우에만 숫자 0을 반환한다.
- Markdown 출력에서도 결측을 `0` 또는 `중립`으로 표현하지 않고 `해당 사건창 수급 데이터 없음`으로 표시한다.

### R4. 검증 메타데이터

기계 소비자가 숫자를 사용하기 전에 확인할 수 있도록 다음 계약을 제공한다.

```json
{
  "validation": {
    "status": "usable | partial | unavailable",
    "codes": [],
    "event_date": "YYYY-MM-DD",
    "earliest_available_date": "YYYY-MM-DD | null",
    "latest_available_date": "YYYY-MM-DD | null",
    "basis_trading_date": "YYYY-MM-DD | null",
    "alignment_gap_calendar_days": 0,
    "alignment_reason": "exact | next_trading_session | first_tradable_session_after_event | null"
  }
}
```

- `validation.status=unavailable`이면 D0/D+ 수익률을 반환하지 않는다.
- 일부 후속 기간만 부족하면 `partial`로 반환하고 부족한 값만 `null`로 둔다.
- 경고 코드와 사람이 읽을 수 있는 설명을 함께 제공한다.

### R5. 서버 조회 범위와 오류 처리

- 현재 500봉 제한을 유지하더라도 데이터 시작일을 응답에 노출하고 범위 밖 사건을 거절해야 한다.
- 빈 OHLCV, 공급자 실패, 파싱 실패를 사건 무반응으로 변환하지 않는다.
- 공급자 오류와 데이터 범위 부족은 서로 다른 코드로 구분한다.
- 기존 정상 사건의 수익률 계산 방식은 유지한다.

## 오류 코드

- `EVENT_BEFORE_PRICE_HISTORY`
- `EVENT_AFTER_PRICE_HISTORY`
- `NO_PRICE_HISTORY`
- `NO_TRADING_ACTIVITY`
- `INSUFFICIENT_POST_EVENT_HISTORY`
- `FLOW_UNAVAILABLE_FOR_EVENT_WINDOW`
- `PROVIDER_ERROR`

## 테스트 요구사항

### 단위 테스트

1. 사건일이 최초 보유 봉보다 이르면 `EVENT_BEFORE_PRICE_HISTORY`이며 수익률이 없다.
2. 사건일이 정확한 양의 거래량 봉이면 기존과 동일하게 `exact`로 계산한다.
3. 주말 사건은 다음 양의 거래량 봉으로 이월되고 `next_trading_session`이 된다.
4. 사건일 봉의 거래량이 0이고 다음 봉에서 거래가 재개되면 첫 거래 가능일을 기준일로 사용한다.
5. 전 구간 거래량이 0이면 `NO_TRADING_ACTIVITY`이며 `0.00%`를 출력하지 않는다.
6. D+10에 필요한 거래 가능 관측치가 부족하면 해당 값만 `null`이고 상태가 `partial`이다.
7. 수급 행이 없으면 합계가 `null`이고 `FLOW_UNAVAILABLE_FOR_EVENT_WINDOW`가 포함된다.
8. 실제 수급 행 합계가 0이면 숫자 0을 유지한다.
9. 입력 봉의 날짜 순서가 뒤섞여 있어도 정렬 후 동일한 결과를 낸다.

### 통합·회귀 테스트

1. 서버의 500봉 응답에서 오래된 사건이 첫 보유 거래일로 정렬되지 않는다.
2. Markdown 출력에 데이터 결측이 `0% 반응`, `기관 0`, `외국인 0`으로 표시되지 않는다.
3. 현재 정상 fixture의 D0/D+ 수익률은 변경되지 않는다.
4. 발견 표본 12건 fixture를 사용하면 4건만 `usable`이고 8건은 숫자 해석이 차단된다.

## 수용 기준

- [ ] 데이터 시작일보다 오래된 사건에 어떤 D0/D+ 수익률도 생성하지 않는다.
- [ ] 양의 거래량이 없는 사건창에 `0.00%` 시장반응을 생성하지 않는다.
- [ ] 수급 데이터가 없는 경우 기관·외국인 값을 0으로 생성하지 않는다.
- [ ] 모든 응답에 `validation.status`와 구조화된 오류·경고 코드가 포함된다.
- [ ] 정상 사건, 주말 이월, 거래 재개 사건이 서로 구분된다.
- [ ] 신규 단위·통합·회귀 테스트가 통과한다.
- [ ] 12건 발견 fixture에서 `usable=4`, 해석 차단 `=8`이 재현된다.
- [ ] 기존 정상 사건의 계산 결과가 회귀하지 않는다.

## 구현 대상 파일

- Modify: `stock_mcp_server/event_reaction.py`
- Modify: `stock_mcp_server/server.py`
- 필요 시 Modify: `stock_mcp_server/naver.py`
- Modify: `tests/test_event_reaction.py`
- Create: 실제 12건을 비식별·최소화한 회귀 fixture 또는 동등한 합성 fixture

## 범위 제외 및 후속 티켓

- 과거 OHLCV·투자자 수급의 보존 기간 확대
- 제2 데이터 공급자 도입
- 공시 시각을 이용한 장중·장후 사건 구분
- 시장·업종 조정 초과수익률 및 인과 추정
- 거래정지 사유 자동 분류

후속 P1에서는 `event_date`를 입력으로 과거 데이터 가용성을 사전 조회하고, 추가 조회 비용·커버리지·정확도를 측정한다.

## 완료 정의

코드 변경만으로 완료 처리하지 않는다. 신규 경계 테스트와 12건 회귀 fixture에서 잘못된 숫자 생성이 차단되고, 정상 4건의 결과가 유지되는 증거를 첨부해야 한다.
