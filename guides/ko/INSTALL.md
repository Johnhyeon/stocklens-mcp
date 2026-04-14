# StockLens 설치 가이드

비개발자도 따라할 수 있게 상세히 정리했습니다.

[🇺🇸 English](../en/INSTALL.md) | [TOOLS](TOOLS.md) | [USAGE](USAGE.md)

---

## 준비물

1. **Python 3.11 이상**
2. **Claude Desktop 앱**
3. **인터넷 연결**

API 키, 증권 계좌는 필요 없습니다.

---

## Step 1. Python 설치

### Windows

1. https://www.python.org/downloads/ 접속
2. 노란색 **"Download Python 3.x.x"** 버튼 클릭
3. 다운로드된 설치 파일(`python-3.12.x-amd64.exe`) 실행

<img width="653" height="401" alt="python_path" src="https://github.com/user-attachments/assets/acdbe3a9-82cb-484f-b4cf-5fda4a6829c9" />

**⚠️ 중요: 설치 화면 맨 아래에 있는 체크박스를 반드시 체크**

```
☑ Add python.exe to PATH  ← 이것을 체크!
```

이 체크박스는 설치 화면 **맨 아래**, "Install Now" 버튼 바로 위에 있습니다.
체크하지 않으면 터미널에서 `python` 명령어가 동작하지 않습니다.

4. **Install Now** 클릭
5. 설치 완료 후 **Close**

### 설치 확인

**PowerShell 또는 명령 프롬프트(cmd)** 열고:

<img width="394" height="202" alt="image" src="https://github.com/user-attachments/assets/8a9a020c-8fcf-4fd2-9ad1-5413df43f311" />

```powershell
py --version
```
<img width="526" height="279" alt="image" src="https://github.com/user-attachments/assets/4e9f01bd-1146-4b66-bdce-b5e06d3952aa" />

`Python 3.12.x` 같은 버전이 나오면 성공.

에러 나오면 → **컴퓨터 재부팅 후 다시 시도**

### macOS

터미널에서:
```bash
brew install python@3.12
```

Homebrew 없으면: https://brew.sh/

### Linux

```bash
sudo apt update
sudo apt install python3 python3-pip  # Ubuntu/Debian
```

---

## Step 2. Claude Desktop 설치

<img width="458" height="644" alt="image" src="https://github.com/user-attachments/assets/5cb8847a-b1bf-4125-a2f0-d7e763234efe" />


https://claude.ai/download

본인 OS에 맞는 버전 다운로드 후 설치. Anthropic 계정 로그인 필요.

---

## Step 3. StockLens 설치

아래 **3가지 방법** 중 하나 선택. **방법 A 가장 쉬움**.

### ⭐ 방법 A: 명령어 복붙 (가장 권장)

파일 다운로드 없이 터미널에 **한 번만 복붙**하면 끝. Windows/macOS/Linux 모두 동일.

**Windows** — PowerShell 또는 명령 프롬프트(cmd) 열고:
```powershell
pip install stocklens-mcp
stocklens-setup
```

**macOS/Linux** — 터미널에서:
```bash
pip3 install stocklens-mcp
stocklens-setup
```

두 줄만 실행하면 설치 + Claude Desktop 연결까지 자동 완료.

> 💡 `pip`가 인식 안 되면 → `py -m pip install stocklens-mcp` (Windows) 또는 `python3 -m pip install stocklens-mcp` (mac/Linux) 로 대체.

### 방법 B: 파워셸 원라이너 (Windows 전용, 다운로드+실행 한 번에)

PowerShell에 복붙:
```powershell
irm https://github.com/Johnhyeon/stocklens-mcp/releases/latest/download/install.bat -OutFile "$env:TEMP\stocklens_install.bat"; & "$env:TEMP\stocklens_install.bat"
```

install.bat를 자동으로 다운로드하고 실행. `.txt`로 저장되는 문제 없음.

### 방법 C: install.bat 파일 다운로드 (시각적으로 단계 보고 싶을 때)

