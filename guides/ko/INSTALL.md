# StockLens 설치 가이드

[🇺🇸 English](../en/INSTALL.md) | [TOOLS](TOOLS.md) | [USAGE](USAGE.md)

---

## 배포 상태

StockLens의 공개 설치 안내는 2026-06-01 기준으로 종료했습니다.

현재 신규 설치는 구매자 안내문을 통해 제공되는 설치 명령어와 가이드를 기준으로 진행합니다.

## 설치 스크립트가 하는 일

1. **uv 확인·설치** — 없으면 [astral.sh](https://astral.sh/uv/) 공식 인스톨러 자동 실행 (Python 런타임 포함)
2. **StockLens MCP 설치** — 격리 환경에 설치해 시스템 Python 오염을 줄임
3. **Claude Desktop config 등록** — `claude_desktop_config.json`에 절대경로로 entry 추가 (PATH 의존 없음)
4. **검증** — `stocklens-doctor` 실행, 문제 발견 시 종료

---

## Claude Desktop 재시작

**창 닫기가 아니라 완전 종료 → 재시작**.

- **Windows**: 시스템 트레이(우하단) → Claude 우클릭 → **Quit**
- **macOS**: 메뉴바 → Claude → **Quit** (또는 `Cmd + Q`)
- **Linux**: 트레이 → Quit

그 후 다시 실행.

---

## 동작 확인

Claude에서:
```
삼성전자 현재가 알려줘
```

종목명, 현재가, 전일대비, 거래량이 나오면 끝.

<img width="850" height="415" alt="result" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />

---

## 업데이트

업데이트는 구매자 안내문 기준으로 진행합니다.

---

## 진단

설치/설정에 문제가 의심되면:

```bash
stocklens-doctor
```

uv·패키지·명령·config 4단계 점검. 각 항목별 상태 + 고치는 명령어를 한 번에 표시.

---

## 트러블슈팅

### `uv: command not found` / `uv를 찾을 수 없습니다`

uv 설치 후 **새 터미널을 열어야** PATH가 반영됩니다. 같은 창에서 계속하려면:

**Windows PowerShell:**
```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
```

**macOS/Linux:**
```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

### `stocklens-setup: command not found`

`uv tool install` 후 PATH 미반영. 위와 동일하게 `~/.local/bin`을 PATH에 추가하거나, 절대경로로 호출:

**Windows:**
```powershell
& "$env:USERPROFILE\.local\bin\stocklens-setup.exe"
```

**macOS/Linux:**
```bash
~/.local/bin/stocklens-setup
```

---

### Claude Desktop에서 StockLens 도구가 안 보임

1. Claude Desktop 완전 종료 확인 (트레이 → Quit)
2. `stocklens-doctor` 실행해서 entry 유효성 확인
3. config 파일 직접 확인:

**Windows**: 파일 탐색기 주소창 → `%APPDATA%\Claude`
**macOS**: Finder → `Cmd + Shift + G` → `~/Library/Application Support/Claude`

`claude_desktop_config.json` 안에 다음과 같은 entry가 있어야 합니다:

```json
{
  "mcpServers": {
    "stocklens": {
      "command": "C:\\Users\\<사용자>\\.local\\bin\\stocklens.exe"
    }
  }
}
```

없거나 경로가 잘못됐으면:
```bash
stocklens-setup
```
다시 실행.

---

### 도구는 보이는데 호출 시 에러

네이버 증권/Yahoo Finance 접속 이슈일 수 있음:

1. 브라우저에서 https://finance.naver.com 정상 접속 확인
2. 회사/학교 방화벽 차단 여부 확인
3. Claude Desktop 재시작

---

### 기존 `naver-stock-mcp` / `pip install stocklens-mcp` 사용자

이름이 변경되었거나 설치 방식이 바뀐 경우 pip 설치를 정리:

```bash
# Windows
py -m pip uninstall naver-stock-mcp stocklens-mcp -y

# macOS/Linux
python3 -m pip uninstall naver-stock-mcp stocklens-mcp -y
```

그 후 구매자 안내문에 있는 설치 절차를 실행합니다. `stocklens-setup`이 Claude config 기존 entry를 새 절대경로로 자동 갱신합니다.

---

## 증권사 연결 (선택, 분봉 기능)

국내·미국 분봉과 상세 수급을 쓰려면 증권사 Open API 를 연결합니다.
**한국투자증권과 키움증권 중 하나만 연결하면 충분합니다.**

| | 한국투자증권 | 키움증권 |
|---|---|---|
| 국내·미국 분봉 | 지원 | 지원 |
| 상세 수급 투자자 구분 | 3종 (개인·외국인·기관계) | 13종 (기관 세부 포함) |
| 상세 수급 매수·매도 분해 | 있음 | 없음 (순매매만) |
| 사용 장소 | IP 제한 없음. 노트북처럼 장소가 바뀌면 이쪽 | 키 발급 시 등록한 IP 에서만 |
| 키 발급 | https://apiportal.koreainvestment.com | https://openapi.kiwoom.com |

한쪽이 다른 쪽의 상위집합이 아닙니다. 기관을 잘게 보고 싶으면 키움,
매수와 매도를 나눠 보고 싶으면 한국투자증권입니다.

프로그램매매·공매도·신용·대차·외국인 보유는 **아직 열지 않았습니다.**
어댑터는 만들어 두었지만 실계좌 검증을 마치지 않아 닫아 둔 상태이고,
호출하면 "확인 중"으로 응답합니다. API 키 문제가 아닙니다.

1. 위 포털에서 App Key·App Secret 발급 (시세 조회용)
2. LeetKit Manager 를 열고 StockLens 카드의 [증권사 연결] 클릭
3. 증권사 탭을 고르고 실전(기본) 선택 후 App Key·App Secret 입력,
   [연결 시험 후 저장]
4. 국내·미국 분봉 사용 가능 여부가 표시되면 완료

두 곳을 다 연결하셔도 데이터는 **주 사용 증권사 한 곳에서만** 옵니다.
장애가 나도 다른 증권사로 조용히 바뀌지 않습니다.

알아둘 것:

- 계좌번호·주문 비밀번호는 입력받지 않습니다. 시세 조회 전용 연결이며
  주문·자동매매 기능은 지원하지 않습니다
- 키는 운영체제 자격 증명 저장소에만 저장됩니다 (파일로 저장되지 않음)
- 연결 해제는 두 단계입니다
  - **현재 환경 연결 해제**: 지금 쓰는 환경(실전 또는 모의)의 키만 삭제.
    프로그램·라이선스·AI 앱 연결·다른 환경 키는 그대로
  - **그 증권사 연결을 모두 해제**: 그 증권사의 모든 키와 분봉 캐시 삭제.
    프로그램·라이선스·AI 앱 연결·네이버·Yahoo 기능은 그대로
- 프로그램만 삭제하면 증권사 연결 정보는 남습니다. 재설치하면 다시 인식됩니다.
  이 컴퓨터에서 완전히 정리하려면 Manager 삭제 화면에서 "완전히 정리"를 선택하세요
- 문제가 생기면 Manager 에서 데이터 사용 방식을 "기본 데이터 유지"로 바꾸면
  연결 전과 완전히 동일하게 동작합니다 (재설치 불필요)

---

## 그래도 안 되면

구매자 안내문에 포함된 지원 채널로 문의해 주세요.

작성 시 포함할 것:
- 운영체제 (Windows/macOS/Linux + 버전)
- `stocklens-doctor` 출력 전체
- 어떤 단계에서 실패했는지
