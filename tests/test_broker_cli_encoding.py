# -*- coding: utf-8 -*-
"""stocklens-broker 는 읽는 쪽 환경과 무관하게 UTF-8 로 써야 한다.

1.0.0 에서 이게 깨졌다. `json.dumps(..., ensure_ascii=False)` 로 만든 한글이
윈도우 시스템 코드페이지(한국은 cp949)로 나갔고, 출력을 UTF-8 로 읽는
LeetKit Manager 의 증권사 연결 화면에서 안내문이 통째로 깨졌다.

출시 검증이 이걸 놓친 이유가 더 중요하다. 검증기는 자식에게
`PYTHONIOENCODING=utf-8` 을 쥐여 주고 불렀고, Manager 는 그러지 않는다.
읽는 쪽이 친절하면 쓰는 쪽 결함이 가려진다. 그래서 **여기서는 그 변수를
일부러 지우고** 자식이 스스로 UTF-8 로 쓰는지만 본다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(request: dict) -> bytes:
    """Manager 와 같은 조건으로 CLI 를 부르고 stdout 바이트를 그대로 받는다."""
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)      # Manager 는 이걸 넣지 않는다
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-c",
         "from stock_mcp_server.broker_cli import main; raise SystemExit(main())",
         "--json", "--non-interactive", "--stdin"],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=env, timeout=120)
    return proc.stdout


class BrokerCliWritesUtf8Tests(unittest.TestCase):
    def test_error_message_is_utf8_without_pythonioencoding(self):
        """한글이 실리는 대표 경로: 잘못된 요청의 오류 메시지."""
        raw = _run_cli({"contract_version": 1, "action": "status",
                        "provider": "존재하지-않는-증권사"})
        self.assertTrue(raw.strip(), "stdout 이 비었습니다")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.fail(
                "stdout 이 UTF-8 이 아닙니다. Manager 화면에서 한글이 깨집니다.\n"
                f"  {exc}\n  앞부분: {raw[:120]!r}")
        payload = json.loads(text)
        message = (payload.get("error") or {}).get("message", "")
        self.assertTrue(
            any("가" <= ch <= "힣" for ch in message),
            f"오류 메시지에 한글이 없어 인코딩을 검증하지 못했습니다: {message!r}")
        self.assertNotIn("�", text, "대체 문자가 섞여 있습니다")

    def test_missing_flags_message_is_utf8(self):
        """플래그 조합이 틀린 경우도 같은 경로로 한글을 뱉는다."""
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c",
             "from stock_mcp_server.broker_cli import main; raise SystemExit(main())",
             "--json"],
            input=b"", capture_output=True, env=env, timeout=120)
        try:
            proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.fail(f"stdout 이 UTF-8 이 아닙니다: {exc}")


class VerifierMatchesManagerTests(unittest.TestCase):
    """검증기가 Manager 와 다른 조건으로 부르면 이 결함을 다시 놓친다."""

    def test_release_smoke_does_not_hand_utf8_to_the_child(self):
        source = (ROOT / "docs" / "release" / "rc1_smoke.py").read_text("utf-8")
        self.assertIn('env.pop("PYTHONIOENCODING", None)', source)
        self.assertNotIn('env["PYTHONIOENCODING"] = "utf-8"', source)


if __name__ == "__main__":
    unittest.main()
