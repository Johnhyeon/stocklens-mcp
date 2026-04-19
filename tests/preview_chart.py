"""차트 HTML 프리뷰 — 개발/튜닝용 로컬 렌더러.

사용 예:
  python tests/preview_chart.py 005930                       # 단일(일봉 120)
  python tests/preview_chart.py 005930 --tf week --count 52  # 주봉 52
  python tests/preview_chart.py 005930 --multi                # 일/주/월 통합
  python tests/preview_chart.py 005930 --multi --no-sr        # S/R 오버레이 끔
  python tests/preview_chart.py 037330 --out my_chart.html    # 커스텀 파일명

생성된 HTML은 tests/preview_output/ 폴더에 저장되고, 기본적으로 브라우저가 자동 오픈됩니다.
(--no-open 으로 자동 오픈 끔)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server.naver import get_current_price, get_ohlcv
from stock_mcp_server._chart_html import render_chart_html, render_multi_chart_html


PREVIEW_DIR = Path(__file__).parent / "preview_output"


async def build(args) -> Path:
    PREVIEW_DIR.mkdir(exist_ok=True)

    info = await get_current_price(args.code)
    name = info.get("name", args.code) if info else args.code

    # custom S/R (테스트용 예시 — 실제 Claude가 넘길 형태 시뮬)
    custom_sr = None
    if args.custom_sr:
        # format: "support:1440,resistance:1520,support:1280-1300:주요지지"
        custom_sr = []
        for spec in args.custom_sr.split(","):
            parts = spec.strip().split(":")
            kind = parts[0]
            price_spec = parts[1]
            label = parts[2] if len(parts) >= 3 else ""
            if "-" in price_spec:
                lo, hi = price_spec.split("-")
                item = {"kind": kind, "low": int(lo), "high": int(hi)}
            else:
                item = {"kind": kind, "price": int(price_spec)}
            if label:
                item["label"] = label
            custom_sr.append(item)

    if args.multi:
        defaults = {"day": 120, "week": 52, "month": 24}
        frames = []
        for tf in ["day", "week", "month"]:
            ohlcv = await get_ohlcv(args.code, tf, defaults[tf])
            if ohlcv:
                frames.append({"timeframe": tf, "ohlcv": ohlcv})
        html = render_multi_chart_html(
            args.code, name, frames,
            show_sr=not args.no_sr, custom_sr=custom_sr,
        )
        suffix = "multi"
    else:
        ohlcv = await get_ohlcv(args.code, args.tf, args.count)
        if not ohlcv:
            raise SystemExit(f"OHLCV 없음: {args.code}")
        html = render_chart_html(
            args.code, name, ohlcv,
            timeframe=args.tf, show_sr=not args.no_sr, custom_sr=custom_sr,
        )
        suffix = f"{args.tf}_{args.count}"

    out = Path(args.out) if args.out else PREVIEW_DIR / f"{args.code}_{suffix}.html"
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("code", help="종목코드 6자리 (예: 005930)")
    p.add_argument("--tf", default="day", choices=["day", "week", "month"])
    p.add_argument("--count", type=int, default=120)
    p.add_argument("--multi", action="store_true", help="일/주/월 통합")
    p.add_argument("--no-sr", action="store_true", help="자동 지지/저항 오버레이 끔")
    p.add_argument(
        "--custom-sr",
        help="커스텀 S/R. 형식: 'support:1440,resistance:1520:전고점,support:1280-1300:주요지지'",
    )
    p.add_argument("--out", help="출력 파일 경로")
    p.add_argument("--no-open", action="store_true", help="브라우저 자동 오픈 안 함")
    args = p.parse_args()

    out = asyncio.run(build(args))
    size_kb = out.stat().st_size / 1024
    print(f"저장: {out}")
    print(f"크기: {size_kb:.1f} KB")

    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
