# -*- coding: utf-8 -*-
"""1.0.0rc1 설치본: 증권사 하나만 연결한 사용자 흐름 (Phase 1 step 9-10).

UAT 홈을 임시 폴더로 복사한 뒤 공급자를 하나만 남겨, 그 하나로 국내와
미국 분봉이 실제로 조회되는지 설치본으로 확인한다. 원본 UAT 홈과
프로덕션 홈은 읽기만 한다.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
VENV = Path(r"C:\Users\whdqj\AppData\Local\Temp\claude"
            r"\D--project-stocklens\1bbc70bb-4455-422e-8819-ab68fa1e67cb"
            r"\scratchpad\rc1-clean2\Scripts")
UAT = Path(r"D:\project\stocklens\.uat-home-1.0")
fails = []


def run_py(code, home, flag=False):
    env = dict(os.environ)
    env["STOCKLENS_HOME"] = str(home)
    # 코드페이지 949 환경에서도 자식 출력을 UTF-8 로 읽을 수 있게 한다.
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LEETKIT_ENABLE_EXPERIMENTAL_BROKERS", None)
    if flag:
        env["LEETKIT_ENABLE_EXPERIMENTAL_BROKERS"] = "1"
    return subprocess.run([str(VENV / "python.exe"), "-c", code],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=env)


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
print("FAILURES:", len(fails), fails)
sys.exit(1 if fails else 0)
