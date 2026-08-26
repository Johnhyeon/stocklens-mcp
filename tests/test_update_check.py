"""업데이트 알림의 버전 비교 - 낮은 버전을 새 버전이라고 말하면 안 된다.

실측(2026-08-27): packaging 을 불러오지 못하는 환경에서 fallback 이
"버전이 다르기만 하면 새 버전"이라, 현재 0.8.2 인데 PyPI 캐시가 0.8.0 이던
시점에 "새 버전 v0.8.0" 안내가 만들어졌다. 그 안내가 JSON 도구 응답 뒤에
붙으면서 json.loads 까지 깨졌다(안내 주입 회귀는 test_coverage_contract_v3).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_mcp_server import _update_check as uc


class VersionCompareTests(unittest.TestCase):
    def test_lower_version_is_not_newer(self):
        self.assertFalse(uc._version_gt("0.8.0", "0.8.2"))

    def test_higher_version_is_newer(self):
        self.assertTrue(uc._version_gt("0.8.3", "0.8.2"))
        self.assertTrue(uc._version_gt("0.10.0", "0.9.9"))
        self.assertTrue(uc._version_gt("1.0.0", "0.8.2"))

    def test_same_version_is_not_newer(self):
        self.assertFalse(uc._version_gt("0.8.2", "0.8.2"))

    def test_fallback_without_packaging_is_still_numeric(self):
        """packaging 이 없어도 '다르면 새 버전'으로 떨어지면 안 된다."""
        with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertFalse(uc._version_gt("0.8.0", "0.8.2"))
            self.assertTrue(uc._version_gt("0.8.3", "0.8.2"))
            self.assertFalse(uc._version_gt("0.8.2", "0.8.2"))
            # 자리 수가 달라도 숫자로 비교한다
            self.assertTrue(uc._version_gt("0.9", "0.8.2"))
            self.assertFalse(uc._version_gt("0.8", "0.8.2"))

    def test_fallback_trailing_zero_is_the_same_version(self):
        """0.8.2.0 과 0.8.2 는 같은 버전이다. 튜플 길이로 이기면 안 된다."""
        with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertFalse(uc._version_gt("0.8.2.0", "0.8.2"))
            self.assertFalse(uc._version_gt("0.8.2", "0.8.2.0"))
            self.assertTrue(uc._version_gt("0.8.2.1", "0.8.2"))

    def test_fallback_never_promotes_suffixed_versions(self):
        """rc1 의 숫자를 그냥 뽑으면 (0,8,3,1) > (0,8,3) 이 되어 프리릴리스가
        정식판 위로 올라간다. 접미사가 붙은 형태는 fallback 에서 비교하지 않는다."""
        with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertFalse(uc._version_gt("0.8.3rc1", "0.8.3"))
            self.assertFalse(uc._version_gt("0.8.3b1", "0.8.2"))
            self.assertFalse(uc._version_gt("0.8.3.post1", "0.8.2"))
            self.assertFalse(uc._version_gt("0.8.3.dev0", "0.8.2"))

    def test_fallback_accepts_a_leading_v(self):
        with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertTrue(uc._version_gt("v0.8.3", "0.8.2"))
            self.assertFalse(uc._version_gt("v0.8.1", "0.8.2"))

    def test_unparseable_version_is_not_announced(self):
        """숫자를 못 뽑으면 새 버전이라고 주장하지 않는다. 오탐 안내가 더 나쁘다."""
        with patch.dict(sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertFalse(uc._version_gt("", "0.8.2"))
            self.assertFalse(uc._version_gt("unknown", "0.8.2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
