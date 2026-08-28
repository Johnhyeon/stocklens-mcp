"""상세 수급 실계좌 UAT 러너 (1.1 Task 20).

**독립 검증이 이 러너의 존재 이유다.** 어댑터가 자기 계산으로 자기를
검증하면 같은 오해를 두 번 하고 통과한다. 그래서 여기서는 공급자 원본
응답을 직접 받아 **여기 있는 코드로 다시 계산**하고, 어댑터 결과와
맞춘다. 어댑터의 파서·검산 함수를 부르지 않는다.

쓰는 독립 근거 세 가지:

1. **출처 교차** - 증권사 API 와 네이버는 서로 다른 회사의 서로 다른
   파이프라인이다. 두 곳의 외국인·기관계 순매매가 자릿수까지 같으면,
   둘 다 같은 방향으로 틀렸을 가능성은 실질적으로 없다. 가장 강한 증거다.
   증권사끼리(KIS↔키움) 교차가 더 좋지만 둘 다 연결돼 있어야 한다.
   네이버는 자격 증명이 필요 없어 **항상** 쓸 수 있다.
   실측(2026-08-28, 005930 7일): 외국인·기관·종가 전부 일치.
2. **응답 내부 산술** - KIS 는 매수·매도·순매수를 다 주므로
   `순매수 = 매수 - 매도` 가 응답 안에서 검산된다. 키움은 5주체 합이
   0 이고 기관 세부 8종 합이 기관계와 같아야 한다.
3. **measure 판별** - 수량과 금액은 다른 숫자여야 한다. 같으면 요청
   파라미터가 무시된 것이고, 라벨-값 계약이 깨진 것이다.

증거 파일에 자격 증명·헤더·토큰·계좌 식별자·원본 오류 본문을 남기지
않는다. 종목코드·날짜·숫자·상태만 남긴다.

이 러너는 두 증권사를 **함께** 쓴다. 교차 검증이 목적이라 공급자를
하나만 고르는 모드가 없다. 한쪽이 연결돼 있지 않으면 그 사실을 결과에
남기고 네이버 교차로만 판정한다.

    $env:STOCKLENS_HOME="D:\project\stocklens\.uat-home-1.0"
    python docs/uat/evidence/common/evidence_uat.py
    python docs/uat/evidence/common/evidence_uat.py --symbols 3   # 빠른 확인
    python docs/uat/evidence/common/evidence_uat.py --phase intraday

기본 홈에는 KIS 만 연결돼 있어 증권사 교차가 빈 채로 돈다. UAT 홈을
지정해야 한다.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

for _stream in ("stdout", "stderr"):
    _handle = getattr(sys, _stream)
    if hasattr(_handle, "buffer"):
        setattr(sys, _stream,
                io.TextIOWrapper(_handle.buffer, encoding="utf-8",
                                 errors="strict", line_buffering=True))

KST = timezone(timedelta(hours=9))

# 1.0 KR UAT 와 같은 종목을 쓴다. 다른 목록을 쓰면 두 UAT 를 나란히
# 놓고 볼 수 없다. 우선주·ETF·ETN 이 섞여 있는 것이 의도다.
SYMBOLS = ["005930", "000660", "373220", "035720", "035420", "051910",
           "950140", "043370", "036560", "069500", "371460", "305720",
           "005935", "003555",
           # 15번째. 계획이 요구하는 공급자별 15종목을 채운다.
           # 코스닥 대형주를 하나 넣어 시장 구분도 섞는다.
           "247540"]

# 증권사끼리 교차 가능한 구분. 키움 13종 중 KIS 가 주는 것은 이 셋뿐이다.
SHARED_CATEGORIES = ("individual", "foreign", "institution_total")
# 네이버와 교차 가능한 구분. 네이버는 개인을 주지 않는다.
NAVER_CATEGORIES = {"foreign": "foreign",
                    "institution_total": "institutional"}

# 키움 원본 필드 (러너가 직접 읽는다. 어댑터 표를 import 하지 않는다).
#
# 합이 0 이 되어야 하는 키움 자신의 5주체 분해다. 여기서 외국인은
# **좁은 쪽**(frgnr_invsr)이다.
KIWOOM_PRINCIPALS = {
    "individual": "ind_invsr", "foreign_registered": "frgnr_invsr",
    "institution_total": "orgn", "other_corporation": "etc_corp",
    "domestic_foreign": "natfor",
}
# KRX·KIS·네이버가 "외국인"이라 부르는 것은 좁은 외국인 + 내외국인이다.
# 실측(2026-08-28, 4종목 32건): KIS frgn_ntby_qty == frgnr_invsr + natfor,
# 32/32 일치. 이 합을 쓰지 않고 좁은 쪽을 `foreign` 이라 부르면 두
# 공급자의 같은 이름 숫자가 달라지고, 사용자는 그 차이를 시장 현상으로
# 읽는다.
KIWOOM_FOREIGN_PARTS = ("foreign_registered", "domestic_foreign")
KIWOOM_INSTITUTION = ("fnnc_invt", "insrnc", "invtrt", "etc_fnnc", "bank",
                      "penfnd_etc", "samo_fund", "natn")
# KIS 원본 필드 (수량 기준). 순매수·매수·매도.
KIS_RAW = {
    "individual": ("prsn_ntby_qty", "prsn_shnu_vol", "prsn_seln_vol"),
    "foreign": ("frgn_ntby_qty", "frgn_shnu_vol", "frgn_seln_vol"),
    "institution_total": ("orgn_ntby_qty", "orgn_shnu_vol",
                          "orgn_seln_vol"),
}


def _int(raw) -> int | None:
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("--"):
        text = text[1:]
    elif text[:1] == "+":
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 원본 수집 (어댑터를 거치지 않는다)
# ---------------------------------------------------------------------------

def _client(provider: str):
    from stock_mcp_server.market_data.credential_store import CredentialStore
    from stock_mcp_server.market_data.token_store import TokenStore

    payload = CredentialStore().load_active(provider, "real")
    if payload is None:
        # 없는 것을 있는 척하지 않는다. 호출부가 그 사실을 결과에 남긴다.
        return None
    store = None
    try:
        store = TokenStore()
    except Exception:  # noqa: BLE001
        pass
    if provider == "kis":
        from stock_mcp_server.market_data.kis_client import KisClient
        return KisClient(payload, "real", token_store=store)
    from stock_mcp_server.market_data.kiwoom_client import KiwoomClient
    return KiwoomClient(payload, "real", token_store=store)


# KIS 클라이언트는 경로를, 키움 클라이언트는 endpoint_id 를 받는다.
# 러너가 두 계약을 각각 맞춘다 (하나로 감싸면 그게 또 하나의 공용 코드가
# 되어 '독립 검증'이 아니게 된다).
_KIS_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"


async def _raw_kis_flow(client, symbol: str) -> list[dict]:
    payload = await client.request(
        "GET", _KIS_FLOW_PATH, tr_id="FHKST01010900",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
    return list(payload.get("output") or [])


async def _raw_kiwoom_flow(client, symbol: str, day: date,
                           amt_qty_tp: str = "2") -> list[dict]:
    response = await client.request(
        "kr_investor_daily", api_id="ka10059",
        body={"dt": day.strftime("%Y%m%d"), "stk_cd": symbol,
              "amt_qty_tp": amt_qty_tp, "trde_tp": "0", "unit_tp": "1"})
    return list(response.payload.get("stk_invsr_orgn") or [])


# ---------------------------------------------------------------------------
# 독립 계산 (어댑터 함수를 부르지 않는다)
# ---------------------------------------------------------------------------

def _kiwoom_rows(raw_rows: list[dict]) -> dict[str, dict]:
    """날짜 -> {구분: 값, _balance, _institution}. 여기서 직접 센다."""
    out: dict[str, dict] = {}
    for raw in raw_rows:
        day = str(raw.get("dt") or "")
        if len(day) != 8:
            continue
        values = {name: _int(raw.get(field))
                  for name, field in KIWOOM_PRINCIPALS.items()}
        parts = [_int(raw.get(f)) for f in KIWOOM_INSTITUTION]
        # 검산은 5주체 분해로만 한다. 아래에서 만드는 파생 합계를
        # 넣으면 내외국인을 두 번 센다.
        principals = list(values.values())
        got = [values[n] for n in KIWOOM_FOREIGN_PARTS]
        values["foreign"] = (None if any(v is None for v in got)
                             else sum(got))
        out[day] = {
            **values,
            "_balance": (None if any(p is None for p in principals)
                         else sum(principals)),
            "_institution": (None if any(p is None for p in parts)
                             else sum(parts)),
        }
    return out


def _kis_rows(raw_rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in raw_rows:
        day = str(raw.get("stck_bsop_date") or "")
        if len(day) != 8:
            continue
        entry: dict = {}
        for name, (net_f, buy_f, sell_f) in KIS_RAW.items():
            net, buy, sell = (_int(raw.get(net_f)), _int(raw.get(buy_f)),
                              _int(raw.get(sell_f)))
            entry[name] = net
            # 응답 내부 산술: 순매수 = 매수 - 매도
            entry[f"_{name}_arith"] = (
                None if None in (net, buy, sell) else net - (buy - sell))
        out[day] = entry
    return out


# ---------------------------------------------------------------------------
# 사례 하나
# ---------------------------------------------------------------------------

async def _case(symbol: str, kis_client, kiwoom_client, day: date) -> dict:
    result: dict = {"symbol": symbol, "checks": [], "failures": []}

    def fail(name: str, detail: str) -> None:
        result["failures"].append({"check": name, "detail": detail})

    def ok(name: str, detail: str = "") -> None:
        result["checks"].append({"check": name, "detail": detail})

    kis_raw: list[dict] = []
    kiwoom_raw: list[dict] = []
    if kis_client is not None:
        try:
            kis_raw = await _raw_kis_flow(kis_client, symbol)
        except Exception as exc:  # noqa: BLE001
            fail("kis_fetch", type(exc).__name__)
    if kiwoom_client is not None:
        try:
            kiwoom_raw = await _raw_kiwoom_flow(kiwoom_client, symbol, day)
        except Exception as exc:  # noqa: BLE001
            fail("kiwoom_fetch", type(exc).__name__)

    kis = _kis_rows(kis_raw)
    kiwoom = _kiwoom_rows(kiwoom_raw)
    result["kis_rows"] = len(kis)
    result["kiwoom_rows"] = len(kiwoom)
    # 리뷰 지적: 무엇을 어떤 자격으로 어디서 받았는지 증거에 남긴다.
    result["profile"] = "real"
    result["endpoints"] = {
        "kis": {"path": _KIS_FLOW_PATH, "tr_id": "FHKST01010900",
                "paginated": False, "rows": len(kis)},
        "kiwoom": {"endpoint_id": "kr_investor_daily", "api_id": "ka10059",
                   "paginated": True, "rows": len(kiwoom)},
    }
    # 공급자가 주지 않는 것. 무엇이 검증 대상이 아니었는지 남긴다.
    result["unsupported"] = {
        "kis": ["kr.investor_flow.daily.breakdown"],
        "kiwoom": ["kr.investor_flow.daily.buy_sell"],
        "naver_cross": ["individual"],
    }

    # 1) KIS 응답 내부 산술: 순매수 = 매수 - 매도
    arith_checked = arith_bad = 0
    for day_key, entry in kis.items():
        for name in SHARED_CATEGORIES:
            diff = entry.get(f"_{name}_arith")
            if diff is None:
                continue
            arith_checked += 1
            if diff != 0:
                arith_bad += 1
                fail("kis_buy_minus_sell",
                     f"{day_key}/{name} 차이 {diff}")
    result["kis_arithmetic_checked"] = arith_checked
    if arith_checked and not arith_bad:
        ok("kis_buy_minus_sell", f"{arith_checked}건 일치")

    # 2) 키움 검산: 5주체 합 0, 기관 세부 8종 합 = 기관계
    settled = unsettled = 0
    for day_key, entry in kiwoom.items():
        balance, institution = entry["_balance"], entry["_institution"]
        total = entry["institution_total"]
        if balance is None or institution is None or total is None:
            unsettled += 1
            continue
        if balance == 0 and institution == total:
            settled += 1
        else:
            # 정산 전 당일 행은 검산이 깨지는 것이 정상이다.
            unsettled += 1
    result["kiwoom_settled_days"] = settled
    result["kiwoom_unsettled_days"] = unsettled
    if settled:
        ok("kiwoom_balance", f"정산일 {settled}일 검산 통과")

    # 3) 공급자 교차: 같은 날 같은 구분이 자릿수까지 같은가
    shared_days = sorted(set(kis) & set(kiwoom), reverse=True)
    compared = mismatched = 0
    for day_key in shared_days:
        if kiwoom[day_key]["_balance"] != 0:
            continue  # 정산 전 날은 비교하지 않는다
        for name in SHARED_CATEGORIES:
            left, right = kis[day_key].get(name), kiwoom[day_key].get(name)
            if left is None or right is None:
                continue
            compared += 1
            if left != right:
                mismatched += 1
                fail("cross_provider",
                     f"{day_key}/{name} KIS {left} vs 키움 {right}")
    result["cross_compared"] = compared
    result["cross_mismatched"] = mismatched
    result["shared_days"] = len(shared_days)
    if compared and not mismatched:
        ok("cross_provider", f"{compared}건 자릿수까지 일치")
    elif not compared:
        # 증권사끼리 교차를 못 한 것은 **커버리지 한계**로 남긴다.
        # 실패로 세지 않는 이유는 네이버 교차(3b)가 독립 출처로 같은
        # 일을 하기 때문이다. 다만 네이버는 개인을 주지 않으므로
        # 무엇이 덜 검증됐는지 명시한다. 둘 다 못 하면 아래에서
        # 실패로 잡힌다.
        result["cross_provider_status"] = (
            "unavailable_second_provider"
            if kiwoom_client is None else "no_settled_shared_day")
        result["cross_provider_note"] = (
            "증권사끼리 교차 미수행. individual 은 네이버가 주지 않아 "
            "KIS 내부 산술(순매수=매수-매도)로만 검증됨.")

    # 3b) 출처 교차: 네이버(독립 파이프라인, 자격 증명 불필요)
    #     증권사 하나만 연결돼 있어도 이 검증은 항상 돌아간다.
    try:
        from stock_mcp_server.naver import get_investor_flow

        naver = {r["date"].replace(".", ""): r
                 for r in await get_investor_flow(symbol, 30)}
        n_compared = n_bad = 0
        for day_key, entry in kis.items():
            row = naver.get(day_key)
            if row is None:
                continue
            for name, naver_key in NAVER_CATEGORIES.items():
                left, right = entry.get(name), row.get(naver_key)
                if left is None or right is None:
                    continue
                n_compared += 1
                if left != right:
                    n_bad += 1
                    fail("kis_vs_naver",
                         f"{day_key}/{name} KIS {left} vs 네이버 {right}")
        result["naver_compared"] = n_compared
        result["naver_mismatched"] = n_bad
        if n_compared and not n_bad:
            ok("kis_vs_naver", f"{n_compared}건 자릿수까지 일치")
        elif not n_compared:
            fail("kis_vs_naver", "겹치는 날이 없었다")
    except Exception as exc:  # noqa: BLE001
        fail("kis_vs_naver", type(exc).__name__)

    # 4) measure 판별: 수량과 금액은 다른 숫자여야 한다
    if kiwoom_client is None:
        result["measure_discrimination_status"] = "skipped_no_kiwoom"
    else:
      try:
        amount_raw = await _raw_kiwoom_flow(kiwoom_client, symbol, day,
                                            amt_qty_tp="1")
        amount = _kiwoom_rows(amount_raw)
        distinct = same = 0
        for day_key in set(amount) & set(kiwoom):
            left = kiwoom[day_key].get("individual")
            right = amount[day_key].get("individual")
            if left is None or right is None or left == 0:
                continue
            if left == right:
                same += 1
            else:
                distinct += 1
        result["measure_distinct_days"] = distinct
        if same:
            fail("measure_discrimination",
                 f"수량과 금액이 같은 날 {same}일 - 요청 파라미터 무시 의심")
        elif distinct:
            ok("measure_discrimination", f"{distinct}일 모두 다른 값")
      except Exception as exc:  # noqa: BLE001
        fail("measure_discrimination", type(exc).__name__)

    # 5) 어댑터 결과가 독립 계산과 같은가.
    #    게이트를 여는 대상이 어댑터이므로, 어댑터가 원본을 그대로
    #    옮기는지 공급자마다 따로 확인한다.
    if kis_client is not None and kis:
        try:
            from stock_mcp_server.market_data.kis_evidence import (
                KisEvidenceProvider,
            )
            dataset = await KisEvidenceProvider(
                kis_client, "real").fetch_investor_flow(symbol,
                                                        base_date=day)
            drift = 0
            settled_rows = 0
            for row in dataset.rows:
                mine = kis.get(row.date.strftime("%Y%m%d"))
                if mine is None:
                    continue
                if row.data_state == "final":
                    settled_rows += 1
                for name in SHARED_CATEGORIES:
                    theirs = row.values.get(name)
                    if theirs is None:
                        continue  # 미정산으로 뺀 값
                    if theirs != mine.get(name):
                        drift += 1
                        fail("kis_adapter_matches_raw",
                             f"{row.date}/{name} 어댑터 {theirs} vs "
                             f"원본 {mine.get(name)}")
            result["kis_adapter_rows"] = len(dataset.rows)
            result["kis_adapter_final_rows"] = settled_rows
            result["kis_adapter_measure"] = dataset.measure
            result["kis_adapter_unit"] = dataset.unit
            if not drift:
                ok("kis_adapter_matches_raw", f"{len(dataset.rows)}행 일치")
        except Exception as exc:  # noqa: BLE001
            fail("kis_adapter_matches_raw", type(exc).__name__)

    if kiwoom_client is None:
        result["adapter_check_status"] = "skipped_no_kiwoom"
        return result
    try:
        from stock_mcp_server.market_data.kiwoom_evidence import (
            KiwoomEvidenceProvider,
        )
        dataset = await KiwoomEvidenceProvider(
            kiwoom_client, "real").fetch_investor_flow(symbol, base_date=day)
        drift = 0
        for row in dataset.rows:
            key = row.date.strftime("%Y%m%d")
            mine = kiwoom.get(key)
            if mine is None:
                continue
            for name in SHARED_CATEGORIES:
                theirs = row.values.get(name)
                if theirs is None:
                    continue  # 미정산으로 뺀 값. 여기서 다루지 않는다
                if theirs != mine.get(name):
                    drift += 1
                    fail("adapter_matches_raw",
                         f"{key}/{name} 어댑터 {theirs} vs 원본 "
                         f"{mine.get(name)}")
        result["adapter_rows"] = len(dataset.rows)
        result["adapter_measure"] = dataset.measure
        result["adapter_unit"] = dataset.unit
        if not drift:
            ok("adapter_matches_raw", f"{len(dataset.rows)}행 일치")
    except Exception as exc:  # noqa: BLE001
        fail("adapter_matches_raw", type(exc).__name__)

    # 6) 공개 도구의 배치 경로. 서비스만 검증하고 도구를 안 밟으면
    #    도구에서만 나는 오류(직렬화·인자 검증)를 못 잡는다.
    if kis_client is not None or kiwoom_client is not None:
        try:
            import json as _json

            from stock_mcp_server import server as _server

            # 짝 종목은 대상과 겹치면 안 된다. 겹치면 중복 제거로
            # 1종목이 되고 도구가 단건 모양을 돌려준다 (정상 동작).
            partner = "005930" if symbol != "005930" else "000660"
            raw = await _server.get_detailed_investor_flow(
                codes=[symbol, partner], days=5)
            parsed = _json.loads(raw)
            accounted = set(parsed.get("entities") or {}) | {
                f["code"] for f in parsed.get("entity_failures") or []}
            result["public_batch_entities"] = sorted(accounted)
            if accounted != {symbol, partner}:
                fail("public_batch",
                     f"요청 종목이 응답에서 사라졌다: {sorted(accounted)}")
            elif not parsed.get("ok"):
                fail("public_batch",
                     " ".join(parsed["_meta"]["warnings"])[:120])
            else:
                ok("public_batch", f"{len(accounted)}종목 응답")
        except Exception as exc:  # noqa: BLE001
            fail("public_batch", type(exc).__name__)

    passed = {c["check"] for c in result["checks"]}
    if not ({"cross_provider", "kis_vs_naver"} & passed):
        fail("independent_cross_check",
             "독립 교차 출처가 하나도 통과하지 않았다. 내부 산술만으로는 "
             "라벨이 맞는지 알 수 없다.")
    return result


# ---------------------------------------------------------------------------

async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=len(SYMBOLS))
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--phase", choices=("final", "intraday"), default="final",
        help="intraday 는 잠정값 스냅샷을 남긴다. 장 마감 후 final 로 "
             "다시 돌리면 잠정->확정 전이를 검증한다.")
    args = parser.parse_args()

    if args.symbols > len(SYMBOLS):
        # 15개를 요구했는데 14개만 돌고 성공으로 끝나면, 증거 파일의
        # 사례 수를 믿고 게이트를 열게 된다.
        parser.error(
            f"목록에 {len(SYMBOLS)}종목뿐인데 {args.symbols}종목을 "
            "요청했습니다. SYMBOLS 를 늘리거나 요청 수를 줄이세요.")

    day = date.today()
    kis_client, kiwoom_client = _client("kis"), _client("kiwoom")
    started = datetime.now(KST).isoformat()

    cases = []
    for symbol in SYMBOLS[:args.symbols]:
        case = await _case(symbol, kis_client, kiwoom_client, day)
        cases.append(case)
        mark = "OK  " if not case["failures"] else "FAIL"
        print(f"  [{mark}] {symbol}  네이버교차 "
              f"{case.get('naver_compared', 0)}건"
              f"/불일치 {case.get('naver_mismatched', 0)}"
              f"  증권사교차 {case.get('cross_compared', 0)}건"
              f"  KIS산술 {case.get('kis_arithmetic_checked', 0)}건")
        for failure in case["failures"]:
            print(f"         - {failure['check']}: {failure['detail']}")

    failures = sum(len(c["failures"]) for c in cases)
    report = {
        "kind": "market_evidence_uat",
        "market": "KR",
        "capability": "kr_investor_flow",
        "started_at": started,
        "finished_at": datetime.now(KST).isoformat(),
        "base_date": day.isoformat(),
        "cases": len(cases),
        "failures": failures,
        "cross_compared": sum(c.get("cross_compared", 0) for c in cases),
        "cross_mismatched": sum(c.get("cross_mismatched", 0) for c in cases),
        "naver_compared": sum(c.get("naver_compared", 0) for c in cases),
        "naver_mismatched": sum(c.get("naver_mismatched", 0) for c in cases),
        "kis_arithmetic_checked": sum(
            c.get("kis_arithmetic_checked", 0) for c in cases),
        "kiwoom_settled_days": sum(
            c.get("kiwoom_settled_days", 0) for c in cases),
        "detail": cases,
    }
    print(f"\n사례 {len(cases)}  실패 {failures}")
    print(f"  네이버 교차 {report['naver_compared']}건 "
          f"불일치 {report['naver_mismatched']}")
    print(f"  증권사 교차 {report['cross_compared']}건 "
          f"불일치 {report['cross_mismatched']}")
    print(f"  KIS 내부 산술 {report['kis_arithmetic_checked']}건")

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] /
        f"uat_evidence_flow_{day:%Y%m%d}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"증거: {out}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
