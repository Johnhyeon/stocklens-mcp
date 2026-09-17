"""StockLens test package.

테스트는 pytest 로만 돌린다. 실사용 홈 격리(STOCKLENS_HOME·체험 표식)는
tests/conftest.py 가 하는데, `python -m unittest discover` 는 conftest 를 읽지
않는다. 2026-09-17 그렇게 돌렸다가 개발자 실사용 홈(~/.stocklens)의
broker_state.json 이 테스트 연결 상태로 덮여 KIS 연결이 끊겼고, 가짜 분봉이
실사용 캐시에 '완전한 하루'로 다시 저장됐다(2026-08-27 과 같은 오염).

그래서 pytest 밖에서 이 패키지를 불러오면 멈춘다. `python tests/xxx.py` 로
직접 실행하는 수동 점검 스크립트는 이 파일을 거치지 않는다.
"""

import sys

if "pytest" not in sys.modules:
    raise ImportError(
        "StockLens 테스트는 pytest 로만 실행합니다 "
        "(예: uv run --no-sync --with pytest python -m pytest tests). "
        "unittest 로 돌리면 conftest 격리가 빠져 실사용 홈 ~/.stocklens 를 덮어씁니다."
    )
