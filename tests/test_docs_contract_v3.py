"""문서가 메타 규약 v3 를 실제로 설명하는가 - 정적 검사.

코드에 필드를 넣어 놓고 문서에 안 적으면, 그 필드는 없는 것과 같다. 읽는 쪽은
문서를 보고 무엇을 믿을지 정한다. 여기서 보는 것은 "그 말이 문서 어딘가에
있는가"뿐이고, 문장이 좋은지까지는 사람이 본다.

패치노트 버전 검사도 함께 둔다. 릴리스 워크플로가 같은 걸 보는데, 거기서 걸리면
이미 태그를 밀어 놓은 뒤다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _version() -> str:
    m = re.search(r'^version = "([^"]+)"', _text("pyproject.toml"), re.M)
    assert m, "pyproject.toml 에 version 이 없습니다."
    return m.group(1)


def test_patchnotes_has_a_section_for_the_current_version():
    """앱은 main 브랜치의 패치노트를 그대로 읽는다. 버전이 없으면 빈 화면이 뜬다."""
    version = _version()
    sections = re.findall(r"^## ([^\s]+) ", _text("PATCHNOTES.md"), re.M)
    assert version in sections, f"PATCHNOTES.md 에 {version} 절이 없습니다 (있는 것: {sections[:5]})"


def test_patchnote_sections_keep_the_shape_the_app_parses():
    heads = [l for l in _text("PATCHNOTES.md").splitlines() if l.startswith("## ")]
    assert heads, "패치노트 절이 하나도 없습니다."
    for h in heads:
        assert re.match(r"^## \S+ . \d{4}-\d{2}-\d{2}$", h), h


# StockLens 도구 레퍼런스가 설명해야 하는 v3 필드
SL_KEYS = [
    "coverage",
    "coverage_complete",
    "requested",
    "effective",
    "truncated",
    "bar_state",
    "last_bar_complete",
    "price_adjustment",
    "period_coverage",
    "indicator_coverage",
    "data_completeness",
]


@pytest.mark.parametrize("rel", ["guides/ko/TOOLS.md", "guides/en/TOOLS.md"])
@pytest.mark.parametrize("key", SL_KEYS)
def test_tool_reference_documents_v3_fields(rel, key):
    assert key in _text(rel), f"{rel} 에 {key} 설명이 없습니다."


@pytest.mark.parametrize("rel", ["guides/ko/TOOLS.md", "guides/en/TOOLS.md"])
def test_reference_shows_a_requested_vs_effective_example(rel):
    """말로만 적으면 안 읽힌다. 60일을 물어 20일이 오는 예가 있어야 한다."""
    body = _text(rel)
    assert "60" in body and "20" in body
    assert "get_flow_batch" in body


@pytest.mark.parametrize("rel", ["guides/ko/TOOLS.md", "guides/en/TOOLS.md",
                                 "README.md", "README.en.md"])
def test_new_fields_are_declared_optional(rel):
    """기존 소비자가 무시해도 된다는 사실이 어딘가에 적혀 있어야 한다."""
    body = _text(rel).lower()
    assert "meta_v" in body or "v3" in body
    assert any(w in body for w in ("선택", "optional"))
