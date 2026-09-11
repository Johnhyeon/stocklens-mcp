"""증권사 연동 국내·미국 공통 차트 선로 (broker market data routing).

설계: docs/plans/2026-08-27-broker-market-data-routing-design.md (워크스페이스 루트)

이 패키지는 기존 naver.py / yfinance_source.py 를 변경하지 않는다.
어댑터가 기존 결과를 공통 모델(BarDataset)로 변환하고, 요청 단위로
공급원을 고정(SourceResolution)해 한 결과 안에서 공급원을 섞지 않는다.
"""
