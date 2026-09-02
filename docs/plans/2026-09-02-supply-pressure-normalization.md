# Supply Pressure Normalization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 수급 압력 응답에서 장중 시각, 가격 부호, 금액 단위를 공급자와 무관하게 오해 없이 읽을 수 있게 한다.

**Architecture:** 기존 `PressureRow`와 `PressureBlock` 계약을 하위 호환 방식으로 확장한다. 행에는 선택적 관측 시각을, 블록에는 측정값별 단위를 싣는다. 공급자 어댑터에서 확인된 필드만 공통 단위로 변환하고, 단위가 확인되지 않은 값은 추정하지 않는다.

**Tech Stack:** Python dataclasses, FastMCP JSON serialization, pytest

---

### Task 1: KIS 장중 시각 보존

**Files:**
- Modify: `stock_mcp_server/market_data/evidence_models.py`
- Modify: `stock_mcp_server/market_data/kis_evidence.py`
- Modify: `stock_mcp_server/server.py`
- Test: `tests/test_kis_supply_pressure.py`
- Test: `tests/test_evidence_tools.py`

1. KIS 프로그램매매 픽스처의 `bsop_hour`가 KST 관측 시각으로 공개되는 실패 테스트를 작성한다.
2. `PressureRow`에 선택적 `observed_at`을 추가하고 KIS 장중 행에서만 채운다.
3. JSON 응답에 `observed_at`을 additive 필드로 직렬화한다.
4. 일별 행은 `observed_at=null`을 유지하는지 검증한다.

### Task 2: 키움 가격 부호 정규화

**Files:**
- Modify: `stock_mcp_server/market_data/kiwoom_evidence.py`
- Test: `tests/test_kiwoom_supply_pressure.py`

1. 하락일의 `cur_prc=-256500`과 `close_pric`이 양의 가격으로 반환되는 실패 테스트를 작성한다.
2. 정규 이름이 `close`인 필드에만 가격 절댓값 정규화를 적용한다.
3. 순매수처럼 실제 부호 의미가 있는 측정값은 기존 부호와 산술 검산을 유지한다.

### Task 3: 단위와 확인된 금액 환산

**Files:**
- Modify: `stock_mcp_server/market_data/evidence_models.py`
- Modify: `stock_mcp_server/market_data/kis_evidence.py`
- Modify: `stock_mcp_server/market_data/kiwoom_evidence.py`
- Modify: `stock_mcp_server/server.py`
- Test: `tests/test_kis_supply_pressure.py`
- Test: `tests/test_kiwoom_supply_pressure.py`
- Test: `tests/test_evidence_tools.py`

1. 블록별 `measure_units`가 공개되는 실패 테스트를 작성한다.
2. KIS 프로그램 금액과 공매도 금액은 이미 KRW인 값을 유지한다.
3. 키움 프로그램 금액은 백만원에서 KRW로, 공매도 금액은 천원에서 KRW로 변환한다.
4. 키움 신용·대차 금액은 근거가 확보되기 전까지 원값과 `unknown` 단위를 유지하고 경고한다.
5. 수량은 `shares`, 비율은 `percent`, 가격과 확인된 금액은 `KRW`로 표시한다.

### Task 4: 회귀와 실데이터 검증

**Files:**
- Test: 수급 압력 관련 표적 테스트
- Test: 증거 도구 및 캐시 관련 회귀 테스트

1. 표적 테스트에서 시간, 부호, 단위, 산술 불변을 검증한다.
2. 15종목 KIS·키움 실데이터를 다시 조회해 프로그램 금액과 공매도 금액의 공통 단위 일치를 확인한다.
3. 장중 잠정값에서 마감 확정값으로의 전이 검증은 별도 장중 스냅샷이 있을 때만 완료로 판정한다.
