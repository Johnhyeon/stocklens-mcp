from __future__ import annotations

import base64
import secrets
from datetime import date, datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stock_mcp_server import licensing as L
from stock_mcp_server import trial_notice as N

_EPOCH = date(1970, 1, 1)


def _today() -> date:
    """오늘 날짜를 UTC 로 센다 — `trial_notice._days_left` 가 UTC 로 세기 때문이다.
    로컬 날짜를 쓰면 KST 00~09시에만 하루가 어긋나 "4일 남음"이 "5일 남음"으로 나온다.
    """
    return datetime.now(timezone.utc).date()


@pytest.fixture
def trial(tmp_path, monkeypatch):
    """임시 홈 + 임시 키쌍. 원하는 만료일의 체험 키를 꽂아주는 함수를 돌려준다."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        L, "_PUBLIC_KEY_B64", base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    )
    monkeypatch.setattr(L, "_home", lambda: tmp_path)

    def use(days_left: int | None) -> None:
        payload = L.PRODUCT + secrets.token_bytes(6)
        if days_left is not None:
            expiry = _today() + timedelta(days=days_left)
            payload += ((expiry - _EPOCH).days).to_bytes(4, "big")
        raw = payload + priv.sign(payload)
        key = base64.b32encode(raw).decode().rstrip("=")
        monkeypatch.setattr(L, "stored_key", lambda: key)

    return use


class TestWhoSeesIt:
    def test_paid_key_never_sees_it(self, trial):
        """기간 없는 키를 산 사람에게 체험 안내가 뜨면 그냥 광고다."""
        trial(None)
        assert N.pending_notice() is None

    def test_no_key_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_home", lambda: tmp_path)
        monkeypatch.setattr(L, "stored_key", lambda: None)
        assert N.pending_notice() is None

    def test_already_expired_is_silent(self, trial):
        """끝난 뒤에는 잠금 안내가 따로 나간다 — 여기서 또 말하면 두 번 말하는 셈이다."""
        trial(-1)
        assert N.pending_notice() is None


class TestTiming:
    """정해진 시점에 딱 세 번. 도구 응답은 사용자가 데이터를 보는 화면이라,
    거기에 매번 안내가 붙으면 제품 신뢰가 깎인다."""

    def test_quiet_while_there_is_plenty_of_time(self, trial):
        for left in (14, 13, 10, 8):
            trial(left)
            assert N.pending_notice() is None, left

    def test_fires_at_each_milestone_once(self, trial):
        fired = []
        for left in range(14, -1, -1):
            trial(left)
            if N.pending_notice():
                fired.append(left)
        assert fired == [7, 4, 1]

    def test_a_milestone_is_not_repeated_within_the_same_day(self, trial):
        trial(7)
        assert N.pending_notice() is not None
        assert N.pending_notice() is None

    def test_skipped_days_still_get_caught(self, trial):
        """며칠 안 쓰다가 다시 열어도 놓치면 안 된다 — '정확히 그날'이 아니라
        '그 아래로 내려왔을 때' 판단한다."""
        trial(2)  # 7·4를 건너뛰고 처음 쓰는 상황
        first = N.pending_notice()
        assert first is not None and "2일 남았습니다" in first

    def test_skipped_milestones_do_not_stack(self, trial):
        """7일을 건너뛰고 3일째에 처음 쓰면, 예전엔 7이 먼저 걸려 뜨고 바로 다음
        호출에서 4가 또 걸려 같은 안내가 연달아 두 번 나갔다. 지난 시점은 지난
        것이므로 한 번만 말하고 전부 닫는다."""
        trial(3)
        assert N.pending_notice() is not None
        for _ in range(5):
            assert N.pending_notice() is None

    def test_the_last_milestone_still_fires_after_a_skip(self, trial):
        """한꺼번에 닫되 아직 안 지난 시점(1일)까지 삼키면 안 된다."""
        trial(3)
        N.pending_notice()
        trial(1)
        assert "1일 남았습니다" in N.pending_notice()

    def test_last_day_says_today(self, trial):
        trial(0)
        assert "오늘까지입니다" in N.pending_notice()


class TestContent:
    def test_says_the_date_and_where_to_go(self, trial):
        trial(4)
        notice = N.pending_notice()
        assert "4일 남았습니다" in notice
        assert L.PURCHASE_URL in notice

    def test_is_short(self, trial):
        """도구 응답 뒤에 붙는 글이다 — 길면 데이터를 가린다."""
        trial(4)
        assert len(N.pending_notice().splitlines()) <= 2

    def test_has_no_sales_pressure(self, trial):
        """'지금 안 사면' 같은 말은 쓰지 않는다. 사실만 적는다."""
        trial(1)
        notice = N.pending_notice()
        for banned in ("지금 안", "마지막 기회", "할인", "서두", "놓치"):
            assert banned not in notice, banned


class TestAppendNotice:
    def test_appends_after_the_data(self, trial):
        trial(4)
        out = N.append_notice("삼성전자 현재가 70,000원")
        assert out.startswith("삼성전자 현재가 70,000원")
        assert "체험 기간" in out

    def test_leaves_the_result_alone_when_nothing_to_say(self, trial):
        trial(None)
        assert N.append_notice("데이터") == "데이터"

    def test_non_string_results_pass_through(self, trial):
        trial(4)
        payload = {"a": 1}
        assert N.append_notice(payload) is payload

    def test_never_raises(self, trial, monkeypatch):
        """안내 하나 때문에 도구 응답이 깨지면 안 된다."""
        trial(4)
        monkeypatch.setattr(L, "expires_on", lambda: 1 / 0)
        assert N.append_notice("데이터") == "데이터"
