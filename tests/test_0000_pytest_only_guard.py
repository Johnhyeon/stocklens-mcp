"""unittest 로 tests 폴더를 직접 훑으면 여기서 멈춘다.

tests/__init__.py 의 가드는 `python -m unittest discover -s tests` 에서는
불리지 않는다. 시작 폴더를 최상위로 잡으면 패키지 __init__ 을 거치지 않고
모듈을 바로 불러오기 때문이다 (2026-09-17 실측: 테스트 1484개가 conftest
격리 없이 로드됨). 그대로 돌리면 실사용 홈 ~/.stocklens 의 증권사 연결
기록과 분봉 캐시가 테스트 값으로 덮인다.

이 파일은 이름순으로 가장 먼저 불린다. unittest 로더는 import 오류를 모두
삼켜 실패 항목 하나로 바꾸고 나머지를 계속 돌리므로, 예외로는 못 막고
프로세스를 끝낸다. pytest 에서는 아무 일도 하지 않는다.
"""

import os
import sys

if "pytest" not in sys.modules:
    sys.stderr.write(
        "StockLens 테스트는 pytest 로만 실행합니다 "
        "(예: uv run --no-sync --with pytest python -m pytest tests). "
        "unittest 로 돌리면 conftest 격리가 빠져 실사용 홈 ~/.stocklens 를 "
        "덮어씁니다.\n")
    sys.stderr.flush()
    os._exit(2)
