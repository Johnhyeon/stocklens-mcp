# -*- coding: utf-8 -*-
"""1.0.0 설치본 스모크 (Phase 1 step 6).

경로를 인자나 환경변수로 받는다 (임시 폴더 하드코딩 금지).

    python rc1_smoke.py --venv <venv경로> [--home <STOCKLENS_HOME>]

    RC1_VENV / RC1_STOCKLENS_HOME 환경변수로도 지정할 수 있다.
"""
import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# argparse 의 안내·오류는 stderr 로 나간다. 여기를 감싸지 않으면
# 코드페이지 949 콘솔에서 한국어 안내가 깨져 나온다.
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def _resolve_paths() -> "tuple[Path, str, str]":
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--venv", default=os.environ.get("RC1_VENV"),
                        help="산출물을 설치한 venv 경로 (Scripts 상위)")
    parser.add_argument("--home",
                        default=os.environ.get("RC1_STOCKLENS_HOME"),
                        help="검증에 쓸 STOCKLENS_HOME")
    # 기대 버전을 소스에 박아 두면 판올림마다 여기를 놓쳐서 멀쩡한 빌드가
    # 실패한다. 1.0.0rc1 -> 1.0.0 때 실제로 그랬다.
    parser.add_argument("--version", default=os.environ.get("RC1_VERSION", "1.0.0"),
                        help="설치본에서 기대하는 버전 (기본 1.0.0)")
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
    return scripts, home, args.version


VENV, UAT, EXPECT_VERSION = _resolve_paths()
fails = []
# 설치본에 없는 실행 파일. 마지막에 한 번에 보고한다.
_missing: list[str] = []
# 자식 출력이 깨진 채로 검증을 통과시키지 않기 위한 목록
_mojibake: list = []


def run(exe, args, request=None, flag=False, home=UAT):
    env = dict(os.environ)
    env["STOCKLENS_HOME"] = home
    # 자식 출력을 UTF-8 로 읽으므로 자식도 UTF-8 로 쓰게 한다. 이게 없으면
    # 코드페이지 949 환경에서 한국어 출력이 CP949 로 나와 여기서
    # UnicodeDecodeError 로 죽는다 (2026-08-28 재현 확인).
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("LEETKIT_ENABLE_EXPERIMENTAL_BROKERS", None)
    if flag:
        env["LEETKIT_ENABLE_EXPERIMENTAL_BROKERS"] = "1"
    target = VENV / exe
    if not target.exists():
        # 실행 파일이 없으면 traceback 으로 죽지 않고 실패로 보고한다.
        # 검증기가 죽으면 나머지 검사 결과가 통째로 사라진다.
        class _Missing:
            returncode = 127
            stdout = ""
            stderr = f"실행 파일 없음: {exe}"
        _missing.append(exe)
        return _Missing()
    p = subprocess.run([str(VENV / exe)] + args,
                       input=json.dumps(request) if request else None,
                       capture_output=True, text=True, encoding="utf-8",
                       # 깨진 바이트를 조용히 넘기지 않는다. 대체 문자가
                       # 나오면 아래에서 검증을 실패시킨다 - 증거가
                       # 읽히지 않는 채로 통과하면 안 된다.
                       errors="replace",
                       env=env)
    if "�" in (p.stdout or "") or "�" in (p.stderr or ""):
        _mojibake.append(" ".join([exe] + args))
    return p


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# 1) 설치본 실행 파일 존재
for exe in ("stocklens-broker.exe", "stocklens-doctor.exe",
            "stock-mcp-server.exe", "leetkit-manager.exe"):
    check(f"실행 파일 {exe}", (VENV / exe).exists())

# 2) 고객 모드: 공급자 목록에 토스 없음
p = run("stocklens-broker.exe", ["--json", "--non-interactive", "--stdin"],
        {"contract_version": 1, "action": "describe_providers",
         "provider": "kis"})
ids = [x["provider_id"] for x in json.loads(p.stdout).get("providers", [])]
check("고객 모드 공급자 = kis,kiwoom", ids == ["kis", "kiwoom"], str(ids))

