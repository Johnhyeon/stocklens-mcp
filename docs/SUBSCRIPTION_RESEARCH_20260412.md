# MCP/API 구독 비즈니스 모델 리서치

> 조사일: 2026-04-12
> 대상: naver-stock-mcp Pro 유료화 전략
> 요청자: 한국 1인 크리에이터 (Python 패키지 + 로컬 실행 구조)

---

## TL;DR (1분 버전)

**결론 한 줄**: **Lemon Squeezy(결제+라이센스) + Keygen 또는 LS 자체 License API(검증) + PyPI 공개 배포 + 코드 내 온라인 검증** 조합이 한국 1인 개발자에게 가장 현실적이다. 한국 사업자 등록 없이 즉시 시작 가능하고, MoR(Merchant of Record)이 부가세/환불/국제결제를 다 처리해준다. 내수 비중이 크면 **Latpeed(빠른 런칭)** 를 병행한다.

**3가지 추천 옵션 요약**:

| 옵션 | 결제 | 라이센스 | 적합 단계 | 월 5% 수수료 | 난이도 |
|------|------|---------|----------|-------------|-------|
| **A. Lemon Squeezy 올인원** | LS | LS License API | 0 → 500만원/월 | 5% + $0.5 + 국제 1.5% | ⭐⭐ |
| **B. Latpeed + JWT 자체 검증** | Latpeed | 자체 JWT 서명 키 | 내수 집중, 즉시 | 1.6~4.6% | ⭐⭐⭐ |
| **C. Gumroad + Keygen** | Gumroad | Keygen 무료티어 | 사이드 프로젝트, 저매출 | 10% + Keygen 무료 | ⭐⭐⭐ |

**핵심 원리 3개**:
1. **MCP 자체에는 결제 레이어가 없다**. 결제/키발급은 전부 웹에서, MCP는 `env`로 키만 받는 게 표준.
2. **로컬 실행 Python 패키지도 서버 1개(또는 SaaS)만 있으면 유료화 가능**. 검증용 경량 엔드포인트만 띄우면 됨.
3. **한국 1인 사업자 기준 Stripe 직접은 어렵고, MoR 플랫폼(Lemon Squeezy/Paddle/Gumroad)이 현실적**. 내수만 노린다면 Latpeed/토스페이먼츠.

---

## 1. 해외 유료 API/MCP 서비스의 구독 관리 패턴

### 1.1 Alpha Vantage ($49.99 ~ $249.99/월)

- **결제**: 자체 사이트 결제 (Stripe 추정, 공개되지 않음). RapidAPI를 통한 재판매 채널도 있음
- **API 키 발급**: 이메일 등록 후 즉시 발급. 프리미엄 가입 시 기존 키가 프리미엄으로 업그레이드
- **사용량 제한**: 분당 요청수(75 → 300 → 600 → 1200 req/min) 차등. 일일 제한은 프리미엄에서 제거됨
- **기간 만료 시**: 키는 살아 있되 무료 티어(25 req/day)로 강등
- **보안 가이드**: 환경변수 저장 권장 — `os.environ["ALPHA_VANTAGE_API_KEY"]`
- **교훈**: "키는 영구, 권한만 갱신" 패턴. 사용자 경험 매우 부드러움 (재설치 불필요)

