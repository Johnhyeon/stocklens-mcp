"""멀티 증권사 실계좌 UAT 러너 (1.0 Task 24).

자격 증명은 OS keyring 의 정상 제품 흐름으로만 쓴다. 이 스크립트는
환경 변수 credential 을 읽지 않고, 요청 헤더·토큰을 어디에도 남기지
않는다. 실행 전에 Manager 또는 stocklens-broker 로 공급자를 연결한다.

사용:
    python uat_runner.py --provider kiwoom --market KR --out out_dir
    python uat_runner.py --provider toss --market US --out out_dir
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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from stock_mcp_server import server  # noqa: E402
from stock_mcp_server.market_data.resample import (  # noqa: E402
    resample_intraday,
)

KR_SYMBOLS = [
    ("005930", None), ("000660", None), ("373220", None),
    ("035720", None), ("035420", None), ("051910", None),
    ("950140", None), ("043370", None), ("036560", None),
    ("069500", None), ("371460", None), ("305720", None),
    ("005935", None), ("003555", None), ("005930", -5),
]

US_SYMBOLS = [
    ("AAPL", "NAS", None), ("MSFT", "NAS", None), ("NVDA", "NAS", None),
    ("TSLA", "NAS", None), ("IBM", "NYS", None), ("KO", "NYS", None),
    ("JPM", "NYS", None), ("QQQ", "NAS", None), ("SPY", "NYS", None),
    ("IWM", "NYS", None), ("PLTR", "NAS", None), ("SOFI", "NAS", None),
    ("BRK.B", "NYS", None),  # 키움: 구조화 오류가 정답
    ("AAPL", "NAS", -5), ("AAPL", "NAS", -20),
]

INTERVALS = ("1m", "5m", "15m", "60m", "120m", "240m")


def _sanitize(value):
    if isinstance(value, Decimal):
        return float(value)
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


async def _fetch(symbol, market, interval, source, venue, trading_date):
    if trading_date is None:
        err, trading_date = server._validate_intraday_args(
            market, interval, source, None)
        if err:
            raise RuntimeError(err)
    return await server._fetch_intraday_dataset(
        symbol=symbol, market=market, interval=interval,
        trading_date=trading_date, row_limit=200,
        venue=venue, session="regular", completed_only=True,
        source=source)


def _recompute_check(base_1m, target_ds, interval, market):
    """1m 원천을 독립 집계해 대상 간격과 비교한다. 불일치 목록 반환."""
    if not base_1m or not base_1m.bars:
        return ["1m 원천 없음 - 재계산 생략"]
    resampled = resample_intraday(base_1m, interval)
    ours = {b.start_at.isoformat(): b for b in resampled.bars
            if b.complete}
    theirs = {b.start_at.isoformat(): b for b in target_ds.bars
              if b.complete}
    mismatches = []
    for key in sorted(set(ours) & set(theirs)):
        a, b = ours[key], theirs[key]
        for field in ("open", "high", "low", "close", "volume"):
            if getattr(a, field) != getattr(b, field):
                mismatches.append(
                    f"{key} {field}: {getattr(a, field)} != "
                    f"{getattr(b, field)}")
    return mismatches


def run(provider: str, market: str, out_dir: Path) -> int:
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
                    trading_date))
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
                          _recompute_check(base_1m, ds, interval, market))
            record["intervals"][interval] = {
                "rows": len(ds.bars),
                "provider": ds.provider,
                "complete": ds.coverage.get("complete"),
                "warnings": list(ds.warnings),
                "selection_reason": meta.get("selection_reason"),
                "primary_provider": meta.get("primary_provider"),
                "recompute_mismatches": mismatches,
                "first": _sanitize(ds.bars[0].start_at) if ds.bars else None,
                "last": _sanitize(ds.bars[-1].start_at) if ds.bars else None,
            }
            if mismatches:
                failures += 1
        results.append(record)
        print(f"[{provider}/{market}] {symbol} done", flush=True)

    out_file = out_dir / (
        f"uat_{provider}_{market.lower()}_"
        f"{dt.date.today().strftime('%Y%m%d')}.json")
    out_file.write_text(
        json.dumps(_sanitize(results), ensure_ascii=False, indent=1),
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
