"""멀티 증권사 실계좌 UAT 러너 (1.0 Task 24, 리뷰 차단 항목 4 반영판).

원칙:
- 자격 증명은 OS keyring 의 정상 제품 흐름으로만 쓴다. 이 스크립트는
  환경 변수 credential 을 읽지 않고 요청 헤더·토큰을 남기지 않는다.
- 재계산 검산은 운영 코드(resample_intraday)를 재사용하지 않는다.
  설계 명세에서 독립 구현한 집계기로 검산한다 (독립 검산).
- 검증 항목: 간격 6종(명시 source), auto 경로(게이트 미리보기),
  페이지네이션 진행, 완료일 캐시 2회차(공급자 호출 0건), 오류 분류
  (없는 종목·잘못된 키), 주 사용 전환 왕복, KIS 원본 교차 대조.

사용:
    STOCKLENS_HOME=<uat home> python uat_runner.py \
        --provider kiwoom --market KR --out out_dir
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from stock_mcp_server import server  # noqa: E402

KR_SYMBOLS = [
    ("005930", None), ("000660", None), ("373220", None),
    ("035720", None), ("035420", None), ("051910", None),
    ("950140", None), ("043370", None), ("036560", None),
    ("069500", None), ("371460", None), ("305720", None),
    ("005935", None), ("005930", -5), ("005930", -20),
]

US_SYMBOLS = [
    ("AAPL", "NAS", None), ("MSFT", "NAS", None), ("NVDA", "NAS", None),
    ("TSLA", "NAS", None), ("IBM", "NYS", None), ("KO", "NYS", None),
    ("JPM", "NYS", None), ("QQQ", "NAS", None), ("SPY", "NYS", None),
    ("IWM", "NYS", None), ("PLTR", "NAS", None), ("SOFI", "NAS", None),
    ("BRK.B", "NYS", None),  # 키움: 구조화 오류가 정답 (미검증 표기)
    ("AAPL", "NAS", -5), ("AAPL", "NAS", -20),
]

INTERVALS = ("1m", "5m", "15m", "60m", "120m", "240m")

# 독립 검산용 세션 명세 (설계 14절에서 독립 기술. 운영 코드 미참조)
_SESSIONS = {
    "KR": {"tz": ZoneInfo("Asia/Seoul"), "open": (9, 0),
           "minutes": 390},   # 09:00~15:30, 15:30 마감 동시호가 print
    "US": {"tz": ZoneInfo("America/New_York"), "open": (9, 30),
           "minutes": 390},   # 09:30~16:00, 16:00 마감 print
}


def _sanitize(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _shift_trading_day(market: str, offset: int | None):
    if offset is None:
        return None
    from stock_mcp_server.market_data.sessions import session_window
    day = dt.date.today()
    remaining = -offset
    while remaining > 0:
        day = day - dt.timedelta(days=1)
        if session_window(market, day) is not None:
            remaining -= 1
    return day


def independent_aggregate(bars_1m, interval_minutes: int, market: str):
    """설계 명세 기반 독립 집계기. 운영 resample 코드를 쓰지 않는다.

    - 세션 개장 시각 기준 고정 버킷 (offset // interval)
    - 마감 print(개장+390분 시작 봉)는 마지막 버킷에 병합
    - open=첫 봉 open, high=max, low=min, close=마지막 봉 close, vol=합
    """
    spec = _SESSIONS[market]
    buckets: dict = {}
    for bar in sorted(bars_1m, key=lambda b: b.start_at):
        day_open = bar.start_at.replace(
            hour=spec["open"][0], minute=spec["open"][1],
            second=0, microsecond=0)
        offset = int((bar.start_at - day_open).total_seconds() // 60)
        if offset < 0 or offset > spec["minutes"]:
            continue  # 세션 밖 (독립 판정)
        idx = offset // interval_minutes
        if offset == spec["minutes"]:  # 마감 print -> 마지막 버킷
            idx = (offset - 1) // interval_minutes
        start = day_open + dt.timedelta(minutes=idx * interval_minutes)
        key = start.isoformat()
        entry = buckets.get(key)
        if entry is None:
            buckets[key] = {
                "open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "volume": bar.volume,
            }
        else:
            entry["high"] = max(entry["high"], bar.high)
            entry["low"] = min(entry["low"], bar.low)
            entry["close"] = bar.close
            entry["volume"] += bar.volume
    return buckets


def independent_check(base_1m, target_ds, interval: str, market: str):
    """독립 집계와 운영 결과를 비교한다. 불일치 목록 반환."""
    if not base_1m or not base_1m.bars:
        return ["1m 원천 없음 - 검산 생략"]
    minutes = int(interval.rstrip("m"))
    ours = independent_aggregate(base_1m.bars, minutes, market)
    base_first = base_1m.bars[0].start_at
    base_last_end = base_1m.bars[-1].end_at
    mismatches = []
    compared = 0
    for bar in target_ds.bars:
        if not bar.complete:
            continue
        # 베이스 1m 이 완전히 덮는 버킷만 검산 대상이다.
        if bar.start_at < base_first or bar.end_at > base_last_end:
            continue
        mine = ours.get(bar.start_at.isoformat())
        if mine is None:
            mismatches.append(f"{bar.start_at} 독립 집계에 없음")
            continue
        compared += 1
        for field in ("open", "high", "low", "close", "volume"):
            if getattr(bar, field) != mine[field]:
                mismatches.append(
                    f"{bar.start_at} {field}: 운영 {getattr(bar, field)}"
                    f" != 독립 {mine[field]}")
    if compared == 0:
        mismatches.append("검산 가능한 완전 버킷이 없음")
    return mismatches


async def _fetch(symbol, market, interval, source, venue, trading_date,
                 row_limit=200):
    if trading_date is None:
        err, trading_date = server._validate_intraday_args(
            market, interval, source, None)
        if err:
            raise RuntimeError(err)
    return await server._fetch_intraday_dataset(
        symbol=symbol, market=market, interval=interval,
        trading_date=trading_date, row_limit=row_limit,
        venue=venue, session="regular", completed_only=True,
        source=source)


_DIRECT_CLIENTS: dict = {}


def _direct_adapter(provider: str, market: str, home=None):
    """서버 캐시를 거치지 않는 직접 어댑터. 클라이언트는 프로세스당
    1회 생성한다 (KIS 토큰 발급 1분 1회 제한)."""
    from stock_mcp_server.market_data.credential_store import (
        CredentialStore,
    )

    key = (provider, str(home))
    client = _DIRECT_CLIENTS.get(key)
    if client is None:
        payload = CredentialStore(home=home).load_active(provider, "real")
        if payload is None:
            return None
        if provider == "kis":
            from stock_mcp_server.market_data.kis_client import KisClient
            client = KisClient(payload, "real")
        elif provider == "kiwoom":
            from stock_mcp_server.market_data.kiwoom_client import (
                KiwoomClient,
            )
            client = KiwoomClient(payload, "real")
        else:
            from stock_mcp_server.market_data.toss_client import TossClient
            client = TossClient(payload, "real")
        _DIRECT_CLIENTS[key] = client

    if provider == "kis":
        if market == "KR":
            from stock_mcp_server.market_data.kis_domestic import (
                KisDomesticProvider,
            )
            return KisDomesticProvider(client, "real")
        from stock_mcp_server.market_data.kis_overseas import (
            KisOverseasProvider,
        )
        return KisOverseasProvider(client, "real")
    if provider == "kiwoom":
        if market == "KR":
            from stock_mcp_server.market_data.kiwoom_domestic import (
                KiwoomDomesticProvider,
            )
            return KiwoomDomesticProvider(client, "real")
        from stock_mcp_server.market_data.kiwoom_overseas import (
            KiwoomOverseasProvider,
        )
        return KiwoomOverseasProvider(client, "real")
    from stock_mcp_server.market_data.toss_provider import TossBarProvider
    return TossBarProvider(client, "real")


def _direct_fetch_1m(provider, market, symbol, venue, trading_date,
                     home=None):
    """직접 어댑터 1m 조회. (dataset, error_status) 를 돌려준다."""
    from stock_mcp_server.market_data.models import BarRequest

    adapter = _direct_adapter(provider, market, home=home)
    if adapter is None:
        return None, "no_credentials"
    request = BarRequest(
        symbol=symbol, market=market, interval="1m", start=None, end=None,
        trading_date=trading_date, row_limit=500,
        venue=("KRX" if market == "KR" else (venue or "NAS")),
        session="regular", adjustment="unadjusted", completed_only=True,
        source=provider)
    try:
        ds = asyncio.run(adapter.fetch_bars(request))
        return ds, None
    except Exception as exc:  # noqa: BLE001
        return None, str(getattr(exc, "provider_status",
                                 type(exc).__name__))


class _CountingProviders:
    """공급자 fetch 호출 수를 세는 프록시 (캐시 2회차 0건 증명용)."""

    def __init__(self, providers):
        self._providers = providers
        self.calls = 0

    def get(self, key):
        inner = self._providers.get(key)
        if inner is None:
            return None
        proxy = self

        class _Wrap:
            provider_id = getattr(inner, "provider_id", key)

            async def fetch_bars(self, request):
                proxy.calls += 1
                return await inner.fetch_bars(request)

        return _Wrap()

    def __getitem__(self, key):
        wrapped = self.get(key)
        if wrapped is None:
            raise KeyError(key)
        return wrapped

    def __contains__(self, key):
        return key in self._providers


def _cache_second_read_leg(provider, market, symbol, venue, trading_date):
    """완료 거래일 2회차 조회: cache_hit=True + 공급자 호출 0건."""
    real_fn = server._intraday_providers
    counter_box = {}

    def counting(market_arg, source="auto"):
        providers = real_fn(market_arg, source)
        proxy = _CountingProviders(providers)
        counter_box["proxy"] = proxy
        return proxy

    # row_limit 을 작게 잡아 당일 캐시만으로 충족되게 한다 - 크게 잡으면
    # 다일 이력 보충이 정상적으로 이전 거래일을 호출한다.
    with patch.object(server, "_intraday_providers", counting):
        ds, meta = asyncio.run(_fetch(
            symbol, market, "1m", provider, venue, trading_date,
            row_limit=50))
    proxy = counter_box.get("proxy")
    return {
        "cache_hit": bool(meta.get("cache_hit")),
        "provider_calls": proxy.calls if proxy else None,
        "rows": len(ds.bars),
    }


def _auto_route_leg(provider, market, symbol, venue, trading_date):
    """auto 경로 확인. 출시 게이트가 아직 닫혀 있으므로 이 leg 는
    게이트를 임시로 연 상태의 미리보기다 (gate_preview=true 로 기록)."""
    from stock_mcp_server.market_data import provider_registry
    from stock_mcp_server.broker_cli import BrokerService

    service = BrokerService()
    try:
        service.set_primary(provider)
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}
    gate = {(provider, "kr_intraday"): True,
            (provider, "us_intraday"): True}
    with patch.dict(provider_registry._RELEASE_VERIFIED, gate):
        ds, meta = asyncio.run(_fetch(
            symbol, market, "5m", "auto", venue, trading_date))
    return {
        "gate_preview": True,
        "selected_provider": meta.get("selected_provider"),
        "primary_provider": meta.get("primary_provider"),
        "selection_reason": meta.get("selection_reason"),
        "rows": len(ds.bars),
        "provider_matches": ds.provider == provider,
    }


def _error_classification_leg(provider, market, venue):
    """없는 종목 + 잘못된 키의 오류 분류를 기록한다."""
    out = {}
    bad_symbol = "999999" if market == "KR" else "ZZZZQX"
    try:
        asyncio.run(_fetch(bad_symbol, market, "5m", provider, venue,
                           None))
        out["bad_symbol"] = "no_error(데이터 없음일 수 있음)"
    except Exception as exc:  # noqa: BLE001
        out["bad_symbol"] = str(
            getattr(exc, "provider_status", type(exc).__name__))

    # 잘못된 키: 검증기에 가짜 자격 증명을 직접 넣는다 (저장 없음).
    from stock_mcp_server.market_data.provider_registry import registry
    from stock_mcp_server.market_data.secrets import SecretPayload
    schema = registry.require(provider).credential_schema
    fake = SecretPayload.from_schema(
        schema, {f.name: "invalid-value-for-uat" for f in schema})
    if provider == "kiwoom":
        from stock_mcp_server.market_data.kiwoom_verifier import (
            KiwoomVerifier as V,
        )
    elif provider == "toss":
        from stock_mcp_server.market_data.toss_verifier import (
            TossVerifier as V,
        )
    else:
        from stock_mcp_server.market_data.kis_verifier import (
            KisVerifier as V,
        )
    result = asyncio.run(V().verify(fake, "real"))
    out["bad_key_auth"] = result.get("auth")
    return out


def _primary_switch_leg(provider):
    """주 사용 전환 왕복: 다른 연결 공급자로 갔다가 되돌아온다.

    provider -> provider 같은 무의미한 전환은 증거가 아니다 (리뷰).
    다른 연결 공급자가 없으면 skipped 로 기록한다.
    """
    from stock_mcp_server.broker_cli import BrokerService
    from stock_mcp_server.market_data.connection_state import load_state_v2

    service = BrokerService()
    state = load_state_v2()
    before = state["primary_provider"]
    others = [pid for pid, rec in state["providers"].items()
              if pid != provider and rec["lifecycle"] == "connected"
              and rec["profiles"]]
    if not others:
        return {"before": before, "skipped": "다른 연결 공급자 없음"}
    other = others[0]
    steps = []
    try:
        service.set_primary(other)
        steps.append(load_state_v2()["primary_provider"])
        service.set_primary(provider)
        steps.append(load_state_v2()["primary_provider"])
        ok = steps == [other, provider]
        return {"before": before, "via": other, "steps": steps, "ok": ok}
    except Exception as exc:  # noqa: BLE001
        return {"before": before, "via": other, "steps": steps,
                "ok": False, "error": type(exc).__name__}


def _kis_cross_check(market, symbol, venue, base_1m):
    """실사용 홈 자격 증명의 KIS 를 기준으로 같은 날·종목 1m 을 직접
    받아와 대조한다 (전 종목). KIS 는 공식가 대조로 검증된 기준선이다."""
    if not base_1m or not base_1m.bars:
        return None
    # 다일 이력 보충으로 첫 봉이 전일일 수 있다. 요청 거래일 = 마지막 봉.
    target_day = base_1m.bars[-1].start_at.date()
    kis_ds, err = _direct_fetch_1m(
        "kis", market, symbol, venue, target_day,
        home=Path.home() / ".stocklens")
    if kis_ds is None:
        return {"available": False, "error": err}
    kis = {b.start_at.isoformat(): b for b in kis_ds.bars
           if b.start_at.date() == target_day}
    mine = {b.start_at.isoformat(): b for b in base_1m.bars
            if b.start_at.date() == target_day}
    if not kis:
        return {"available": False, "error": "kis_empty"}
    common = sorted(set(kis) & set(mine))
    diffs = 0
    ratios = []
    for t in common:
        a, b = kis[t], mine[t]
        for f in ("open", "high", "low", "close"):
            if getattr(a, f) != getattr(b, f):
                diffs += 1
                break
        if a.volume:
            ratios.append(b.volume / a.volume)
    ratios.sort()
    return {
        "available": True,
        "kis_rows": len(kis), "provider_rows": len(mine),
        "common": len(common),
        "only_kis": len(set(kis) - set(mine)),
        "only_provider": len(set(mine) - set(kis)),
        "ohlc_diff_minutes": diffs,
        "volume_ratio_median": (
            round(ratios[len(ratios) // 2], 4) if ratios else None),
    }


def run(provider: str, market: str, out_dir: Path) -> int:
    """UAT 본체.

    출시 게이트(_RELEASE_VERIFIED)는 검증 전이라 닫혀 있고, 이 러너가
    바로 그 검증 도구다. 러너 프로세스 안에서만 게이트를 열어 실측하고,
    그 사실을 증거에 기록한다. 사용자 경로의 게이트는 그대로 닫혀 있다.
    """
    from stock_mcp_server.market_data import provider_registry

    gate = patch.dict(provider_registry._RELEASE_VERIFIED, {
        (provider, "kr_intraday"): True,
        (provider, "us_intraday"): True,
    })
    gate.start()
    try:
        return _run_inner(provider, market, out_dir)
    finally:
        gate.stop()


def _run_inner(provider: str, market: str, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    symbols = KR_SYMBOLS if market == "KR" else US_SYMBOLS
    failures = 0
    for entry in symbols:
        if market == "KR":
            symbol, offset = entry
            venue = None
        else:
            symbol, venue, offset = entry
        trading_date = _shift_trading_day(market, offset)
        record = {"symbol": symbol, "venue": venue,
                  "trading_date": str(trading_date) if trading_date
                  else "latest", "intervals": {}}
        base_1m = None
        for interval in INTERVALS:
            time.sleep(0.6)  # 호출 제한 보호
            try:
                ds, meta = asyncio.run(_fetch(
                    symbol, market, interval, provider, venue,
                    trading_date,
                    row_limit=500 if interval == "1m" else 200))
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "provider_status",
                                 type(exc).__name__)
                record["intervals"][interval] = {"error": str(status)}
                if symbol not in ("BRK.B",):
                    failures += 1
                continue
            if interval == "1m":
                base_1m = ds
            mismatches = ([] if interval == "1m" else
                          independent_check(base_1m, ds, interval, market))
            record["intervals"][interval] = {
                "rows": len(ds.bars),
                "provider": ds.provider,
                "complete": ds.coverage.get("complete"),
                "pages": ds.coverage.get("pages"),
                "warnings": list(ds.warnings),
                "selection_reason": meta.get("selection_reason"),
                "primary_provider": meta.get("primary_provider"),
                "independent_mismatches": mismatches,
                "first": _sanitize(ds.bars[0].start_at) if ds.bars else None,
                "last": _sanitize(ds.bars[-1].start_at) if ds.bars else None,
            }
            if mismatches:
                failures += 1
        # 페이지네이션 증거: 서버 캐시를 우회한 직접 어댑터 조회의
        # coverage (pages·complete). 캐시 적중 시 pages 가 비는 문제를
        # 이 leg 가 대신 증명한다.
        if base_1m and base_1m.bars:
            direct_ds, direct_err = _direct_fetch_1m(
                provider, market, symbol, venue,
                base_1m.bars[-1].start_at.date())
            if direct_ds is not None:
                record["pagination"] = {
                    "pages": direct_ds.coverage.get("pages"),
                    "rows": len(direct_ds.bars),
                    "complete": direct_ds.coverage.get("complete"),
                }
                if not direct_ds.coverage.get("pages"):
                    failures += 1
            else:
                record["pagination"] = {"error": direct_err}
                failures += 1

        # 완료 거래일이면 캐시 2회차 leg (KR 만 일 단위 캐시 대상)
        if market == "KR" and base_1m and base_1m.bars:
            try:
                record["cache_second_read"] = _cache_second_read_leg(
                    provider, market, symbol, venue,
                    base_1m.bars[-1].start_at.date())
                csr = record["cache_second_read"]
                if not csr.get("cache_hit") or csr.get("provider_calls"):
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                record["cache_second_read"] = {
                    "error": type(exc).__name__}
                failures += 1
        if provider != "kis":
            cross = _kis_cross_check(market, symbol, venue, base_1m)
            record["kis_cross_check"] = cross
            if cross and cross.get("available"):
                # KR 은 KRX 단일 기준이라 OHLC·거래량이 KIS 와 일치해야
                # 한다. US 는 거래소·테이프 기준 차이가 있어 관찰 기록.
                if market == "KR":
                    ratio = cross.get("volume_ratio_median")
                    if cross.get("ohlc_diff_minutes", 0) > 0:
                        failures += 1
                    if ratio is None or abs(ratio - 1.0) > 0.01:
                        failures += 1
            elif cross is not None and market == "KR":
                failures += 1  # KR 기준선 대조 불가는 증거 부족이다
        results.append(record)
        print(f"[{provider}/{market}] {symbol} done", flush=True)

    summary = {
        "provider": provider,
        "market": market,
        "ran_at": dt.datetime.now(
            ZoneInfo("Asia/Seoul")).isoformat(),
        # 이 러너는 검증 도구라서 자기 프로세스 안에서만 출시 게이트를
        # 열고 실측했다. 제품 사용자 경로의 게이트는 닫혀 있다.
        "release_gate_opened_for_verification": True,
        "auto_route": None,
        "error_classification": None,
        "primary_switch": None,
    }
    # 보조 leg 실패도 실패다 (리뷰 잔여 1: 종료코드에 반영).
    try:
        first = symbols[0]
        f_symbol = first[0]
        f_venue = None if market == "KR" else first[1]
        summary["auto_route"] = _auto_route_leg(
            provider, market, f_symbol, f_venue, None)
    except Exception as exc:  # noqa: BLE001
        summary["auto_route"] = {"error": type(exc).__name__}
    if not (summary["auto_route"] or {}).get("provider_matches"):
        failures += 1
    try:
        summary["error_classification"] = _error_classification_leg(
            provider, market, None if market == "KR" else "NAS")
    except Exception as exc:  # noqa: BLE001
        summary["error_classification"] = {"error": type(exc).__name__}
    err_cls = summary["error_classification"] or {}
    if err_cls.get("bad_symbol") != "entity_not_found":
        failures += 1
    if err_cls.get("bad_key_auth") not in ("credential_invalid",
                                           "ip_not_allowed"):
        failures += 1
    try:
        summary["primary_switch"] = _primary_switch_leg(provider)
    except Exception as exc:  # noqa: BLE001
        summary["primary_switch"] = {"error": type(exc).__name__}
    switch = summary["primary_switch"] or {}
    if not switch.get("ok") and not switch.get("skipped"):
        failures += 1
    summary["failures"] = failures

    out_file = out_dir / (
        f"uat_{provider}_{market.lower()}_"
        f"{dt.date.today().strftime('%Y%m%d')}.json")
    out_file.write_text(
        json.dumps({"summary": _sanitize(summary),
                    "symbols": _sanitize(results)},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"saved {out_file} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True,
                        choices=("kis", "kiwoom", "toss"))
    parser.add_argument("--market", required=True, choices=("KR", "US"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    sys.exit(run(args.provider, args.market, Path(args.out)))
