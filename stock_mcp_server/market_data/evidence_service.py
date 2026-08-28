"""상세 수급 서비스 계층 (1.1 Task 11).

도구와 어댑터 사이. 하는 일은 셋이다.

1. 공급자 하나를 고정한다 (evidence_router). 자동으로 갈아타지 않는다.
2. **무엇을 못 받았는지 명시한다.** 공급자마다 줄 수 있는 것이 정확히
   반대로 갈려서(키움=기관 세부 13종·매수매도 없음, KIS=3종·매수매도
   있음) 한쪽을 상위집합처럼 다루면 응답이 거짓말이 된다.
3. 요청 도중 연결이 바뀌면 결과를 버린다.

빈 성공을 만들지 않는 것이 이 계층의 존재 이유다. 못 받은 항목은
`data_availability.unavailable` 에 사유와 함께 남고, 종류별 요청은
종류마다 독립된 블록으로 남는다. 서로 다른 종류를 하나의 점수로
합치지 않는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date

from stock_mcp_server.market_data.evidence_models import (
    DEFAULT_MEASURE,
    UNIT_BY_MEASURE,
    InvestorFlowDataset,
    PressureBlock,
)
from stock_mcp_server.market_data.evidence_router import (
    PRESSURE_KINDS,
    EvidenceRouterError,
    assert_same_generation,
    capability_state,
    resolve_evidence_source,
    select_provider,
)

MARKET = "KR"
MAX_BATCH_CODES = 30
DEFAULT_ROW_LIMIT = 30

# 투자자 수급 요청 하나가 묻는 세부 능력. 호출자가 고르는 것이 아니라
# 늘 셋 다 묻고, 공급자가 주는 만큼 받고 나머지는 사유와 함께 남긴다.
FLOW_CAPABILITIES = (
    "kr.investor_flow.daily.total",
    "kr.investor_flow.daily.breakdown",
    "kr.investor_flow.daily.buy_sell",
)


@dataclass(frozen=True)
class EvidenceResult:
    """투자자 수급 응답 봉투. 실패해도 같은 모양을 유지한다."""

    ok: bool
    provider: str | None
    profile: str | None
    market: str
    data_availability: dict
    records: tuple[InvestorFlowDataset, ...]
    coverage: dict
    warnings: tuple[str, ...]
    error_code: str | None
    measure: str = DEFAULT_MEASURE
    unit: str = "shares"
    alternative_provider: str | None = None


@dataclass(frozen=True)
class BatchEvidenceResult:
    ok: bool
    provider: str | None
    profile: str | None
    market: str
    data_availability: dict
    records: tuple[InvestorFlowDataset, ...]
    coverage: dict
    warnings: tuple[str, ...]
    error_code: str | None
    entity_failures: list = field(default_factory=list)
    measure: str = DEFAULT_MEASURE
    unit: str = "shares"
    alternative_provider: str | None = None


@dataclass(frozen=True)
class BatchPressureResult:
    """여러 종목의 종류별 블록. 공급자는 요청 전체에 하나뿐이다."""

    ok: bool
    provider: str | None
    profile: str | None
    market: str
    entities: dict
    entity_failures: list
    coverage: dict
    warnings: tuple[str, ...]
    error_code: str | None
    # 라우팅이 실패해 종목별 결과가 없을 때 쓸 상태 블록.
    fallback_blocks: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BatchPressureResult:
    """여러 종목의 종류별 블록. 공급자는 요청 전체에 하나뿐이다."""

    ok: bool
    provider: str | None
    profile: str | None
    market: str
    entities: dict
    entity_failures: list
    coverage: dict
    warnings: tuple[str, ...]
    error_code: str | None
    # 라우팅이 실패해 종목별 결과가 없을 때 쓸 상태 블록.
    fallback_blocks: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PressureResult:
    """종류별 블록. 여기에 합산 필드를 추가하지 않는다."""

    ok: bool
    provider: str | None
    profile: str | None
    market: str
    blocks: dict
    coverage: dict
    warnings: tuple[str, ...]
    error_code: str | None


def _unique(codes) -> list[str]:
    """입력 순서를 지키면서 중복만 접는다.

    같은 종목을 두 번 적었다고 API 를 두 번 부르지 않는다. 중복이
    상한을 잡아먹지도 않는다.
    """
    seen: list[str] = []
    for code in codes:
        text = str(code).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _guard_batch_size(unique: list[str]) -> None:
    """공급자를 부르기 전에 막는다.

    절반만 조회하고 실패하면 사용자는 어디까지가 진짜인지 알 수 없다.
    """
    if len(unique) > MAX_BATCH_CODES:
        raise ValueError(
            f"한 번에 조회할 수 있는 종목은 최대 {MAX_BATCH_CODES}개입니다 "
            f"(요청 {len(unique)}개).")


def _unique(codes) -> list[str]:
    """입력 순서를 지키면서 중복만 접는다.

    같은 종목을 두 번 적었다고 API 를 두 번 부르지 않는다. 중복이
    상한을 잡아먹지도 않는다.
    """
    seen: list[str] = []
    for code in codes:
        text = str(code).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _guard_batch_size(unique: list[str]) -> None:
    """공급자를 부르기 전에 막는다.

    절반만 조회하고 실패하면 사용자는 어디까지가 진짜인지 알 수 없다.
    """
    if len(unique) > MAX_BATCH_CODES:
        raise ValueError(
            f"한 번에 조회할 수 있는 종목은 최대 {MAX_BATCH_CODES}개입니다 "
            f"(요청 {len(unique)}개).")


def _error_result(exc: EvidenceRouterError, requested: tuple[str, ...],
                  measure: str) -> EvidenceResult:
    return EvidenceResult(
        ok=False, provider=exc.provider, profile=None, market=MARKET,
        data_availability={
            "requested": list(requested), "returned": [],
            "unavailable": [{"capability": c, "reason": exc.error_code}
                            for c in requested]},
        records=(), coverage={"rows": 0, "complete": False},
        warnings=(str(exc),), error_code=exc.error_code, measure=measure,
        unit=UNIT_BY_MEASURE.get(measure, "shares"),
        alternative_provider=exc.alternative)


def _flow_availability(adapter) -> tuple[list, list]:
    """어댑터의 실측 표에서 받은 것과 못 받은 것을 가른다."""
    caps = adapter.capabilities()
    returned, unavailable = [], []
    for name in FLOW_CAPABILITIES:
        if caps.get(name) == "available":
            returned.append(name)
        else:
            unavailable.append({
                "capability": name,
                "reason": "unsupported_by_selected_provider",
            })
    return returned, unavailable


class EvidenceService:
    def __init__(self, runtime=None, *, base_date: date | None = None,
                 public_providers: "tuple[str, ...] | None" = None,
                 release_override: bool = False) -> None:
        self._runtime = runtime
        self._base_date = base_date
        self._public = public_providers
        # 테스트에서만 쓴다. 운영 경로에서 출시 게이트를 우회하지 않는다.
        self._release_override = release_override

    # -- 기준일 -----------------------------------------------------------
    def _resolve_base_date(self, given: date | None) -> date:
        if given is not None:
            return given
        if self._base_date is not None:
            return self._base_date
        # server.py 는 이 함수를 build_market_clock 이라는 별칭으로 쓴다.
        # 별칭을 여기서 import 하면 없는 이름이라 ImportError 가 난다.
        from stock_mcp_server.market_clock import get_market_clock
        raw = get_market_clock()["krx"].get("last_trading_day")
        return date.fromisoformat(str(raw))

    # -- 공통 -------------------------------------------------------------
    def _context(self):
        runtime = self._runtime
        if runtime is None:
            from stock_mcp_server.market_data.runtime import ProviderRuntime
            runtime = self._runtime = ProviderRuntime()
        snapshot = runtime.snapshot()
        from stock_mcp_server.market_data.provider_registry import registry
        ids = self._public if self._public is not None else registry.ids()
        caps = {pid: snapshot.capabilities(pid) for pid in ids}
        return runtime, snapshot, caps

    def _adapter(self, runtime, snapshot, provider):
        adapter = runtime.evidence_provider_for(provider, snapshot=snapshot)
        if adapter is None:
            raise EvidenceRouterError(
                "not_configured",
                f"{provider} 연결 기록은 있지만 저장된 키를 읽지 못했습니다. "
                f"LeetKit Manager 에서 {provider} 를 다시 연결해주세요.",
                error_code="provider_not_configured", provider=provider)
        return adapter

    # -- 투자자 수급 ------------------------------------------------------
    async def investor_flow(self, *, code: str, days: int = 20,
                            measure: str = DEFAULT_MEASURE,
                            source: str = "auto",
                            base_date: date | None = None
                            ) -> EvidenceResult:
        try:
            runtime, snapshot, caps = self._context()
            resolution = resolve_evidence_source(
                capability="kr_investor_flow", requested_source=source,
                capabilities=caps,
                primary_provider=snapshot.primary_provider,
                public_providers=self._public,
                release_override=self._release_override)
            provider = resolution.selected_provider
            adapter = self._adapter(runtime, snapshot, provider)
            before = snapshot.provider_generation(provider)
            dataset = await adapter.fetch_investor_flow(
                code, base_date=self._resolve_base_date(base_date),
                measure=measure, row_limit=max(1, min(days, 120)))
            assert_same_generation(
                provider, before,
                runtime.snapshot().provider_generation(provider))
        except EvidenceRouterError as exc:
            return _error_result(exc, FLOW_CAPABILITIES, measure)

        returned, unavailable = _flow_availability(adapter)
        return EvidenceResult(
            ok=True, provider=provider, profile=adapter.profile,
            market=MARKET,
            data_availability={"requested": list(FLOW_CAPABILITIES),
                               "returned": returned,
                               "unavailable": unavailable},
            records=(dataset,), coverage=dict(dataset.coverage),
            warnings=tuple(dataset.warnings), error_code=None,
            measure=dataset.measure, unit=dataset.unit)

    async def investor_flow_batch(self, *, codes, days: int = 20,
                                  measure: str = DEFAULT_MEASURE,
                                  source: str = "auto",
                                  base_date: date | None = None
                                  ) -> BatchEvidenceResult:
        unique = _unique(codes)
        _guard_batch_size(unique)

        try:
            runtime, snapshot, caps = self._context()
            resolution = resolve_evidence_source(
                capability="kr_investor_flow", requested_source=source,
                capabilities=caps,
                primary_provider=snapshot.primary_provider,
                public_providers=self._public,
                release_override=self._release_override)
            provider = resolution.selected_provider
            adapter = self._adapter(runtime, snapshot, provider)
            before = snapshot.provider_generation(provider)
        except EvidenceRouterError as exc:
            base = _error_result(exc, FLOW_CAPABILITIES, measure)
            return BatchEvidenceResult(
                ok=False, provider=base.provider, profile=None,
                market=MARKET, data_availability=base.data_availability,
                records=(), coverage=base.coverage,
                warnings=base.warnings, error_code=base.error_code,
                # 요청한 종목이 목록에서 조용히 사라지면 호출자는 그것을
                # '조회했는데 없음'으로 읽는다. 시도조차 못 했다는 사실을
                # 종목마다 남긴다.
                entity_failures=[{"code": c, "reason": base.error_code}
                                 for c in unique],
                measure=measure, unit=base.unit,
                alternative_provider=base.alternative_provider)

        day = self._resolve_base_date(base_date)
        row_limit = max(1, min(days, 120))

        async def _one(code: str):
            try:
                return code, await adapter.fetch_investor_flow(
                    code, base_date=day, measure=measure,
                    row_limit=row_limit)
            except Exception as exc:  # noqa: BLE001
                # 한 종목이 실패해도 다른 공급자로 넘어가지 않는다. 실패한
                # 종목만 사유와 함께 남긴다.
                return code, getattr(exc, "provider_status", None) or \
                    "provider_unavailable"

        settled = await asyncio.gather(*[_one(c) for c in unique])

        try:
            assert_same_generation(
                provider, before,
                runtime.snapshot().provider_generation(provider))
        except EvidenceRouterError as exc:
            base = _error_result(exc, FLOW_CAPABILITIES, measure)
            return BatchEvidenceResult(
                ok=False, provider=provider, profile=adapter.profile,
                market=MARKET, data_availability=base.data_availability,
                records=(), coverage=base.coverage,
                warnings=base.warnings, error_code=base.error_code,
                entity_failures=[], measure=measure, unit=base.unit)

        records, failures, warnings = [], [], []
        for code, outcome in settled:
            if isinstance(outcome, InvestorFlowDataset):
                records.append(outcome)
                warnings.extend(outcome.warnings)
            else:
                failures.append({"code": code, "reason": outcome})

        returned, unavailable = _flow_availability(adapter)
        unit = records[0].unit if records else \
            UNIT_BY_MEASURE.get(measure, "shares")
        return BatchEvidenceResult(
            ok=bool(records), provider=provider, profile=adapter.profile,
            market=MARKET,
            data_availability={"requested": list(FLOW_CAPABILITIES),
                               "returned": returned,
                               "unavailable": unavailable},
            records=tuple(records),
            coverage={"requested_entities": len(unique),
                      "returned_entities": len(records),
                      "rows": sum(len(r.rows) for r in records),
                      "complete": not failures},
            warnings=tuple(dict.fromkeys(warnings)),
            error_code=None if records else "all_entities_failed",
            entity_failures=failures, measure=measure, unit=unit)

    # -- 수급 압력 --------------------------------------------------------
    @staticmethod
    def _clean_kinds(kinds) -> list[str]:
        requested: list[str] = []
        for kind in kinds:
            if kind not in PRESSURE_KINDS:
                raise ValueError(
                    f"지원하지 않는 수급 종류: {kind} (지원: {PRESSURE_KINDS})")
            if kind not in requested:
                requested.append(kind)
        return requested

    async def supply_pressure(self, *, code: str, kinds,
                              days: int = 30, source: str = "auto",
                              base_date: date | None = None
                              ) -> PressureResult:
        """단건. 배치 경로를 그대로 쓴다.

        단건과 배치가 서로 다른 코드를 타면 한쪽에만 공급자 고정이
        빠진다. 실제로 그렇게 빠졌었다 - 단건은 고정돼 있었는데 도구가
        종목마다 이 함수를 새로 불러서 종목별로 상태를 다시 읽었다.
        """
        batch = await self.supply_pressure_batch(
            codes=[code], kinds=kinds, days=days, source=source,
            base_date=base_date)
        blocks = batch.entities.get(code) or batch.fallback_blocks
        return PressureResult(
            ok=any(b.status == "ok" for b in blocks.values()),
            provider=batch.provider, profile=batch.profile, market=MARKET,
            blocks=blocks, coverage=batch.coverage,
            warnings=batch.warnings, error_code=batch.error_code)

    async def supply_pressure_batch(self, *, codes, kinds,
                                    days: int = 30, source: str = "auto",
                                    base_date: date | None = None
                                    ) -> BatchPressureResult:
        """여러 종목. **공급자와 generation 을 한 번만 고정한다.**

        종목마다 상태를 다시 읽으면, 조회 도중 Manager 에서 주 사용
        증권사가 바뀌었을 때 앞 종목과 뒤 종목이 서로 다른 증권사에서
        온다. 응답 최상위에는 공급자가 하나만 적히므로 사용자는 전부
        그 증권사 숫자라고 읽는다. 1.0 "한 요청은 한 공급자" 계약이
        깨지는 자리다.
        """
        requested = self._clean_kinds(kinds)
        unique = _unique(codes)
        _guard_batch_size(unique)

        try:
            runtime, snapshot, caps = self._context()
            provider = select_provider(
                requested_source=source, capabilities=caps,
                primary_provider=snapshot.primary_provider,
                public_providers=self._public)
            adapter = self._adapter(runtime, snapshot, provider)
            before = snapshot.provider_generation(provider)
        except EvidenceRouterError as exc:
            # 여섯 종류를 물었는데 블록이 0 개면 호출자는 두 가지 모양을
            # 따로 다뤄야 한다. 실패해도 요청한 종류는 자기 자리를 갖는다.
            blocks = {k: _state_block(k, "not_configured",
                                      exc.provider or "none", reason=None,
                                      granularity="unknown")
                      for k in requested}
            return BatchPressureResult(
                ok=False, provider=exc.provider, profile=None,
                market=MARKET,
                entities={c: dict(blocks) for c in unique},
                entity_failures=[], fallback_blocks=blocks,
                coverage={"requested_kinds": len(requested),
                          "fetched_kinds": 0,
                          "requested_entities": len(unique),
                          "complete": False},
                warnings=(str(exc),), error_code=exc.error_code)

        # 종류마다 능력이 다르다. 요청 전체를 하나로 판정하지 않고
        # 종류별로 가른 뒤, 받을 수 있는 것만 실제로 부른다. 능력은
        # 공급자 하나에 대한 사실이므로 종목마다 다시 따지지 않는다.
        state_blocks: dict[str, PressureBlock] = {}
        fetchable: list[str] = []
        reasons = adapter.pressure_unavailable_reasons()
        shapes = adapter.pressure_granularity()
        for kind in requested:
            state = capability_state(provider, f"kr_{kind}",
                                     caps.get(provider))
            if self._release_override and state == "verifying":
                state = "available"
            if state == "available":
                fetchable.append(kind)
            else:
                state_blocks[kind] = _state_block(
                    kind, state, provider, reason=reasons.get(kind),
                    granularity=shapes.get(kind, "unknown"))

        day = self._resolve_base_date(base_date)

        async def _one(code: str):
            if not fetchable:
                return code, {}
            try:
                return code, await adapter.fetch_supply_pressure(
                    code, kinds=tuple(fetchable), base_date=day,
                    lookback_days=max(1, days),
                    row_limit=DEFAULT_ROW_LIMIT)
            except Exception as exc:  # noqa: BLE001
                # 한 종목이 실패해도 다른 공급자로 넘어가지 않는다.
                reason = getattr(exc, "provider_status", None)
                return code, reason or "provider_unavailable"

        settled = await asyncio.gather(*[_one(c) for c in unique])

        try:
            assert_same_generation(
                provider, before,
                runtime.snapshot().provider_generation(provider))
        except EvidenceRouterError as exc:
            return BatchPressureResult(
                ok=False, provider=provider, profile=adapter.profile,
                market=MARKET, entities={}, entity_failures=[
                    {"code": c, "reason": exc.error_code} for c in unique],
                fallback_blocks={},
                coverage={"complete": False}, warnings=(str(exc),),
                error_code=exc.error_code)

        entities: dict[str, dict] = {}
        failures: list = []
        for code, outcome in settled:
            if isinstance(outcome, dict):
                merged = {**state_blocks, **outcome}
                entities[code] = {k: merged[k] for k in requested
                                  if k in merged}
            else:
                failures.append({"code": code, "reason": outcome})

        served = sum(1 for blocks in entities.values()
                     for b in blocks.values() if b.status == "ok")
        return BatchPressureResult(
            ok=served > 0,
            provider=provider, profile=adapter.profile, market=MARKET,
            entities=entities, entity_failures=failures,
            fallback_blocks=dict(state_blocks),
            coverage={"requested_kinds": len(requested),
                      "fetched_kinds": len(fetchable),
                      "requested_entities": len(unique),
                      "returned_entities": len(entities),
                      "complete": (len(fetchable) == len(requested)
                                   and not failures)},
            warnings=(),
            error_code=None if entities else "all_entities_failed")


def _state_block(kind: str, state: str, provider: str,
                 reason: str | None,
                 granularity: str = "unknown") -> PressureBlock:
    """받지 못한 종류도 자기 자리를 갖는다. 빠뜨리지 않는다.

    사용자가 여섯 종류를 물었는데 셋만 돌아오면 나머지 셋이 '없음'인지
    '못 물어봄'인지 알 수 없다. 그래서 못 준 것도 사유를 달고 남는다.
    """
    if state == "verifying":
        status, why = "unverified", "release_gate_closed"
        message = (f"{kind} 는 데이터 계약 검증 중이라 아직 제공하지 "
                   "않습니다. API 키 문제가 아닙니다.")
    elif state == "unverified":
        status, why = "unverified", reason or "not_verified"
        message = (f"{kind} 는 응답은 오지만 데이터를 확인하지 못했습니다. "
                   "된다고도 안 된다고도 말하지 않습니다.")
    elif state == "not_configured":
        status, why = "not_configured", "provider_not_configured"
        message = f"{provider} 가 연결되어 있지 않습니다."
    else:
        status = "unsupported"
        why = (reason if reason and reason != "unsupported"
               else "not_provided_by_provider")
        message = f"{kind} 는 {provider} 가 제공하지 않습니다."
    return PressureBlock(
        kind=kind, status=status, provider=provider, market=MARKET,
        rows=(), data_as_of=None, data_completeness="none",
        warnings=(message,), unavailable_reason=why,
        coverage={"rows": 0, "complete": False}, granularity=granularity)