# 3) 개발자 모드: 토스 노출
p = run("stocklens-broker.exe", ["--json", "--non-interactive", "--stdin"],
        {"contract_version": 1, "action": "describe_providers",
         "provider": "kis"}, flag=True)
ids2 = [x["provider_id"] for x in json.loads(p.stdout).get("providers", [])]
check("개발자 모드 공급자에 toss 포함", "toss" in ids2, str(ids2))

# 4) 고객 모드 status: 토스 레코드 없음 + 토스 직접 조회 차단
p = run("stocklens-broker.exe", ["--json", "--non-interactive", "--stdin"],
        {"contract_version": 1, "action": "status", "provider": "kis"})
st = json.loads(p.stdout).get("status") or {}
check("고객 status 에 toss 없음", "toss" not in st.get("providers", {}),
      str(sorted(st.get("providers", {}))))
p = run("stocklens-broker.exe", ["--json", "--non-interactive", "--stdin"],
        {"contract_version": 1, "action": "status", "provider": "toss"})
d = json.loads(p.stdout)
check("고객 모드 toss status 차단",
      d.get("ok") is False
      and (d.get("error") or {}).get("code") == "provider_not_public")

# 5) 게이트 표가 설치본에서도 동일
code = ("import json;"
        "from stock_mcp_server.market_data.provider_registry import"
        " is_release_verified as v;"
        "print(json.dumps({f'{p}.{c}': v(p, c)"
        " for p in ('kis','kiwoom','toss')"
        " for c in ('kr_intraday','us_intraday','kr_daily')}))")
p = run("python.exe", ["-c", code])
gates = json.loads(p.stdout)
check("게이트: KIS/키움 분봉 4개 열림",
      all(gates[f"{x}.{m}"] for x in ("kis", "kiwoom")
          for m in ("kr_intraday", "us_intraday")))
check("게이트: 토스 분봉 닫힘",
      not gates["toss.kr_intraday"] and not gates["toss.us_intraday"])
check("게이트: 일봉 전부 닫힘",
      not any(gates[f"{x}.kr_daily"] for x in ("kis", "kiwoom", "toss")))

# 6) 도구 source 목록 (고객 모드에 toss 없음)
code2 = ("import os; os.environ.setdefault('STOCKLENS_HOME', r'%s');"
         "from stock_mcp_server import server;"
         "print(','.join(server._public_intraday_sources()))" % UAT)
p = run("python.exe", ["-c", code2])
srcs = p.stdout.strip()
check("고객 모드 source 목록에 toss 없음", "toss" not in srcs, srcs)

# 7) doctor 가 설치본에서 동작하고 버전을 보고
p = run("stocklens-doctor.exe", ["--json"])
try:
    doc = json.loads(p.stdout)
    ver = doc.get("installed_version") or doc.get("version")
    check("doctor --json 동작", isinstance(doc, dict), f"version={ver}")
    check(f"doctor 버전 = {EXPECT_VERSION}", str(ver) == EXPECT_VERSION, str(ver))
except Exception as exc:  # noqa: BLE001
    check("doctor --json 동작", False, f"{type(exc).__name__} {p.stdout[:120]}")

# 8) Manager 버전과 selftest (CLI 에 --version 은 없다: 하위 명령 구조)
p = run("python.exe", ["-c", "import leetkit_manager as m; print(m.__version__)"])
check(f"manager __version__ = {EXPECT_VERSION}", p.stdout.strip() == EXPECT_VERSION,
      p.stdout.strip())
p = run("leetkit-manager.exe", ["selftest"])
check("manager selftest 통과", p.returncode == 0,
      (p.stdout + p.stderr).strip()[-120:])

print()
if _mojibake:
    print("DECODE 실패:", len(_mojibake), _mojibake[:3])
    fails.append("child-output-decode")
if _missing:
    # 검증 대상이 설치돼 있지 않았다는 사실을 통과로 넘기지 않는다.
    print("설치본에 없는 실행 파일:", sorted(set(_missing)))
    fails.append("missing-executable")
print("FAILURES:", len(fails), fails)
sys.exit(1 if fails else 0)
