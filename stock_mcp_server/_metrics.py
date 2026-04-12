"""MCP 도구 호출 메트릭 수집.

각 도구 호출 시 JSONL 파일에 기록한다.
추후 get_metrics_summary 도구로 집계/분석 가능.

저장 위치: ~/Downloads/kstock/logs/metrics_YYYYMMDD.jsonl

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
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Awaitable

# tiktoken은 선택적 의존성
try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(text: str) -> int:
        try:
            return len(_encoder.encode(text))
        except Exception:
            return len(text) // 3
except ImportError:
    def estimate_tokens(text: str) -> int:
        """tiktoken 없을 때 근사값.

        한국어/영어/숫자 혼합 기준 문자당 약 0.35 토큰.
        """
        return len(text) // 3


def get_metrics_dir() -> Path:
    """로그 저장 폴더. ~/Downloads/kstock/logs/"""
    from stock_mcp_server._excel import get_snapshot_dir
    folder = get_snapshot_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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
            start = time.monotonic()
            error_type: str | None = None
            result_text = ""
            try:
                result = await func(*args, **kwargs)
                if result is not None:
                    result_text = str(result)
                return result
            except Exception as e:
                error_type = type(e).__name__
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
                    }
                    with open(get_metrics_file(), "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception:
                    # 메트릭 실패가 도구 호출 자체를 막으면 안 됨
                    pass

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

    for i in range(days):
        date = today - timedelta(days=i)
        date_str = date.strftime("%Y%m%d")
        file_path = get_metrics_dir() / f"metrics_{date_str}.jsonl"
        if not file_path.exists():
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

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
