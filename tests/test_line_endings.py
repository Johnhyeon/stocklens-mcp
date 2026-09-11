"""줄바꿈이 파일 통째로 바뀌지 않게 한다 (1.1 리뷰 정리 9).

편집 도구가 파일을 읽어 다시 쓰면 줄바꿈이 통일된다. 내용은 한 글자도
안 바뀌었는데 git diff 는 파일 전체가 바뀐 것으로 보이고, 리뷰어는 진짜
변경 395줄을 찾으려고 8,652줄을 넘겨야 한다. 실제로 그렇게 냈다.

이 저장소는 CRLF 파일과 LF 파일이 **섞여 있다**(1.1 이전부터). 그래서
"전부 LF" 같은 규칙을 세우지 않는다. 대신 두 가지만 지킨다.

1. 새로 만드는 파일은 파일 안에서 일관된 줄바꿈을 쓴다.
2. 파일 안에서 줄바꿈이 섞인 파일이 늘어나지 않는다. 섞인 파일은
   부분 재작성의 흔적이고, 다음 편집에서 통째로 뒤집히기 쉽다.

`.gitattributes` 의 `whitespace=cr-at-eol` 은 CR 을 후행 공백 오류로
보지 않게 해서 `git diff --check` 거짓 경보를 없앤다. 줄바꿈 변환
지시가 아니라서 파일 내용에는 영향이 없다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

# 1.1 이전부터 줄바꿈이 섞여 있던 파일 (release/1.0.0rc1 시점 전수 조사
# 결과 정확히 이 둘뿐이다). 고치면 내용 변경 0줄짜리 대형 커밋이 되므로
# 릴리스와 분리해서 따로 판단한다.
#   server.py                    CRLF 8,604 / 8,652줄
#   test_result_meta_contract.py CRLF 422 / 497줄
KNOWN_MIXED = {
    "stock_mcp_server/server.py",
    "tests/test_result_meta_contract.py",
}

# 1.1 에서 새로 만든 모듈·테스트. 이들은 처음부터 LF 로 만들었다.
NEW_IN_1_1 = (
    "stock_mcp_server/market_data/evidence_models.py",
    "stock_mcp_server/market_data/evidence_provider.py",
    "stock_mcp_server/market_data/evidence_router.py",
    "stock_mcp_server/market_data/evidence_service.py",
    "stock_mcp_server/market_data/evidence_capabilities.py",
    "stock_mcp_server/market_data/kis_evidence.py",
    "stock_mcp_server/market_data/kiwoom_evidence.py",
    "stock_mcp_server/market_data/token_store.py",
)


def _style(path: Path) -> str:
    raw = path.read_bytes()
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    if not lf:
        return "none"
    if crlf == lf:
        return "CRLF"
    if crlf == 0:
        return "LF"
    return "mixed"


class LineEndingTests(unittest.TestCase):
    def test_new_1_1_modules_are_internally_consistent(self):
        for name in NEW_IN_1_1:
            path = ROOT / name
            self.assertTrue(path.exists(), name)
            self.assertEqual(_style(path), "LF", name)

    def test_no_new_file_has_mixed_line_endings(self):
        mixed = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if any(part in rel.split("/") for part in
                   (".venv", "build", "dist", "__pycache__", ".worktrees")):
                continue
            if _style(path) == "mixed":
                mixed.append(rel)
        unexpected = sorted(set(mixed) - KNOWN_MIXED)
        self.assertEqual(
            unexpected, [],
            "줄바꿈이 섞인 파일이 늘었다. 파일을 통째로 읽어 다시 쓰는 "
            "편집이 원인일 가능성이 높다.")

    def test_the_gitattributes_hint_is_present(self):
        # 이게 없으면 CRLF 파일에 한 줄만 더해도 git diff --check 가
        # 그 줄마다 오류를 낸다. 진짜 문제를 덮어버린다.
        path = ROOT / ".gitattributes"
        self.assertTrue(path.exists())
        self.assertIn("whitespace=cr-at-eol", path.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
