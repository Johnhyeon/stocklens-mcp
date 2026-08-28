# -*- coding: utf-8 -*-
"""1.0.0rc1 설치본: 증권사 하나만 연결한 사용자 흐름 (Phase 1 step 9-10).

경로를 인자나 환경변수로 받는다 (임시 폴더 하드코딩 금지).

    python rc1_single_broker.py --venv <venv경로> [--home <STOCKLENS_HOME>]

    RC1_VENV / RC1_STOCKLENS_HOME 환경변수로도 지정할 수 있다.
"""
import argparse
import io
import shutil
import tempfile
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# argparse 의 안내·오류는 stderr 로 나간다. 여기를 감싸지 않으면
# 코드페이지 949 콘솔에서 한국어 안내가 깨져 나온다.
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def _resolve_paths() -> "tuple[Path, str]":
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--venv", default=os.environ.get("RC1_VENV"),
                        help="산출물을 설치한 venv 경로 (Scripts 상위)")
    parser.add_argument("--home",
                        default=os.environ.get("RC1_STOCKLENS_HOME"),
                        help="검증에 쓸 STOCKLENS_HOME")
    args = parser.parse_args()
    if not args.venv:
        parser.error("--venv 또는 RC1_VENV 가 필요합니다 "
                     "(예: --venv C:/tmp/rc1-clean)")
    venv = Path(args.venv)
    scripts = venv / "Scripts" if (venv / "Scripts").exists() else venv
    if not (scripts / "python.exe").exists() and             not (scripts / "python").exists():
        parser.error(f"venv 에서 python 을 찾지 못했습니다: {scripts}")
    home = args.home or os.environ.get("STOCKLENS_HOME") or ""
    if not home:
        parser.error("--home 또는 RC1_STOCKLENS_HOME 이 필요합니다")
    return scripts, home


VENV, UAT = _resolve_paths()
fails = []
_mojibake: list = []


def run_py(code, home, flag=False):
    env = dict(os.environ)
    env["STOCKLENS_HOME"] = str(home)
    # 코드페이지 949 환경에서도 자식 출력을 UTF-8 로 읽을 수 있게 한다.
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LEETKIT_ENABLE_EXPERIMENTAL_BROKERS", None)
    if flag:
        env["LEETKIT_ENABLE_EXPERIMENTAL_BROKERS"] = "1"
    done = subprocess.run([str(VENV / "python.exe"), "-c", code],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=env)
    if "�" in (done.stdout or "") or "�" in (done.stderr or ""):
        _mojibake.append(code[:40])
    return done


def only(provider: str, tmp: Path) -> Path:
    home = tmp / f"only-{provider}"
    shutil.copytree(UAT, home)
    state = json.loads((home / "broker_state.json").read_text("utf-8"))
    state["providers"] = {k: v for k, v in state["providers"].items()
                          if k == provider}
    state["primary_provider"] = provider
    state["data_source_mode"] = "auto"
    (home / "broker_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return home


FETCH = """
import asyncio, json, os
from stock_mcp_server import server
async def go():
    out = {}
    for market, symbol, venue in (("KR", "005930", None),
                                  ("US", "AAPL", "NAS")):
        err, day = server._validate_intraday_args(market, "5m", "auto", None)
        if err:
            out[market] = {"error": err}
            continue
        try:
            ds, meta = await server._fetch_intraday_dataset(
                symbol=symbol, market=market, interval="5m",
                trading_date=day, row_limit=30, venue=venue,
                session="regular", completed_only=True, source="auto")
            out[market] = {"provider": ds.provider, "rows": len(ds.bars),
                           "reason": meta.get("selection_reason"),
                           "primary": meta.get("primary_provider")}
        except Exception as exc:
            out[market] = {"error": f"{type(exc).__name__}: "
                           f"{getattr(exc, 'provider_status', exc)}"}
    print(json.dumps(out, ensure_ascii=False))
asyncio.run(go())
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    for provider in ("kis", "kiwoom"):
        home = only(provider, tmp)
        st = run_py(
            "import json;from stock_mcp_server import broker_cli;"
            "print(json.dumps(broker_cli.handle_request("
            "{'contract_version':1,'action':'status','provider':'%s'})))"
            % provider, home)
        status = json.loads(st.stdout)["status"]
        check(f"[{provider} 단독] 연결 인식",
              sorted(status["providers"]) == [provider],
              str(sorted(status["providers"])))
        check(f"[{provider} 단독] 주 사용 = {provider}",
              status["primary_provider"] == provider,
              str(status["primary_provider"]))

        r = run_py(FETCH, home)
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            check(f"[{provider} 단독] 분봉 조회", False,
                  (r.stdout + r.stderr).strip()[-200:])
            continue
        for market in ("KR", "US"):
            got = res.get(market, {})
            ok = got.get("provider") == provider and got.get("rows", 0) > 0
            check(f"[{provider} 단독] {market} 분봉을 {provider}에서 수신",
                  ok, json.dumps(got, ensure_ascii=False))

print()
if _mojibake:
    print("DECODE 실패:", len(_mojibake))
    fails.append("child-output-decode")
print("FAILURES:", len(fails), fails)
sys.exit(1 if fails else 0)