출처:
- [Alpha Vantage Premium](https://www.alphavantage.co/premium/)
- [Alpha Vantage API Request Limits - Macroption](https://www.macroption.com/alpha-vantage-api-limits/)
- [AlphaLog 2026 Guide](https://alphalog.ai/blog/alphavantage-api-complete-guide)

### 1.2 Polygon.io (Massive.com, $29 ~ $199/월)

- **2025.10.30 Massive.com으로 리브랜딩**. 기존 API 키/통합은 그대로 작동
- **결제**: 자체 체크아웃 (Stripe 백엔드)
- **티어 예시**: Stocks Developer $79/월 (과거 10년 데이터), 프리미엄은 분당 제한 없음
- **제한 방식**: 무료는 분당 5회 / 프리미엄은 무제한, 대신 티어별로 접근 가능한 엔드포인트(실시간/과거범위/옵션 등)가 차등
- **키 관리**: 대시보드에서 키 생성/폐기/복수 키 생성 가능. 계정 단위로 구독 관리
- **교훈**: "Feature-gating"이 "rate-gating"보다 강한 차별화 수단. 무료는 기능 제한, 유료는 무제한

출처:
- [Polygon.io Pricing](https://polygon.io/pricing)
- [Polygon Rate Limit KB](https://polygon.io/knowledge-base/article/what-is-the-request-limit-for-polygons-restful-apis)

### 1.3 Finnhub ($11.99 ~ $99.99/월)

- **결제**: 자체 사이트, 월/연 구독
- **무료**: 60 req/min, 실시간 US 주가, 기본 재무, SEC 공시, WebSocket 50 심볼
- **유료**: 해외 주식, 상세 재무, 대체 데이터(감정/뉴스/ESG), 분당 제한 상향
- **키 발급**: 가입 즉시. 월 결제로 그 키가 유료 권한 획득
- **교훈**: 가격대 $11.99부터 시작 → 진입장벽 낮춤. "무료도 충분히 쓸 만함 + 유료는 명확히 다른 데이터셋"이 핵심

출처:
- [Finnhub Pricing](https://finnhub.io/pricing)
- [Finnhub Rate Limit Docs](https://finnhub.io/docs/api/rate-limit)

### 1.4 MarketXLS ($29.99/월)

- **제품 형태**: Excel Add-in — 완전히 로컬 실행, 하지만 구독 필수
- **결제**: 자체 사이트 월/연 구독
- **라이센스 검증**: Excel 실행 시 Settings → Apply License → 키 입력. **실행할 때마다 MarketXLS 서버에 로그인 검증**
- **고급 플랜**: Quotemedia username/password 별도 제공 (데이터 소스 직접 제공)
- **문제 발생 시**: 지원팀에 연락해 키 재발급
- **교훈**: 로컬 앱 + 서버 검증 하이브리드. 검증 서버가 죽으면 앱도 죽음 → 그레이스 기간 구현 필수

출처:
- [MarketXLS FAQ](https://marketxls.com/docs/faq)
- [MarketXLS Installation Guide](https://docs.marketxls.com/marketxls-tutorials/getting-started/installing-marketxls)

### 1.5 Claude Skills 360 ($39 1회)

- **1회 결제 번들**: 2,350+ 스킬, 평생 업데이트 포함이라 주장
- **검증**: 번들 다운로드 링크 이메일 발송 방식 추정 (명확한 DRM 없음)
- **교훈**: "스킬/프롬프트 번들"은 1회 결제가 자연스러움. 월 구독으로 포장하기 어려움 → 이 모델은 유료 강의/전자책에 가까움

### 1.6 TradeStation MCP (계좌 연동형)

- **구독이 아니라 증권 계좌 연동**. TradeStation 계좌가 있어야 실행 가능
- **결제 없음**: 증권사 수수료에서 수익 발생
- **교훈**: 우리에게는 적용 불가 모델 (증권사 라이선스 필요)

### 1.7 Anthropic 공식 MCP 마켓플레이스

- **현재까지 대부분 무료/오픈소스**. Anthropic은 MCP 마켓플레이스에 과금 레이어를 공식 제공하지 않음
- **정책 변화**: 2025~2026년 Anthropic은 third-party 구독 통합을 차단하고 공식 API/integration 경로로 유도
- **유료 MCP = 바깥에서 결제받고 키를 `env`로 전달하는 구조가 사실상 표준**

출처:
- [Claude Marketplaces](https://claudemarketplaces.com/mcp)
- [MCP Anthropic Partners](https://www.anthropic.com/partners/mcp)
- [Anthropic blocks third-party tools](https://decodethefuture.org/en/anthropic-blocks-third-party-tools/)

### 패턴 요약

| 항목 | 표준 패턴 |
|------|----------|
| 결제 | 자체 웹사이트 (Stripe 백엔드) 또는 RapidAPI |
| 키 발급 | 계정 생성 즉시 자동 발급, 구독 상태만 변동 |
| 만료 처리 | 키 유지 + 무료 티어로 강등 (재설치 없음) |
| 사용량 제한 | 분당 요청수 + 엔드포인트 접근 권한 혼합 |
| 환불 | 보통 월 기준 비례 환불 또는 7일 이내 전액 |
| 무료체험 | 무료 티어로 대체하는 경우가 더 많음 (신용카드 없는 무료 플랜) |
| 검증 방식 | 요청마다 서버 측 검증 (API 호출 기반이라 자연스러움) |

**우리 상황의 차이점**: API 기반이 아니라 **Python 패키지 + 로컬 실행**. 데이터를 서버에서 뱉어주는 구조가 아님 → 따라서 "API 키 검증"이 아니라 **"로컬 패키지 안에서 라이센스 체크"** 가 필요.

---

## 2. 로컬 데스크톱 소프트웨어의 라이센스 관리 패턴

### 2.1 JetBrains IDE (월 구독)

- **라이센스 서버**: JetBrains Account 기반. 처음 등록 시 인터넷 필요
- **오프라인 허용**: 인터넷 없이 최대 **48시간** 사용 가능 (IDE 재시작만 안 하면)
- **48시간 후**: 재연결 필수. 재시작 시 즉시 연결 필요
- **오프라인 액티베이션 코드**: 상용 라이센스 구매자는 계정에서 코드 다운로드 가능. 비상용/교육용은 이 옵션 없음
- **환경변수**: `JETBRAINS_LICENSE_SERVER`로 사내 라이센스 서버 지정 가능 (엔터프라이즈용)
- **교훈**: **그레이스 기간(48시간)이 핵심 UX**. "오프라인 상태여도 사용자가 갑자기 튕기면 안 된다"

출처:
- [JetBrains License Vault](https://www.jetbrains.com/help/license-vault-cloud/Activating_a_license.html)
- [JetBrains Working Offline](https://www.jetbrains.com/help/rider/Working_Offline.html)
- [ReSharper Offline Activation](https://resharper-support.jetbrains.com/hc/en-us/articles/207327790-How-to-execute-an-offline-activation)

### 2.2 1Password (월 구독)

- **서버 의존**: 볼트가 1Password 클라우드에 암호화 저장 → 로그인 세션 기반
- **오프라인**: 세션 유지 중에는 볼트 접근 가능. 단, 새 로그인/동기화는 온라인 필요
- **교훈**: "데이터 자체가 클라우드"라는 특수성이 있음. 우리 케이스와는 달라서 직접 참고는 어려움

출처:
- [1Password Membership](https://support.1password.com/explore/membership/)

### 2.3 Sublime Text (1회 결제)

- **라이센스 파일**: `License.sublime_license` 파일을 해당 경로에 저장
- **서버 검증**: 실행 시 `license.sublimehq.com`에 쿼리해 상태 확인
- **최근 정책**: 3년마다 업데이트 접근권 갱신 (1회 결제 + 유지보수 구독 하이브리드)
- **교훈**: **파일 기반 라이센스 + 실행 시 서버 검증** — 단순하고 효과적

출처:
- [Sublime Text Portable License Keys](https://www.sublimetext.com/docs/portable_license_keys.html)

### 2.4 라이센스 키 형태 비교

| 형태 | 예시 | 장점 | 단점 |
|------|------|------|------|
| 단순 문자열 | `NAVR-STCK-XXXX` | 구현 간단 | 서버 검증 필수 |
| 서명된 JWT | `eyJhbGc...` (payload: user_id, expiry, tier) | 오프라인 검증 가능 (공개키만으로) | 취소 어려움 → CRL/revocation list 필요 |
| 암호화된 토큰 (Fernet) | 바이너리 파일 | 탈취 시 복호화 어려움 | 복잡도 증가 |
| 하드웨어 지문 바인딩 | MAC/디스크 ID 기반 | 다중 기기 방지 | PC 교체 시 UX 나쁨 |

**권장**: **JWT + 주기적 온라인 검증 + 7일 그레이스** 가 로컬 Python 패키지에 가장 잘 맞음.

---

## 3. Python 패키지 유료화 패턴

### 3.1 가능한 구조 3가지

#### A) 공개 PyPI + 라이센스 게이팅 (추천)
```python
# naver-stock-mcp-pro 패키지는 PyPI에 공개
# 설치는 누구나 가능: pip install naver-stock-mcp-pro
# 단, 실행 시 유효한 라이센스 키 없으면 기능 잠김
```
- **장점**: 배포 인프라 무료 (PyPI), 설치 UX 간단
- **단점**: 코드가 노출됨 → 고급 사용자는 우회 가능 (라이센스 체크 제거)
- **대응**: 핵심 로직은 난독화(pyarmor) + 핵심 데이터는 서버에서 받아오게

#### B) Private PyPI / Git (키 기반 접근)
```python
pip install --index-url https://pypi.naver-stock.io/<license_key>/simple naver-stock-mcp-pro
```
- **Keygen.sh 예시**: 라이센스 키를 URL 일부로 사용해 private PyPI repository 접근
- **장점**: 미결제 사용자는 아예 다운로드조차 못함
- **단점**: Claude Desktop 설정 파일에 URL 적어야 해서 UX 불편

#### C) 바이너리 번들 (PyInstaller/Nuitka)
- Python 코드를 단일 exe로 패키징
- **장점**: 코드 은닉
- **단점**: MCP는 `stdio` 기반이라 `uv`/`pip` 설치가 오히려 자연스러움. 바이너리화 시 Claude Desktop 설정 패턴이 깨짐

**권장**: **A안 (공개 PyPI + 런타임 라이센스 체크)**. MCP 생태계 관행과 맞고, 애초에 경쟁이 없는 상황이라 "우회 가능성"은 무시할 수 있는 리스크.

### 3.2 라이센스 체크 코드 패턴 (의사코드)

```python
# naver_stock_mcp_pro/license.py
import os, time, httpx, jwt
from pathlib import Path

LICENSE_CACHE = Path.home() / ".naver-stock-mcp" / "license.json"
PUBLIC_KEY = "..."  # JWT 검증용 공개키
VERIFY_URL = "https://api.naver-stock.io/v1/license/verify"
GRACE_DAYS = 7

def verify_license():
    key = os.environ.get("NAVER_STOCK_PRO_KEY")
    if not key:
        raise RuntimeError("Pro 라이센스 키가 설정되지 않았습니다.")

    # 1) 캐시된 JWT 확인
    cached = load_cached_jwt(key)
    if cached and not expired(cached):
        return cached

    # 2) 서버 검증 시도
    try:
        r = httpx.post(VERIFY_URL, json={"key": key}, timeout=5)
        jwt_token = r.json()["token"]
        save_cached_jwt(key, jwt_token)
        return jwt.decode(jwt_token, PUBLIC_KEY, algorithms=["RS256"])
    except Exception:
        # 3) 오프라인 그레이스
        if cached and days_since(cached["verified_at"]) < GRACE_DAYS:
            return cached
        raise RuntimeError("라이센스 검증 실패. 7일 이상 오프라인 상태.")
```

이 구조면:
- **키는 환경변수 (MCP 표준)** → `env` 필드에 넣으면 됨
- **JWT 캐싱** → 매 실행마다 서버 안 때림
- **7일 그레이스** → 출장/오프라인 사용자 UX 보호
- **서버는 경량 검증 엔드포인트 1개만** → Vercel/Fly.io 무료 티어로 충분

### 3.3 "pykrx-pro" 같은 사례?

- 조사 결과 **한국 금융 데이터 Python 패키지의 유료 상용 버전은 사실상 존재하지 않음**. 대부분 MIT/Apache 오픈소스
- `ta-lib` 상용은 C 라이브러리 라이센스이지 Python 패키지 유료화 패턴은 아님
- **→ 이 영역은 비어 있음**. naver-stock-mcp Pro가 레퍼런스 사례가 될 여지

출처:
- [Keygen for Python Packages](https://keygen.sh/for-python-packages/)
- [example-python-license-validation](https://github.com/keygen-sh/example-python-license-validation)

---

## 4. 한국 1인 크리에이터용 결제 인프라

### 4.1 옵션별 비교 (2026년 기준)

| 플랫폼 | 유형 | 수수료 | 월 구독 지원 | 한국 1인 접근성 | 국제 결제 |
|--------|------|--------|------------|----------------|----------|
| **Latpeed (래피드)** | 국내 MoR형 | 무료 4.6% / Pro(월 24k) 1.6% | ✅ | ⭐⭐⭐⭐⭐ 개인 가능 | ❌ (원화 중심) |
| **Lemon Squeezy** | 해외 MoR | 5% + $0.5, 국제 +1.5% | ✅ | ⭐⭐⭐⭐ (해외 은행송금, 문서 필요) | ✅ (135+개국) |
| **Paddle** | 해외 MoR | 5% + $0.5 | ✅ | ⭐⭐⭐ (SaaS 창업자 대상, 심사 있음) | ✅ |
| **Gumroad** | 해외 마켓 | 10% flat | ✅ | ⭐⭐⭐⭐ (개인 바로 가입) | ✅ |
| **Stripe 직접** | PG | 2.9% + $0.3 | ✅ | ❌ **한국 직접 계정 불가 (2026)** | — |
| **토스페이먼츠** | 국내 PG | 2~3% (빌링 별도 계약) | ✅ (빌링 API) | ⭐⭐ (사업자등록 필수) | ❌ |
| **KG이니시스** | 국내 PG | 2.5~3.5% | ✅ (자동결제) | ⭐⭐ (사업자등록 필수) | ❌ |
| **Patreon** | 후원 | 8~12% | ✅ | ⭐⭐⭐ (한국 개인 가능) | ✅ |

### 4.2 매출 규모별 실효 수수료 시뮬레이션

**월 14,900원 Pro, 100 구독자 = 149만원/월**

| 플랫폼 | 수수료 | 월 수수료 | 실수령 |
|--------|-------|----------|-------|
| Latpeed 무료 | 4.6% | 68,540원 | 1,421,460원 |
| Latpeed Pro(월 24k) | 1.6% + 24,000 | 47,840원 | 1,442,160원 |
| Lemon Squeezy (내수) | 5% + 50¢ × 100 = ~5% + $50 | 약 143,000원 | 1,347,000원 |
| Lemon Squeezy (해외 1.5% 추가) | 6.5% + $50 | 약 165,000원 | 1,325,000원 |
| Gumroad | 10% | 149,000원 | 1,341,000원 |

**손익분기(Latpeed Pro)**: 월 매출 **약 80만원** 이상이면 Pro 플랜이 무료 플랜보다 이득.

### 4.3 Stripe는 왜 한국 개인이 못 쓰나

- Stripe는 2026년 현재 **South Korea에 직접 계정 개설을 지원하지 않음** (체크아웃/지불 처리 기능은 있지만, 판매자 가입은 불가)
- 한국에서 Stripe를 쓰려면: 미국/싱가포르 법인 + 해당 국가 은행계좌 필수 (doola 등 대행 서비스 이용)
- **→ 한국 1인 개발자에게 Stripe 직접은 현실성 없음**. Lemon Squeezy/Paddle 같은 MoR 계층이 유일한 해외 결제 루트

### 4.4 Lemon Squeezy: 한국 1인 개발자에게 가장 균형점

**장점**:
- **MoR**: 부가세/EU VAT/미국 sales tax 전부 LS가 처리 → 한국 사업자 등록 불필요 (소득만 종합소득세로 신고)
- **라이센스 키 발급 내장** — 결제 완료 시 자동 발급, Webhook/License API로 검증
- **구독 관리 일체형**: 플랜 업그레이드/다운그레이드/환불/grace period 내장
- **한국 셀러 지급**: Bank payout 지원 확장 (2025 기준 79개국), 한국 수취 가능
- **Python SDK** 제공

**단점**:
- 5% + $0.50 기본 수수료 + 국제 1.5% → 실효 약 6.5% (10만원 결제당 6,500원)
- UI/고객 대응 영어 (한국 사용자에겐 어색할 수 있음)
- 환불/세금계산서 한국 관행과 다름

**사용 방식**:
```
사용자 → LS 체크아웃 → 카드 결제 → Webhook 발사
  ↓
내 Vercel/Cloudflare 함수 → 라이센스 키 생성 → LS License API에 등록
  ↓
사용자에게 이메일로 키 전달 (LS가 자동 발송)
  ↓
사용자가 MCP `env`에 키 입력 → 실행 시 LS License API `/v1/licenses/validate` 호출
```

출처:
- [Lemon Squeezy Fees](https://docs.lemonsqueezy.com/help/getting-started/fees)
- [LS License API](https://docs.lemonsqueezy.com/api/license-api)
- [LS License Keys and Subscriptions](https://docs.lemonsqueezy.com/help/licensing/license-keys-subscriptions)
- [LS Bank Payouts Expansion](https://www.lemonsqueezy.com/blog/new-bank-payouts)

### 4.5 Latpeed: 내수 집중 + 즉시 런칭

**장점**:
- 한국인, 한글, 원화 → **UX가 월등히 친숙**
- 개인 즉시 가입, 사업자등록증 없이도 시작 가능 (세금계산서 별도 처리)
- 디스커버리 채널(크리에이터 피드) 있어서 자연 유입 가능
- 콘텐츠 판매 + 구독 둘 다 지원
- 수수료 낮음 (Pro 플랜 1.6%)

**단점**:
- **월 구독이 콘텐츠 구독 모델 중심** — API/라이센스 키 연동은 자체 구현 필요 (Webhook API는 있음)
- 해외 결제 불가
- 라이센스 관리는 **100% 셀러가 직접**해야 함 → 별도 라이센스 서버 필요

**사용 방식**:
```
Latpeed 상품 등록 → 카드/계좌 결제 → Latpeed Webhook
  ↓
내 FastAPI 서버 → 결제 ID 검증 → JWT 라이센스 발급
  ↓
이메일로 키 전달 → MCP env에 입력
```

**Latpeed vs Lemon Squeezy 결정 기준**:
- 한국 사용자 90% 이상 → Latpeed
- 해외 사용자도 타겟 → Lemon Squeezy
- 사업자 등록 귀찮음 + 즉시 시작 → 둘 다 가능
- 글로벌 SaaS 확장 계획 → Lemon Squeezy

출처:
- [Latpeed](https://www.latpeed.com/)
- [Payple 래피드 케이스](https://team.payple.kr/customer-story/latpeed)

### 4.6 토스페이먼츠 / KG이니시스

- **둘 다 사업자등록번호 필수** (개인사업자도 가능하지만 사업자등록증 필요)
- **빌링(정기결제) API**: 별도 계약 + 심사 통과 필요
- **적용 시점**: 월 매출 500만원 이상 꾸준히 찍고 "사업화" 결정 후가 적절
- **장점**: 한국 사용자 UX 최고 (네이버페이/카카오페이/삼성페이 다 붙음), 수수료 가장 낮음
- **단점**: 초기 진입장벽 (사업자등록+계약+심사 2~4주)

출처:
- [토스페이먼츠 자동결제 가이드](https://docs.tosspayments.com/guides/v2/billing)

---

## 5. MCP 특화 라이센스 관리 패턴

### 5.1 MCP 프로토콜의 인증 표준

- **MCP 프로토콜 자체는 결제/라이센스를 규정하지 않음**. 인증은 서버 재량
- **표준 관행 (2026)**:
  - **로컬 stdio 서버**: `env` 필드로 API 키 전달
  - **리모트 HTTP 서버**: 2025년 3월부터 **OAuth 2.1** 공식 채택
- **환경변수 명명 관례**: `<SERVICE>_API_KEY` 패턴이 업계 표준
  - Exa MCP: `EXA_API_KEY`
  - Scalekit MCP: `EXTERNAL_API_KEY`
  - Atlassian Rovo MCP: API 토큰 방식
- **Claude Desktop 설정 예시**:
  ```json
  {
    "mcpServers": {
      "naver-stock-pro": {
        "command": "naver-stock-mcp-pro",
        "env": {
          "NAVER_STOCK_PRO_KEY": "nsm_pro_ABCD1234..."
        }
      }
    }
  }
  ```

### 5.2 유료 MCP 사례

- **사실상 거의 없음** (2026년 4월 기준). MCP 마켓플레이스의 주류는 여전히 무료/오픈소스
- **상용 MCP는 주로 기존 유료 API의 MCP 래퍼**: Atlassian Rovo MCP (Jira/Confluence), Stripe MCP (Stripe 계정 필요), Polygon MCP (Polygon API 키 필요)
- **→ 우리 경쟁 포지션은 여전히 비어 있음**. "유료 MCP 패키지"라는 카테고리의 선두 주자 여지

### 5.3 Anthropic 공식 가이드

- MCP 인증은 OAuth 2.1 권장 (리모트 서버)
- 로컬 stdio 서버의 키 관리는 **명시적 가이드 없음** → `env` 파라미터 활용이 관행
- 온라인 검증에 대한 Anthropic 공식 입장 없음 → 셀러 재량

출처:
- [MCP Authentication in Cursor 2026](https://www.truefoundry.com/blog/mcp-authentication-in-cursor-oauth-api-keys-and-secure-configuration)
- [MCP API Key Best Practices - Stainless](https://www.stainless.com/mcp/mcp-server-api-key-management-best-practices)
- [WorkOS MCP Authentication](https://workos.com/blog/introduction-to-mcp-authentication)

---

## 6. 우리 상황에 추천하는 3가지 구체적 옵션

### 옵션 A: Lemon Squeezy 올인원 (⭐ 최우선 추천)

**구성**:
- 결제/구독: Lemon Squeezy
- 라이센스 발급/검증: LS License API (내장)
- 배포: PyPI 공개 + 런타임 검증
- 검증 서버: **불필요** (LS API 직접 호출)

**구현 난이도**: ⭐⭐ (2~3일)

**초기 비용**:
- LS 수수료: 5% + $0.50 (국내), +1.5% (해외)
- 인프라: $0 (검증 서버 불필요)
- 개발: 라이센스 체크 코드 ~200줄

**월 14,900원 × 100 구독 가정 실수령**:
- 약 1,325,000원/월 (해외 포함 시)
- 약 1,347,000원/월 (국내만)

**장점**:
- 한국 1인이 법인/사업자등록 없이 즉시 시작 가능
- 부가세/환불/국제 카드 전부 LS가 처리 (MoR)
- 라이센스 로직을 직접 안 만들어도 됨 (LS가 제공)
- 해외 사용자로 확장 쉬움

**단점**:
- 실효 수수료 6~6.5% (국내 PG 대비 2~3배)
- 한글 UX 없음
- 한국 사용자에게 "해외 사이트 결제"라는 심리적 장벽

**실행 순서**:
1. LS 가입 → 상품 등록 (월 구독 + 라이센스 키 옵션 활성화)
2. Pro 패키지에 라이센스 검증 코드 삽입 (LS License API `/v1/licenses/validate` 호출)
3. 체크아웃 URL을 판매 페이지(Carrd/Notion)에 링크
4. Webhook으로 라이센스 활성/취소 이벤트 수신 (선택)

### 옵션 B: Latpeed + JWT 자체 검증 (내수 집중)

**구성**:
- 결제/구독: Latpeed
- 라이센스 발급/검증: 자체 FastAPI 서버 (JWT 발급)
- 배포: PyPI 공개 + 런타임 검증
- 검증 서버: Vercel/Fly.io 무료 티어 FastAPI

**구현 난이도**: ⭐⭐⭐ (5~7일)

**초기 비용**:
- Latpeed 수수료: 4.6% (무료) / 1.6% + 월 24,000원 (Pro)
- 인프라: $0 (Vercel 무료 티어)
- 개발: 결제 Webhook 수신 + JWT 발급 + 검증 엔드포인트

**월 14,900원 × 100 구독 가정 실수령**:
- Latpeed 무료 플랜: 약 1,421,000원/월
- Latpeed Pro 플랜: 약 1,418,000원/월 (매출 100만원 넘으면 Pro가 유리)

**장점**:
- 수수료 가장 낮음 (실효 1.6~4.6%)
- 한국 사용자 UX 최고 (한글, 원화, 카카오페이)
- Latpeed 피드로 추가 노출 가능

**단점**:
- 해외 결제 불가 → 글로벌 확장 시 2중 구조 필요
- 라이센스 서버 직접 구축 필요 (LS보다 개발량 많음)
- Webhook 시그니처 검증, JWT 서명, 키 취소 처리 전부 직접

**실행 순서**:
1. Latpeed 가입 → 월 구독 상품 등록
2. 검증 FastAPI 서버 구축 (Vercel/Fly.io)
3. Latpeed Webhook → 결제 확인 → JWT 발급 → 이메일 발송
4. Pro 패키지에 검증 엔드포인트 호출 코드 삽입

### 옵션 C: Gumroad + Keygen.sh 무료 티어 (사이드 프로젝트)

**구성**:
- 결제/구독: Gumroad (월 구독 지원)
- 라이센스 발급/검증: Keygen.sh (무료 Dev 티어: 100 ALU)
- 배포: PyPI 공개

**구현 난이도**: ⭐⭐⭐ (4~5일)

**초기 비용**:
- Gumroad 수수료: 10% flat
- Keygen: 무료 (100명까지) → 유료 전환 시 월 $25~
- 개발: Zapier로 Gumroad → Keygen 연결

**장점**:
- Keygen이 라이센스 로직 전부 제공 (Python 예제 공개)
- 개인 즉시 가입
- 사이드 프로젝트/MVP에 이상적

**단점**:
- Gumroad 10% 수수료 (Lemon Squeezy보다 비쌈)
- Keygen 100명 넘으면 월 $25 추가
- 두 플랫폼 연동 작업 필요

### 옵션 비교 매트릭스

| 기준 | 옵션 A (LS) | 옵션 B (Latpeed) | 옵션 C (Gumroad+Keygen) |
|------|-----------|-----------------|------------------------|
| 초기 셋업 시간 | 2~3일 | 5~7일 | 4~5일 |
| 월 인프라 비용 | $0 | $0 (Vercel 무료) | $0 → $25 |
| 실효 수수료 | 5~6.5% | 1.6~4.6% | 10% |
| 해외 결제 | ✅ | ❌ | ✅ |
| 한국 UX | 중 | 상 | 중 |
| 확장성 (글로벌) | 상 | 하 | 중 |
| 개발 복잡도 | 낮음 | 중 | 중 |
| 매출 500만원/월 시 최적 | ✅ | △ | ❌ |
| 매출 50만원/월 시 최적 | ✅ | ✅ | △ |

---

## 7. 우리 상황 전용 구체 권장

### 단계별 로드맵

**Phase 1 (지금 ~ 1개월): 옵션 A로 런칭**
1. Lemon Squeezy 가입 + Pro 상품 등록 (월 14,900원)
2. LS License API 연동 코드 작성 (~200줄)
3. Pro 패키지 PyPI 배포
4. 판매 페이지: 기존 구글폼 → Notion/Carrd + LS 체크아웃 버튼

**Phase 2 (2~3개월): 내수 10명 이상 확보 후 Latpeed 추가**
- Latpeed를 한국 사용자용 2차 채널로 추가
- 라이센스 키는 동일한 검증 서버에서 발급 (결제 출처만 다름)
- 한국 사용자에게는 Latpeed 링크, 해외는 LS 링크 제시

**Phase 3 (6개월+): 매출 500만원 이상 시 토스페이먼츠 검토**
- 이 시점에 개인사업자 등록 + 토스페이먼츠 자동결제 계약
- 수수료 2%대로 내림 → 월 10만원 이상 절감

### 핵심 의사결정

1. **월 구독 가격 14,900원은 적절한가?**
   - 해외 벤치마크 대비 1/3~1/2 수준 (Finnhub $11.99 = 약 16,000원, MarketXLS $29.99 = 약 42,000원)
   - 한국 시장 고려 시 적정. 연간 149,000원으로 2개월 할인 제공 권장

2. **무료 체험 vs 무료 티어?**
   - **무료 티어 강추**. 14일 체험보다 "영구 무료 버전 + 제한 기능" 구조가 유리
   - 이미 구조가 그렇게 잡혀 있음 (500개 제한, 스크리닝 없음)

3. **라이센스 키 탈취 대응**
   - 실사용자 기준 100명 단위 규모에서는 사실상 무시해도 됨
   - 필요 시 LS License API의 `activation limit`(최대 기기 수) 옵션 활용

---

## 한 줄 결론

> **한국 1인 크리에이터가 Python 패키지(MCP 포함)를 유료화할 때 최적 경로는 "Lemon Squeezy(MoR 결제+내장 라이센스 API) + PyPI 공개 배포 + 런타임 JWT 검증"이며, 한국 내수 비중이 70% 이상이면 Latpeed + 자체 JWT 검증 서버(Vercel 무료 티어) 조합을 병행한다. 토스페이먼츠는 매출 500만원/월 이상 + 사업자등록 이후 단계에서 합류시킨다.**

---

## 출처 모음

### API/MCP 서비스
- [Alpha Vantage Premium](https://www.alphavantage.co/premium/)
- [Alpha Vantage API Limits](https://www.macroption.com/alpha-vantage-api-limits/)
- [Polygon.io Pricing](https://polygon.io/pricing)
- [Polygon Rate Limit](https://polygon.io/knowledge-base/article/what-is-the-request-limit-for-polygons-restful-apis)
- [Finnhub Pricing](https://finnhub.io/pricing)
- [MarketXLS FAQ](https://marketxls.com/docs/faq)
- [MarketXLS Install Guide](https://docs.marketxls.com/marketxls-tutorials/getting-started/installing-marketxls)
- [Claude Marketplaces](https://claudemarketplaces.com/mcp)
- [Anthropic MCP Partners](https://www.anthropic.com/partners/mcp)

### 로컬 앱 라이센스
- [JetBrains License Vault](https://www.jetbrains.com/help/license-vault-cloud/Activating_a_license.html)
- [JetBrains Working Offline](https://www.jetbrains.com/help/rider/Working_Offline.html)
- [ReSharper Offline Activation](https://resharper-support.jetbrains.com/hc/en-us/articles/207327790-How-to-execute-an-offline-activation)
- [Sublime Text Portable License](https://www.sublimetext.com/docs/portable_license_keys.html)
- [1Password Membership](https://support.1password.com/explore/membership/)

### 결제 플랫폼
- [Lemon Squeezy Fees](https://docs.lemonsqueezy.com/help/getting-started/fees)
- [Lemon Squeezy License API](https://docs.lemonsqueezy.com/api/license-api)
- [LS License Keys + Subscriptions](https://docs.lemonsqueezy.com/help/licensing/license-keys-subscriptions)
- [LS Bank Payouts 45 Countries](https://www.lemonsqueezy.com/blog/new-bank-payouts)
- [Gumroad License Keys](https://gumroad.com/help/article/76-license-keys)
- [Paddle MoR Overview](https://www.paddle.com/paddle-101)
- [Paddle Supported Countries](https://developer.paddle.com/concepts/sell/supported-countries-locales)
- [Stripe Korea Payment Methods](https://docs.stripe.com/payments/countries/korea)
- [Stripe Korea Subscriptions](https://docs.stripe.com/billing/subscriptions/kr-card)
- [How to Open Stripe Account in Korea](https://www.doola.com/stripe-guide/how-to-open-a-stripe-account-in-south-korea/)
- [토스페이먼츠 자동결제](https://docs.tosspayments.com/guides/v2/billing)
- [토스페이먼츠 수수료](https://www.tosspayments.com/about/fee)
- [Latpeed](https://www.latpeed.com/)
- [Payple Latpeed 케이스](https://team.payple.kr/customer-story/latpeed)

### 라이센스 관리
- [Keygen.sh for Python Packages](https://keygen.sh/for-python-packages/)
- [Keygen Python License Validation Example](https://github.com/keygen-sh/example-python-license-validation)
- [Keygen Python Machine Activation Example](https://github.com/keygen-sh/example-python-machine-activation)
- [Keygen + Gumroad Integration](https://keygen.sh/integrate/gumroad/)
- [Keygen + Stripe Integration](https://keygen.sh/integrate/stripe/)

### MCP 인증 패턴
- [MCP Authentication in Cursor (2026)](https://www.truefoundry.com/blog/mcp-authentication-in-cursor-oauth-api-keys-and-secure-configuration)
- [MCP API Key Best Practices - Stainless](https://www.stainless.com/mcp/mcp-server-api-key-management-best-practices)
- [WorkOS MCP Auth Intro](https://workos.com/blog/introduction-to-mcp-authentication)
- [MCP Framework Auth Docs](https://mcp-framework.com/docs/Authentication/overview/)
