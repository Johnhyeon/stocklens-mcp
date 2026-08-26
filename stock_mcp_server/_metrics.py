"""MCP 도구 호출 메트릭 수집.

각 도구 호출 시 JSONL 파일에 기록한다.
추후 get_metrics_summary 도구로 집계/분석 가능.

저장 위치: ~/.stocklens/logs/metrics_YYYYMMDD.jsonl
          (2026-08 이전: ~/Downloads/kstock/logs/ — 기존 사용자 것은 읽기만 계속한다)

기록 항목:
- timestamp: 호출 시각
- tool: 도구 이름
- kwargs: 주요 파라미터 (값 일부만, 길면 truncate)
- duration_ms: 실행 시간
- output_chars: 응답 문자 수
- output_tokens: 추정 토큰 수 (tiktoken 또는 근사)
- cache_hit: 캐시 히트 추정 (duration < 10ms)
- error: 에러 발생 시 타입
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Awaitable

_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_DETAIL_MAX = 200


def _error_detail(exc: Exception) -> str | None:
    """예외 메시지를 로그에 담을 수 있는 형태로. 메시지가 없으면 None.

    쿼리스트링은 통째로 지운다 — httpx 예외 문자열에는 요청 URL이 그대로 들어가고
    거기에 크리덴셜이 실린다. 이 파일은 지원 번들에 담겨 고객이 메일로 내보낸다.
    """
    msg = str(exc).strip()
    if not msg:
        return None
    return _QUERY_RE.sub("?…", msg)[:_DETAIL_MAX]

# tiktoken은 선택적 의존성이자 최초 사용 시 인코딩 파일을 인터넷에서 내려받는다.
# import 시점에 바로 불러오면 네트워크가 없는/제한된 PC에서 서버 자체가
# 뜨지 못하므로, 실제로 토큰을 세는 시점까지 로딩을 미루고 실패하면 즉시
# 문자 수 기반 근사로 전환한다. 인코더 로딩은 프로세스당 최대 1회만 시도한다.
_encoder = None
_encoder_load_attempted = False
_ENCODER_LOAD_TIMEOUT = 2.0  # 초. 제한된 네트워크에서 다운로드가 멈춰 있어도 빨리 포기.


def _load_encoder():
    """tiktoken 인코더를 백그라운드 스레드에서 불러오고 최대 _ENCODER_LOAD_TIMEOUT초만 기다린다.

    socket.setdefaulttimeout()으로 감싸는 방식은 쓰지 않는다 — 그건 프로세스 전역
    설정이라, 이 로딩이 진행되는 동안 동시에 실행 중인 다른 도구 호출(예: yfinance가
    명시적 타임아웃 없이 여는 커넥션)까지 의도치 않게 잘라버릴 수 있다. 별도 스레드로
    격리하면 지정한 타임아웃 안에 못 끝나도 이 스레드만 백그라운드에 남고(daemon이라
    프로세스 종료를 막지 않음), 다른 동시 요청에는 전혀 영향을 주지 않는다.
    """
    global _encoder, _encoder_load_attempted
    if _encoder is not None or _encoder_load_attempted:
        return _encoder
    _encoder_load_attempted = True

    result: dict = {}

    def _attempt() -> None:
        try:
            import tiktoken
            result["encoder"] = tiktoken.get_encoding("cl100k_base")
        except Exception:
            result["encoder"] = None

    thread = threading.Thread(target=_attempt, daemon=True)
    thread.start()
    thread.join(timeout=_ENCODER_LOAD_TIMEOUT)
    _encoder = result.get("encoder")  # 타임아웃 시 None — 스레드는 백그라운드에서 계속되다 버려짐
    return _encoder


def estimate_tokens(text: str) -> int:
    """텍스트의 토큰 수를 추정한다.

    tiktoken 인코더 로딩(최초 1회)에 성공하면 정확한 카운트, 실패하면
    (미설치/오프라인/네트워크 제한) 한국어·영어·숫자 혼합 기준 문자당
    약 0.35 토큰으로 근사한다.
    """
    encoder = _load_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return len(text) // 3


def _state_home() -> Path:
    """앱 상태 폴더. licensing._home()과 같은 규칙(STOCKLENS_HOME 존중)."""
    import os

    base = os.environ.get("STOCKLENS_HOME")
    return Path(base) if base else (Path.home() / ".stocklens")


def _legacy_metrics_dir() -> Path:
    """2026-08 이전에 쓰던 자리. 읽기만 한다."""
    from stock_mcp_server._excel import get_snapshot_dir

    return get_snapshot_dir() / "logs"


def get_metrics_dir() -> Path:
    """로그 저장 폴더. ~/.stocklens/logs/

    예전에는 엑셀 저장 폴더(~/Downloads/kstock/)에 얹어 썼다. 그 폴더가 Downloads에
    있는 건 '사용자가 내보낸 엑셀을 찾을 수 있게'라는 이유였고 그건 지금도 맞다.
    문제는 사용자가 열 일 없는 진단 로그까지 거기 쌓았다는 것이다 — Downloads는
    사람들이 주기적으로 비우는 폴더라, 정리 한 번에 지원 문의 때 필요한 기록이
    통째로 날아간다(실측 42MB·81일치). DartLens는 처음부터 ~/.dartlens/logs 를
    썼으므로 여기서 그쪽에 맞춘다.
    """
    folder = _state_home() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_metrics_dirs() -> list[Path]:
    """읽을 때 훑을 폴더들 — 새 위치 먼저, 옛 위치 나중.

    기존 사용자의 로그는 옛 위치에 그대로 있다. 옮기지 않는다: 42MB를 복사하다
    실패하면 하필 지원 문의에 필요한 자료가 반쯤 날아가는데, 얻는 건 없다.
    새 기록만 새 자리에 쌓고 읽을 때 둘 다 보면 아무도 아쉬울 일이 없다.
    """
    dirs = [get_metrics_dir()]
    legacy = _legacy_metrics_dir()
    if legacy not in dirs:
        dirs.append(legacy)
    return dirs


def get_metrics_file() -> Path:
    """오늘 날짜의 메트릭 파일 경로."""
    date_str = datetime.now().strftime("%Y%m%d")
    return get_metrics_dir() / f"metrics_{date_str}.jsonl"


def _sanitize_kwargs(kwargs: dict) -> dict:
    """kwargs를 JSON-직렬화 가능한 형태로 변환 + 긴 값 truncate."""
    result = {}
    for k, v in kwargs.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            if isinstance(v, str) and len(v) > 50:
                result[k] = v[:47] + "..."
            else:
                result[k] = v
        elif isinstance(v, (list, tuple)):
            # 리스트면 길이만 기록
            result[k] = f"<list len={len(v)}>"
        elif isinstance(v, dict):
            result[k] = f"<dict keys={list(v.keys())}>"
        else:
            result[k] = f"<{type(v).__name__}>"
    return result


def track_metrics(tool_name: str) -> Callable:
    """MCP 도구 함수에 메트릭 추적을 추가하는 데코레이터.

    사용:
        @mcp.tool()
        @safe_tool
        @track_metrics("get_chart")
        async def get_chart(...): ...
    """
    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            token = _current_tool.set(tool_name)
            start = time.monotonic()
            error_type: str | None = None
            error_detail: str | None = None
            result_text = ""
            try:
                result = await func(*args, **kwargs)
                if result is not None:
                    result_text = str(result)
                return result
            # BaseException 까지 잡는 이유: asyncio.CancelledError 는 Exception 이
            # 아니라 BaseException 이다. 클라이언트가 느린 호출을 취소하면 예전엔
            # error=null, output_chars=0 으로 기록돼 **성공한 것처럼** 보였다.
            # corpCode 같은 벌크 다운로드는 취소될 여지가 실제로 있다. 잡아서
            # 기록만 하고 그대로 다시 올린다(흐름은 바뀌지 않는다).
            except BaseException as e:
                error_type = type(e).__name__
                # 타입만 남기면 "ConnectError"가 전부라 원인을 못 좁힌다 — 이름 조회
                # 실패인지·거부인지·프록시인지에 따라 사용자가 할 일이 완전히 다르다
                # (2026-08-13 문의에서 실제로 여기서 막혔다). 쿼리스트링은 지우고
                # 담는다 — httpx 예외 문자열엔 요청 URL이 통째로 들어가고 거기에
                # 크리덴셜이 실린다.
                error_detail = _error_detail(e)
                raise
            finally:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                try:
                    record = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "tool": tool_name,
                        "kwargs": _sanitize_kwargs(kwargs),
                        "duration_ms": duration_ms,
                        "output_chars": len(result_text),
                        "output_tokens": estimate_tokens(result_text),
                        "cache_hit": duration_ms < 10.0,  # 10ms 이하는 캐시 히트로 추정
                        "error": error_type,
                        "error_detail": error_detail,
                    }
                    with open(get_metrics_file(), "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception:
                    # 메트릭 실패가 도구 호출 자체를 막으면 안 됨
                    pass
                # 도구 밖에서 current_tool() 이 남아 있지 않게 되돌린다.
                _current_tool.reset(token)

        return wrapper
    return decorator


def load_metrics(days: int = 1) -> list[dict]:
    """최근 N일간의 메트릭 레코드를 전부 로드.

    Args:
        days: 몇 일치를 로드할지 (기본 1, 오늘만)
    """
    from datetime import timedelta
    records: list[dict] = []
    today = datetime.now()

    folders = get_metrics_dirs()
    for i in range(days):
        date = today - timedelta(days=i)
        date_str = date.strftime("%Y%m%d")
        # 같은 날짜 파일은 한 곳에만 있다(새 기록은 새 자리에만 쌓인다). 옮기던 날
        # 양쪽에 걸쳐 있을 수는 있으므로, 먼저 찾은 쪽(새 위치)만 쓰고 멈춘다.
        for folder in folders:
            file_path = folder / f"metrics_{date_str}.jsonl"
            if not file_path.exists():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue  # 권한 등 — 못 읽는 폴더 때문에 전체가 실패하면 안 된다
            break

    return records


def summarize_metrics(records: list[dict]) -> dict:
    """메트릭 레코드를 집계해서 도구별 통계 반환."""
    by_tool: dict[str, dict] = {}

    for r in records:
        tool = r.get("tool", "unknown")
        if tool not in by_tool:
            by_tool[tool] = {
                "call_count": 0,
                "cache_hits": 0,
                "errors": 0,
                "total_duration_ms": 0.0,
                "total_output_chars": 0,
                "total_output_tokens": 0,
                "durations": [],
                "token_counts": [],
            }

        s = by_tool[tool]
        s["call_count"] += 1
        if r.get("cache_hit"):
            s["cache_hits"] += 1
        if r.get("error"):
            s["errors"] += 1
        s["total_duration_ms"] += r.get("duration_ms", 0)
        s["total_output_chars"] += r.get("output_chars", 0)
        s["total_output_tokens"] += r.get("output_tokens", 0)
        s["durations"].append(r.get("duration_ms", 0))
        s["token_counts"].append(r.get("output_tokens", 0))

    # 평균/p50/p95 계산
    for tool, s in by_tool.items():
        durations = sorted(s["durations"])
        tokens = sorted(s["token_counts"])
        n = len(durations)
        if n > 0:
            s["avg_duration_ms"] = round(s["total_duration_ms"] / n, 1)
            s["p50_duration_ms"] = durations[n // 2]
            s["p95_duration_ms"] = durations[int(n * 0.95)] if n > 1 else durations[0]
            s["avg_tokens"] = round(s["total_output_tokens"] / n)
            s["p50_tokens"] = tokens[n // 2]
            s["cache_hit_rate"] = round(s["cache_hits"] / n * 100, 1)
        # 리스트는 제거 (요약용)
        del s["durations"]
        del s["token_counts"]

    return by_tool


# ---------------------------------------------------------------------------
# 커버리지 한계 카운터 (메타 규약 v3)
# ---------------------------------------------------------------------------
# 도구가 요청보다 적게 돌려주는 일이 얼마나 자주 일어나는지 세지 않으면, 계약을
# 만들어 놓고도 "그래서 실제로 얼마나 잘리나"에 답할 수 없다.
#
# 라벨은 고정 집합만 받는다. 종목코드·티커·검색어를 라벨로 받으면 두 가지가
# 동시에 깨진다: 시계열 카디널리티가 종목 수만큼 늘어나고, 지원 번들로 나가는
# 로그에 고객이 무엇을 조회했는지가 그대로 남는다.

COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    "lens_coverage_truncated_total": ("lens", "tool", "reason"),
    "lens_incomplete_bar_total": ("tool", "timeframe"),
    "lens_mixed_period_total": ("tool",),
    "lens_unknown_adjustment_total": ("tool",),
}

_LABEL_VALUE_MAX = 40

# 실행 중인 도구 이름. 카운터 라벨 하나 때문에 도구 이름을 40곳 호출부까지
# 인자로 실어 나르지 않으려고 여기 둔다.
_current_tool: ContextVar = ContextVar("lens_current_tool", default=None)


def current_tool():
    """지금 실행 중인 MCP 도구 이름. 도구 밖에서는 None."""
    return _current_tool.get()


def get_counters_file() -> Path:
    """오늘 날짜의 카운터 파일. 도구 호출 기록과 **다른 파일**이다.

    한 파일에 섞으면 기존 파서(load_metrics 등)가 모양이 다른 줄을 만나 조용히
    어긋난다. 지원 번들에는 같은 폴더째로 담기므로 나눠도 함께 나간다.
    """
    date_str = datetime.now().strftime("%Y%m%d")
    return get_metrics_dir() / f"counters_{date_str}.jsonl"


def count_limitation(metric: str, **labels) -> None:
    """커버리지 한계를 1 센다. 허용 라벨 밖의 값은 받지 않는다.

    라벨이 틀리면 조용히 넘어가지 않고 ValueError 로 올린다. 라벨은 전부 코드에
    박힌 상수라, 틀렸다면 그건 버그이지 사용자 입력이 아니다. 파일 쓰기 실패만
    삼킨다 - 메트릭 때문에 도구 호출이 죽으면 안 된다.
    """
    allowed = COUNTER_LABELS.get(metric)
    if allowed is None:
        raise ValueError(f"정의되지 않은 카운터: {metric!r} (허용: {sorted(COUNTER_LABELS)})")
    if set(labels) != set(allowed):
        raise ValueError(
            f"{metric} 의 라벨은 정확히 {sorted(allowed)} 여야 합니다 (받음: {sorted(labels)})"
        )
    for key, value in labels.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"라벨 {key} 는 비어 있지 않은 문자열이어야 합니다 (받음: {value!r})")
        if len(value) > _LABEL_VALUE_MAX:
            raise ValueError(f"라벨 {key} 가 너무 깁니다({len(value)}자). 자유 문자열은 라벨이 아닙니다.")

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "metric": metric,
        "labels": {k: labels[k] for k in allowed},
        "value": 1,
    }
    try:
        with open(get_counters_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
