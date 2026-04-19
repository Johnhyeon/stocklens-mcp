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

- 📊 **19개 도구** — 현재가, 차트, 수급, 재무, 스크리닝, Excel 출력
- 🔑 **API 키 불필요** — 네이버 증권 공개 데이터
- 🚀 **빠른 응답** — TTL 캐시 + Semaphore 최적화
- 📁 **Excel 스냅샷** — 한 번 스캔 → 반복 쿼리 즉시
- 🤖 **Gemini/GPT 연동** — Excel 내보내기로 다른 AI에서도 활용

## 빠른 시작 (`.mcpb`, 권장)

Claude Desktop 확장프로그램으로 클릭 몇 번이면 설치 완료 — **Python·의존성 자동 포함, 사전 설치 불필요**.

<!-- TODO: 30초 설치 GIF — 다운로드 → 설정 → 확장프로그램 설치 → 모두 허용 -->
![Install demo](assets/setup.gif)

**순서**
1. [Claude Desktop 다운로드](https://claude.ai/download) → 설치 → 계정 로그인
2. 좌상단 메뉴 → **설정 → 개발자 → 확장프로그램 설치**
3. [Releases 페이지](https://github.com/Johnhyeon/stocklens-mcp/releases/latest)에서 `stocklens-mcp-*.mcpb` 다운로드
4. 받은 `.mcpb` 선택 → **모두 허용**

> 💡 **응답 속도**
> - **설치 시 시간이 좀 걸릴 수 있습니다**
> - **권한 허용**: 사용 시 편의를 위해 설치 후 권한을 모두 허용으로 변경해주세요.
> - **설치 직후 첫 호출**: 1~5분 걸릴 수 있습니다. — Claude Desktop이 Python·의존성을 자동 다운로드합니다. 
    진행 표시가 없으니 **타임아웃 같으면 동일 질문을 한 번 더** 시도하세요.
> - **이후 호출**: 첫 요청 1~2초, 같은 종목 재조회는 즉시 (내부 캐시)

> ⚠️ **pip + `.mcpb` 동시 등록 금지** — 두 방식이 충돌해 응답 멈춤 현상이 발생합니다. 기존 pip 사용자는 아래 안내 참고

---

### 🔄 기존 pip 사용자

**`.mcpb`로 전환 (권장)**:
```bash
py -m pip uninstall stocklens-mcp
```
그 후 `%APPDATA%\Claude\claude_desktop_config.json` 에서 `"stocklens"` 엔트리 삭제 → 위 `.mcpb` 설치 플로우.

**pip 경로 그대로 유지 (업그레이드만)**:
```bash
py -m pip install --upgrade stocklens-mcp
```

> 📌 pip 최초 설치 / 트러블슈팅 상세: [설치 가이드](guides/ko/INSTALL.md)

## 동작 확인

Claude에서:
```
삼성전자 현재가 알려줘
```

종목명, 현재가, 전일대비, 거래량이 나오면 설치 완료입니다.

<!-- TODO: 스크린샷 — Claude 응답 예시 -->
<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />

## 설치 문제 진단 (pip 설치자 전용)

`.mcpb` 설치 시엔 Claude Desktop이 알아서 처리합니다. pip 경로에서 MCP가 안 잡히면:

```bash
stocklens-doctor
```

Python·패키지·명령·config 4단계 자동 점검. 문제 원인과 고치는 명령어까지 표시. 친구분이 막혔을 때 이 한 줄만 보내주세요.

## 사용 예시

```
"SK하이닉스 120일 일봉 보고 20일 이동평균선 기준으로 추세 판단해줘"
"카카오 외국인/기관 최근 20일 수급 분석해줘"
"시가총액 상위 100개 중 PER 15 이하인 종목 찾아줘"
"오늘 강세 테마 3개 알려주고 각 테마 주도주 분석해줘"
```

> ✅ 릴리즈 전 전 도구 실측 QA + 부하 테스트 통과한 빌드만 배포합니다. ([상세](QUALITY.md))

## 더 알아보기

- [📘 **도구 19개 상세** →](guides/ko/TOOLS.md)
- [💡 **프롬프트 예시 50개** →](guides/ko/USAGE.md)
- [🔧 **설치/트러블슈팅** →](guides/ko/INSTALL.md)

## 지원 환경

| 환경 | 지원 |
|------|------|
| Claude Desktop (앱) | ✅ 메인 |
| Claude Code (CLI) | ✅ |
| Claude.ai (웹) | ❌ 로컬 MCP 미지원 |
| ChatGPT / Gemini | Excel 내보내기로 우회 가능 |

## 기여

이슈, PR 모두 환영합니다. 버그 제보나 기능 요청은 [Issues](https://github.com/Johnhyeon/stocklens-mcp/issues)에 남겨주세요.

## 라이선스

MIT License
