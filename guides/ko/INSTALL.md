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

## 그래도 안 되면

구매자 안내문에 포함된 지원 채널로 문의해 주세요.

작성 시 포함할 것:
- 운영체제 (Windows/macOS/Linux + 버전)
- `stocklens-doctor` 출력 전체
- 어떤 단계에서 실패했는지