**Windows**:
1. **[📥 설치파일 다운로드](https://github.com/Johnhyeon/stocklens-mcp/releases/latest/download/install.bat)** 클릭 → 바로 다운로드
2. 다운로드된 `install.bat` 더블클릭
3. 창이 열리고 자동 진행 → "Installation complete!" 메시지 확인

**⚠️ `.txt` 파일로 열리는 경우 (흔함):**
브라우저가 `install.bat.txt`로 저장하는 경우가 있습니다. 해결:

- **확장자 확인:** 파일 탐색기 "보기" → "파일 확장명" 체크 → `install.bat.txt`면 **파일명 변경해서 `.txt` 삭제**
- **또는** 파일 우클릭 → **"연결 프로그램" → "Windows 명령 프로세서"** 선택
- **또는** 우클릭 → **"관리자 권한으로 실행"**

**macOS/Linux**:
```bash
curl -O https://github.com/Johnhyeon/stocklens-mcp/releases/latest/download/install.sh
chmod +x install.sh
./install.sh
```

---

## Step 4. Claude Desktop 재시작

**중요**: 창 닫기가 아니라 **완전 종료**.

- **Windows**: 시스템 트레이(화면 우하단)에서 Claude 아이콘 우클릭 → **Quit**
- **macOS**: Claude 메뉴 → **Quit** 또는 `Cmd + Q`
- **Linux**: 트레이에서 Quit

그 다음 Claude Desktop을 다시 실행.

---

## Step 5. 동작 확인

<img width="850" height="415" alt="image" src="https://github.com/user-attachments/assets/ac50dd95-85b8-4471-a79c-6aa196f62af4" />


Claude에서:
```
삼성전자 현재가 알려줘
```

<img width="797" height="948" alt="image" src="https://github.com/user-attachments/assets/1daa0535-4ab5-480c-b70f-dcfdb5c5c864" />


정상 응답:
```
종목: 삼성전자 (005930)
현재가: 206,000원
전일대비: +2,000원
거래량: 18,229,163
...
```

---

## 트러블슈팅

### "python is not recognized as an internal command"

Python이 PATH에 등록 안 됨. 해결:

1. Python 설치 파일(`python-*.exe`) 다시 실행
2. **"Modify"** 클릭
3. **"Add Python to environment variables"** 체크
4. Next → Install
5. 컴퓨터 재부팅

---

### "pip install" 시 SSL 에러

```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org stocklens-mcp
```

---

### Claude Desktop에서 StockLens 도구가 안 보임

1. Claude Desktop을 완전히 종료했는지 확인 (트레이 → Quit)
2. `stocklens-setup` 명령어가 정상 완료됐는지 확인
3. 설정 파일 직접 확인:

**Windows**: 파일 탐색기 주소창에 `%APPDATA%\Claude` 입력

**macOS**: Finder → `Cmd + Shift + G` → `~/Library/Application Support/Claude`

`claude_desktop_config.json` 파일 열어서:

```json
{
  "mcpServers": {
    "stocklens": {
      "command": "stocklens"
    }
  }
}
```

위와 같은 내용이 있어야 합니다. 없으면:
```bash
stocklens-setup
```
다시 실행.

---

### "command not found: stocklens"

Python Scripts 폴더가 PATH에 없어서 발생. 해결:

**Windows PowerShell**:
```powershell
python -m stock_mcp_server.setup_claude stocklens
```

**그 다음** `claude_desktop_config.json`의 `"command"` 값을 다음으로 변경:
```json
"command": "python",
"args": ["-m", "stock_mcp_server.server"]
```

---

### 도구는 보이는데 호출 시 에러

네이버 증권 접속 이슈일 수 있음:

1. 브라우저에서 https://finance.naver.com 정상 접속 확인
2. 회사/학교 방화벽이 차단하는지 확인
3. Claude Desktop 재시작

---

### 업데이트 방법

```bash
pip install --upgrade stocklens-mcp
```

또는 `update.bat` / `update.sh` 실행.

---

### 기존 `naver-stock-mcp` 사용자

`naver-stock-mcp`는 `stocklens-mcp`로 이름이 변경되었습니다.

```bash
# 기존 제거
pip uninstall naver-stock-mcp

# 새 버전 설치
pip install stocklens-mcp
stocklens-setup
```

Claude Desktop 재시작 후 정상 동작합니다.

---

## 그래도 안 되면

GitHub Issues에 남겨주세요:
https://github.com/Johnhyeon/stocklens-mcp/issues

작성 시 포함할 것:
- 운영체제 (Windows/macOS/Linux + 버전)
- Python 버전 (`python --version`)
- 에러 메시지 전체 (스크린샷 or 텍스트)
- 어떤 단계에서 실패했는지
