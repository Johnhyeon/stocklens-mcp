<div align="center">

<img src="assets/logo.svg" width="120" height="120" alt="StockLens logo">

# StockLens

**AI가 진짜 데이터로 분석합니다**

[![PyPI](https://img.shields.io/pypi/v/stocklens-mcp.svg)](https://pypi.org/project/stocklens-mcp/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

🇰🇷 **한국어** | [🇺🇸 English](README.en.md)

</div>

---

## 왜 필요한가

AI에게 차트 이미지를 보여주면 **숫자를 추측해서 틀린 분석**을 합니다 (할루시네이션).

**StockLens**는 Claude에 네이버 증권의 **실제 시세 데이터**를 직접 연결해서, AI가 추측이 아닌 **진짜 숫자를 읽고 분석**하도록 만듭니다.

```
❌ "삼성전자 8만원대인 것 같아요" (추측, 틀림)
✅ "삼성전자 206,000원, 20일 이평선 대비 +5.3%" (실제 데이터)
```

## 주요 기능

- 📊 **48개 도구** — 시장 캘린더, 현재가, 차트, 수급, 재무, 실적, 배당, 스크리닝, Excel 출력
- 🔑 **API 키 불필요** — 네이버 증권 + Yahoo Finance 공개 데이터
- 🚀 **빠른 응답** — TTL 캐시 + Semaphore 최적화
- 📁 **Excel 스냅샷** — 한 번 스캔 → 반복 쿼리 즉시
- 🤖 **Gemini/GPT 연동** — Excel 내보내기로 다른 AI에서도 활용

## 빠른 시작 (3줄, Python 사전 설치 불필요)

[`uv`](https://docs.astral.sh/uv/)가 Python 런타임까지 자동으로 설치합니다. 터미널에 한 줄 복붙.

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Johnhyeon/stocklens-mcp/main/install.ps1 | iex"
```

### macOS / Linux (터미널)

```bash
curl -LsSf https://raw.githubusercontent.com/Johnhyeon/stocklens-mcp/main/install.sh | sh
```

스크립트가 ① uv 설치 → ② `uv tool install stocklens-mcp` → ③ Claude Desktop config 자동 등록 → ④ 검증까지 처리합니다. 끝나면 **Claude Desktop을 완전히 종료(트레이 → Quit)** 후 재시작하세요.

> 💡 **응답 속도**
> - **첫 실행**: PyPI에서 패키지 + 의존성 다운로드 (10~30초)
> - **이후 호출**: 1~2초, 같은 종목 재조회는 즉시 (내부 캐시)

### 업데이트

```bash
uv tool upgrade stocklens-mcp
```

또는 위 install 명령을 다시 실행하면 됩니다.

---

### 🔄 기존 pip 사용자

기존 pip 설치를 정리하고 uv로 전환하면 환경이 격리되어 충돌이 사라집니다.

```bash
py -m pip uninstall stocklens-mcp        # 기존 pip 제거 (Windows)
python3 -m pip uninstall stocklens-mcp   # 기존 pip 제거 (mac/Linux)
```

그 후 위 install 명령 한 줄 실행. setup_claude가 Claude config의 기존 entry를 새 절대경로로 자동 갱신합니다.

> 📌 수동 설치 / 트러블슈팅 상세: [설치 가이드](guides/ko/INSTALL.md)

## 동작 확인

Claude에서:
```
삼성전자 현재가 알려줘
```

종목명, 현재가, 전일대비, 거래량이 나오면 설치 완료입니다.

<!-- TODO: 스크린샷 — Claude 응답 예시 -->
<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />

## 설치 문제 진단

```bash
stocklens-doctor
```

uv·패키지·명령·config 4단계 자동 점검. 문제 원인과 고치는 명령어까지 표시. 친구분이 막혔을 때 이 한 줄만 보내주세요.

## 사용 예시

```
"SK하이닉스 120일 일봉 보고 20일 이동평균선 기준으로 추세 판단해줘"
"카카오 외국인/기관 최근 20일 수급 분석해줘"
"시가총액 상위 100개 중 PER 15 이하인 종목 찾아줘"
"오늘 강세 테마 3개 알려주고 각 테마 주도주 분석해줘"
```

> ✅ 릴리즈 전 전 도구 실측 QA + 부하 테스트 통과한 빌드만 배포합니다. ([상세](QUALITY.md))

## 더 알아보기

- [📘 **도구 48개 상세** →](guides/ko/TOOLS.md)
- [💡 **프롬프트 예시 50개** →](guides/ko/USAGE.md)
- [🔧 **설치/트러블슈팅** →](guides/ko/INSTALL.md)

## 지원 환경

| 환경 | 지원 |
|------|------|
| Claude Desktop (앱) | ✅ 메인 |
| Claude Code (CLI) | ✅ |
| Claude.ai (웹) | ❌ 로컬 MCP 미지원 |
| ChatGPT / Gemini | Excel 내보내기로 우회 가능 |

## 지원 시장

- **한국 (KOSPI/KOSDAQ)** — 네이버 증권, 6자리 종목코드 (`005930` = 삼성전자, `000660` = SK하이닉스)
- **미국 (NYSE/NASDAQ)** — Yahoo Finance, 알파벳 티커 (`AAPL`, `TSLA`, `BRK.B`)

티커 형식으로 자동 판별. 자연어로 섞어 써도 됩니다 (예: `"005930이랑 AAPL 비교"`). 전체 도구 목록은 [TOOLS.md](guides/ko/TOOLS.md).

## 기여

이슈, PR 모두 환영합니다. 버그 제보나 기능 요청은 [Issues](https://github.com/Johnhyeon/stocklens-mcp/issues)에 남겨주세요.

## 라이선스

MIT License
