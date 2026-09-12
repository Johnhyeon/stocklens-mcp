# -*- coding: utf-8 -*-
"""1.0 출시 점검 — **설치본을 MCP 프로토콜로** 실제 호출한다.

`v1_tool_sweep.py` 는 파이썬 함수를 직접 부른다. 그것만으로는 못 보는 게 있다.

- 도구가 MCP 서버에 실제로 등록됐는지 (`@mcp.tool()` 이 빠져도 함수는 돈다)
- 클라이언트가 보낸 인자가 스키마를 통과하는지
- 응답이 JSON-RPC 로 직렬화되는지
- 개발 트리가 아니라 **설치된 패키지**로 그게 되는지

그래서 여기서는 Claude Desktop 과 같은 방식으로 stdio 위에서 JSON-RPC 를
주고받는다. 서버는 별도 프로세스이고, 우리 소스 트리를 import 하지 않는다.

    python docs/release/v1_mcp_e2e.py --venv <venv경로> [--home <STOCKLENS_HOME>]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROTOCOL = "2024-11-05"

# 응답 본문에 이게 있으면 실패다. 메타 봉투 안(warnings)은 보지 않는다.
FAIL_MARKS = ("가져올 수 없습니다", "파싱 실패", "읽지 못했습니다",
              "찾지 못했습니다", "처리 중 오류", "Traceback",
              "라이선스가 필요합니다")

# 호출해 볼 도구. 계열마다 대표를 하나씩 두되, 1.0 에서 새로 열린 것은
# 전부 넣는다. 전수는 v1_tool_sweep.py 가 맡는다.
CALLS = [
    ("stocklens_status", {}),
    ("get_price", {"code": "005930"}),
    ("get_multi_stocks", {"codes": ["005930", "000660"]}),
    ("get_index", {}),
    ("get_chart", {"code": "005930", "timeframe": "day", "count": 20}),
    ("get_financial", {"code": "005930"}),
    ("get_flow", {"code": "005930", "days": 5}),
    ("list_themes", {"page": 1}),
    ("list_sectors", {}),
    ("get_change_ranking", {"direction": "up", "market": "ALL", "count": 3}),
    ("get_reports", {"code": "005930", "count": 2}),
    # 1.0 증권사 연결 구간
    ("get_intraday_chart", {"symbol": "005930", "market": "KR",
                            "interval": "5m", "row_limit": 20}),
    ("get_intraday_indicators", {"symbol": "005930", "market": "KR",
                                 "interval": "60m", "bars": 60}),
    ("get_detailed_investor_flow", {"code": "005930", "days": 5}),
    # 2026-09 개편 신규 자료
    ("get_ipo_schedule", {}),
    ("get_investor_deposit", {"days": 5}),
    ("get_reports", {"kind": "market", "count": 3}),
    # 미국
    ("get_us_price", {"ticker": "AAPL"}),
    ("get_us_chart", {"ticker": "AAPL", "period": "1mo", "interval": "1d"}),
]

# 게이트가 닫혀 있어 ok=false 가 정상인 도구. 닫힌 채로 나가는 게 맞으므로
# 실패로 세지 않되, 무엇이 닫혔는지는 결과에 적는다.
GATED = {"get_supply_pressure"}


class Server:
    """설치본 stocklens 실행 파일과 stdio 로 JSON-RPC 를 주고받는다."""

    def __init__(self, exe: Path, home: str):
        env = dict(os.environ)
        env["STOCKLENS_HOME"] = home
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, bufsize=0)
        self._id = 0
        # stderr 를 계속 비워 준다. 서버는 조회할 때마다 로그를 stderr 로
        # 쏟는데, 우리가 안 읽으면 파이프 버퍼(윈도우 64KB)가 차는 순간
        # 서버가 **로그를 쓰다가 멈춘다.** 그러면 stdout 응답도 같이 멎어서
        # 밖에서 보면 "서버가 죽었다"로 보인다. 이 점검기를 처음 돌렸을 때
        # 실제로 그렇게 멈췄다. 서버 잘못이 아니라 읽는 쪽 잘못이다.
        self._err: list[bytes] = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self._err.append(line)
            del self._err[:-200]        # 최근 것만 남긴다

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read(self, want_id: int, timeout: float = 150.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = b"".join(self._err).decode("utf-8", "replace")
                raise RuntimeError(f"서버가 응답 없이 종료했습니다.\n{err[-1500:]}")
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue                    # 서버 로그 줄은 건너뛴다
            if msg.get("id") == want_id:
                return msg
        raise TimeoutError(f"id={want_id} 응답이 {timeout}초 안에 오지 않았습니다")

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params})
        return self._read(self._id)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def body_of(result: dict) -> str:
    """tools/call 결과에서 사람이 읽는 본문만 꺼낸다."""
    parts = []
    for item in (result.get("content") or []):
        if item.get("type") == "text":
            parts.append(item.get("text") or "")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", default=os.environ.get("E2E_VENV"),
                        help="설치본이 들어 있는 venv 경로")
    parser.add_argument("--home", default=os.environ.get("E2E_HOME"),
                        help="복사해서 쓸 STOCKLENS_HOME (증권사 연결 포함)")
    args = parser.parse_args()
    if not args.venv:
        parser.error("--venv 또는 E2E_VENV 가 필요합니다")

    venv = Path(args.venv)
    exe = venv / "Scripts" / "stocklens.exe"
    if not exe.exists():
        exe = venv / "bin" / "stocklens"
    if not exe.exists():
        parser.error(f"설치본 실행 파일을 찾지 못했습니다: {exe}")

    tmp = Path(tempfile.mkdtemp(prefix="e2e-home-"))
    home = tmp / "home"
    if args.home:
        shutil.copytree(args.home, home)
    else:
        home.mkdir(parents=True)

    fails: list[str] = []
    print(f"설치본: {exe}", flush=True)
    server = Server(exe, str(home))
    try:
        t0 = time.time()
        init = server.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "v1-e2e", "version": "1.0"}})
        if "result" not in init:
            print(f"FAIL initialize: {init}")
            return 1
        info = init["result"].get("serverInfo") or {}
        print(f"initialize OK  ({time.time() - t0:.1f}초) "
              f"서버={info.get('name')} {info.get('version', '')}")
        server.notify("notifications/initialized")

        listed = server.request("tools/list", {})
        tools = {t["name"] for t in listed["result"]["tools"]}
        print(f"tools/list OK  등록된 도구 {len(tools)}개", flush=True)

        missing = [n for n, _ in CALLS if n not in tools]
        if missing:
            fails.append(f"등록 안 된 도구: {missing}")
            print(f"FAIL 등록 안 된 도구: {missing}")

        print()
        print(f"{'도구':30} {'판정':6} {'초':>6}  비고")
        print("-" * 88)
        for name, arguments in CALLS:
            if name not in tools:
                continue
            t0 = time.time()
            resp = server.request("tools/call",
                                  {"name": name, "arguments": arguments})
            sec = time.time() - t0
            if "error" in resp:
                fails.append(name)
                print(f"{name:30} {'ERROR':6} {sec:6.1f}  {resp['error']}")
                continue
            result = resp.get("result") or {}
            text = body_of(result)
            head = text.split("RESULT_META_JSON_START")[0]
            if result.get("isError"):
                fails.append(name)
                verdict, detail = "ERROR", head.strip()[:48]
            elif (hit := next((m for m in FAIL_MARKS if m in head), None)):
                fails.append(name)
                verdict, detail = "FAIL", f"'{hit}' 포함"
            elif len(head.strip()) < 20:
                fails.append(name)
                verdict, detail = "FAIL", f"본문 {len(head.strip())}자"
            else:
                verdict, detail = "OK", f"{len(head)}자"
            print(f"{name:30} {verdict:6} {sec:6.1f}  {detail}", flush=True)

        # 닫힌 게이트가 닫힌 채로 응답하는지도 본다. 조용히 열려 있으면
        # 검증 안 된 값이 고객에게 나간다.
        if "get_supply_pressure" in tools:
            resp = server.request("tools/call", {
                "name": "get_supply_pressure",
                "arguments": {"code": "005930", "kind": "program_trading"}})
            text = body_of(resp.get("result") or {})
            closed = '"unverified"' in text or "release_gate_closed" in text
            print(f"{'get_supply_pressure':30} {'GATED' if closed else 'OPEN':6} "
                  f"{0.0:6.1f}  {'게이트 닫힘 확인' if closed else '열려 있음 — 확인 필요'}")
            if not closed:
                fails.append("get_supply_pressure 게이트가 열려 있다")
    finally:
        server.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 88)
    if fails:
        print(f"실패 {len(fails)}건: {', '.join(str(f) for f in fails)}")
        return 1
    print("전부 통과 — 설치본을 MCP 프로토콜로 호출해 확인했습니다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
