"""네이버 증권에서 주식 데이터를 수집하는 모듈.

HTTP 요청은 _http.get_client()의 싱글톤 AsyncClient를 통해 keep-alive로 재사용한다.
결과는 _cache.cached() 데코레이터로 TTL 캐싱 (장중/장마감 차등).
"""

import asyncio
import re

from bs4 import BeautifulSoup

from stock_mcp_server._http import fetch
from stock_mcp_server._cache import cached

BASE_URL = "https://finance.naver.com"
FCHART_URL = "https://fchart.stock.naver.com/siseJson.nhn"

# div.description 안의 em 중 '종목 상태 마커가 아닌' 것들.
# date=기준일, realtime=실시간 배지, summary=기업개요 본문.
_NON_STATUS_EM_CLASSES = {"date", "realtime", "summary"}

# 시장경보 목록 페이지. type은 실제 탭 링크에서 확인한 값만 쓴다
# (trading_halt 같은 추측 값은 기본 페이지를 돌려주므로 넣지 않는다).
_ALERT_TYPES = {
    "caution": "투자주의",
    "warning": "투자경고",
    "risk": "투자위험",
}


def _parse_int(text: str, default: int = 0) -> int:
    """'+1,234', '-1,234', '1,234', '-' 등을 정수로 변환합니다."""
    if text is None:
        return default
    cleaned = text.strip().replace(",", "").replace("+", "")
    if not cleaned or cleaned == "-":
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


@cached(ttl_market=600, ttl_closed=86400)  # 장중 10분, 장마감 1일
async def search_stock(query: str) -> list[dict]:
    """종목명 또는 코드로 검색하여 종목 코드를 반환합니다.

    네이버 모바일 증권의 autoComplete JSON API 사용.
    (이전 finance.naver.com/search/searchList.naver 엔드포인트는 2026년경 폐지)
    """
    url = "https://m.stock.naver.com/front-api/search/autoComplete"
    params = {"query": query, "target": "stock"}
    # 연결 실패를 삼키지 않는다. 예전엔 여기서 통째로 []를 돌려줘서, 네이버에 아예
    # 못 붙는 PC에서도 화면에는 "종목을 찾을 수 없습니다"만 떴다 — 2026-08-13 문의에서
    # 고객이 종목명만 바꿔가며 재시도했고, 기록에도 error=null 로 남아 우리도 성공한
    # 호출로 읽었다. 네트워크 오류는 safe_tool 이 원인까지 붙여 안내한다.
    resp = await fetch(url, params=params)
    try:
        data = resp.json()
    except Exception:
        # 200인데 JSON이 아니면 네이버가 형식을 바꾼 것 — '검색 결과 없음'이 맞다.
        return []

    if not data.get("isSuccess"):
        return []

    items = data.get("result", {}).get("items", []) or []
    results = []
    for it in items:
        code = it.get("code")
        name = it.get("name")
        if code and name and re.match(r"^[A-Za-z0-9]{6}$", code):
            results.append({
                "code": code,
                "name": name,
                "market": it.get("typeName", ""),  # "코스닥" / "코스피"
            })
    return results[:5]


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_ohlcv(
    code: str,
    timeframe: str = "day",
    count: int = 120,
) -> list[dict]:
    """네이버 차트 API에서 OHLCV(시가/고가/저가/종가/거래량) 데이터를 가져옵니다.

    Args:
        code: 종목코드 (예: "005930")
        timeframe: "day"(일봉), "week"(주봉), "month"(월봉)
        count: 가져올 봉 개수 (기본 60개)
    """
    params = {
        "symbol": code,
        "timeframe": timeframe,
        "count": count,
        "requestType": "0",
    }
    resp = await fetch(FCHART_URL, params=params)
    text = resp.text

    # 네이버 fchart 응답은 JS 배열 형태 → 파싱
    text = text.strip()
    rows = []
    candidates = 0  # 데이터 행처럼 생긴 줄의 수 — '없는 것'과 '못 읽은 것'의 구분자
    for line in text.split("\n"):
        line = line.strip().strip(",")
        # 데이터 행처럼 생긴 줄만 후보로 센다. 헤더('날짜'...)와 대괄호만 있는 줄
        # ('[' / ']')을 후보에 넣으면, 데이터가 진짜 없는 종목에서 빈 응답을
        # '파싱 실패'로 오판한다.
        if not line or "날짜" in line or not any(ch.isdigit() for ch in line):
            continue
        # ['20250401', 67800, 68200, 67100, 67500, 12345678]
        candidates += 1
        line = line.strip("[]")
        parts = [p.strip().strip("'\"") for p in line.split(",")]
        if len(parts) >= 6:
            try:
                rows.append({
                    "date": parts[0].strip(),
                    "open": int(parts[1]),
                    "high": int(parts[2]),
                    "low": int(parts[3]),
                    "close": int(parts[4]),
                    "volume": int(parts[5]),
                })
            except (ValueError, IndexError):
                continue

    # 데이터처럼 생긴 줄이 있었는데 단 하나도 못 읽었다면 형식이 바뀐 것이다.
    # 그냥 빈 리스트를 돌려주면 화면엔 '차트 데이터 없음'으로 뜨고, 사용자는
    # 상장폐지된 종목인 줄 안다. 행이 애초에 0줄이면 그건 진짜 '없음'이다.
    if candidates and not rows:
        raise NaverParseError(
            f"차트 응답에서 {candidates}줄을 받았지만 한 줄도 해석하지 못했습니다 "
            f"(네이버 fchart 형식 변경 가능성). code={code}, timeframe={timeframe}"
        )

    return rows


# 파싱 결과에 '무엇을 못 읽었나'를 실어 보내는 키. 소비자는 무시해도 되고,
# 메타 봉투를 만드는 쪽이 읽어서 data_completeness / warnings 로 바꾼다.
PARSE_MISS_KEY = "_parse_miss"

# 정상 종목 페이지라면 반드시 읽혀야 하는 시세 항목.
_RATE_INFO_REQUIRED = ("price", "change", "open", "high", "low", "volume")


def _parse_rate_info(scope) -> tuple[dict, list[str]]:
    """rate_info 블록(KRX 또는 NXT) 하나에서 현재가/전일대비/시가/고가/저가/거래량을 추출합니다.

    Returns:
        (읽어낸 값, 못 읽은 필드 이름 목록).

    결측을 0으로 채우지 않는다 — 거래량 0은 거래정지 종목의 **실제 값**이라
    '못 읽었다'와 같은 자리에 두면 둘을 영영 구분할 수 없다.
    """
    info: dict = {}

    price_tag = scope.select_one("p.no_today span.blind")
    if price_tag:
        price = _parse_int_strict(price_tag.text)
        if price is not None:
            info["price"] = price

    diff_tag = scope.select_one("p.no_exday em span.blind")
    if diff_tag:
        diff_text = diff_tag.text.replace(",", "")
        icon = scope.select_one("p.no_exday em.no_up, p.no_exday em.no_down")
        if icon and "no_down" in icon.get("class", []):
            diff_text = "-" + diff_text
        change = _parse_int_strict(diff_text)
        if change is not None:
            info["change"] = change

    # 네이버 no_info 테이블 구조: td마다 span.sptxt(라벨) + em > span.blind(값)
    for td in scope.select("table.no_info td"):
        label_tag = td.select_one("span.sptxt")
        value_tag = td.select_one("em > span.blind")
        if not label_tag or not value_tag:
            continue

        label = label_tag.text.strip()
        value = _parse_int_strict(value_tag.text)
        if value is None:
            continue

        if "거래량" in label:
            info["volume"] = value
        elif "시가" in label:
            info["open"] = value
        elif "고가" in label and "상한" not in label:
            info["high"] = value
        elif "저가" in label and "하한" not in label:
            info["low"] = value

    return info, [f for f in _RATE_INFO_REQUIRED if f not in info]


@cached(ttl_market=30, ttl_closed=3600)  # 장중 30초, 장마감 1시간
async def get_current_price(code: str) -> dict:
    """종목의 현재가 정보를 가져옵니다.

    코스피200/코스닥150 등 NXT 대상 종목은 네이버가 KRX/NXT 탭으로 시세를 나눠 보여주며,
    두 table.no_info가 한 페이지에 동시에 존재한다. KRX를 기본값으로 쓰고 NXT는
    nxt_ 접두사 필드로 별도 반환해 두 시장 수치가 섞이지 않게 한다.
    """
    url = f"{BASE_URL}/item/main.naver?code={code}"
    resp = await fetch(url)
    soup = BeautifulSoup(resp.text, "lxml")

    result = {"code": code}

    # 종목명
    name_tag = soup.select_one("div.wrap_company h2 a")
    if name_tag:
        result["name"] = name_tag.text.strip()

    desc = soup.select_one("div.wrap_company div.description")

    # 네이버가 페이지 상단에 "2026.08.14 기준(KRX 장마감)"으로 **기준일을 직접**
    # 명시한다. 우리 시장 캘린더로 역산하는 것보다 이게 정확하다(예상 못 한 휴장
    # 포함). 못 찾으면 None으로 두고 호출부가 캘린더로 대체한다.
    stamp = desc.select_one("em.date") if desc else None
    stamp_text = " ".join(stamp.get_text(" ", strip=True).split()) if stamp else ""
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", stamp_text)
    result["quote_date"] = (
        f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    )

    # 종목 상태(관리종목·투자경고·투자주의 등). 같은 div.description 안에 em으로
    # 붙는다 — 035290은 em.caution "투자주의" + em.manage "관리종목",
    # 024850은 em.warning "투자경고", 정상 종목(039840)은 아무것도 없다.
    # 클래스 이름을 화이트리스트로 잡으면 새 유형(투자위험·거래정지 등)이 생겼을 때
    # 조용히 놓친다. 그래서 **마커가 아닌 것만 제외**하고 나머지는 텍스트 그대로
    # 싣는다 — 라벨은 네이버가 이미 한글로 써 준다.
    result["status_flags"] = (
        [
            t
            for em in desc.select("em")
            if not (set(em.get("class") or []) & _NON_STATUS_EM_CLASSES)
            for t in [" ".join(em.get_text(" ", strip=True).split())]
            if t and len(t) <= 12
        ]
        if desc
        else []
    )

    # KRX 블록이 없는(NXT 비대상) 종목은 페이지 전체를 그대로 스코프로 사용
    krx_scope = soup.select_one("#rate_info_krx") or soup
    krx_info, krx_missing = _parse_rate_info(krx_scope)
    result.update(krx_info)

    nxt_scope = soup.select_one("#rate_info_nxt")
    if nxt_scope:
        # NXT는 부가 정보다. 여기 결측을 본 조회의 흠으로 세지 않는다.
        nxt_info, _ = _parse_rate_info(nxt_scope)
        for key, value in nxt_info.items():
            result[f"nxt_{key}"] = value

    # 호출부가 '값이 없다'와 '우리가 못 읽었다'를 구분할 수 있게 실어 보낸다.
    result[PARSE_MISS_KEY] = krx_missing
    return result


@cached(ttl_market=1800, ttl_closed=86400)  # 시장경보는 하루 단위로만 바뀐다
async def get_alert_codes() -> dict[str, list[str]]:
    """시장경보 종목 → {종목코드: [경보 라벨]}.

    랭킹·스크리닝 결과에 상태를 달기 위한 것. 종목마다 페이지를 여는 대신
    경보 목록 3개(수십 종목)만 받아 매칭한다. 라벨은 추론이 아니라 **어느
    목록에서 나왔는지** 그 자체다.

    한 종목이 여러 목록에 들어갈 수 있어 값은 리스트다.
    """
    out: dict[str, list[str]] = {}

    async def one(alert_type: str, label: str) -> None:
        try:
            resp = await fetch(
                f"{BASE_URL}/sise/investment_alert.naver", params={"type": alert_type}
            )
        except Exception:
            return  # 경보 목록은 부가 정보다. 실패해도 본 조회를 막지 않는다.
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.select('table a[href*="code="]'):
            m = re.search(r"code=(\d{6})", a.get("href", ""))
            if m:
                out.setdefault(m.group(1), [])
                if label not in out[m.group(1)]:
                    out[m.group(1)].append(label)

    await asyncio.gather(*(one(t, lbl) for t, lbl in _ALERT_TYPES.items()))
    return out


class NaverParseError(RuntimeError):
    """네이버 페이지 구조가 예상과 달라 파싱에 실패했다.

    '데이터가 없다'(빈 리스트)와 '우리가 못 읽었다'(이 예외)를 구분하기 위해 존재한다.
    둘을 섞으면 사용자는 구조 변경을 '데이터 없음'으로 읽고, 조용히 틀린 결론에 도달한다.
    """


def _flatten_header_labels(table) -> list[str]:
    """헤더 행들의 rowspan/colspan을 펼쳐 '컬럼 인덱스 → 라벨'을 만든다.

    네이버 수급표는 2단 헤더다(<thead>가 아니라 <tr><th> 로 들어 있다):

        1단: 날짜 종가 전일비 등락률 거래량 │ 기관    │ 외국인(colspan=3)
        2단:  (rowspan=2 로 1단이 점유)     │ 순매매량 │ 순매매량 보유주수 지분율

    상위·하위를 합쳐 `외국인 순매매량` / `외국인 보유주수` 처럼 구분 가능한 라벨을 만든다.
    이래야 '외국인' 컬럼이 셋인 표에서 순매매량만 정확히 집어낼 수 있다.
    """
    header_rows = [tr for tr in table.select("tr") if tr.select("th")]
    if not header_rows:
        return []

    grid: dict[int, list[str]] = {}
    occupied: dict[int, int] = {}  # 컬럼 → 위 행의 rowspan이 점유하는 마지막 행 인덱스(배타)
    for r, tr in enumerate(header_rows[:2]):
        c = 0
        for th in tr.select("th"):
            while occupied.get(c, 0) > r:  # 위 행이 rowspan 으로 잡고 있는 자리는 건너뛴다
                c += 1
            label = " ".join(th.text.split())
            try:
                colspan = max(1, int(th.get("colspan") or 1))
                rowspan = max(1, int(th.get("rowspan") or 1))
            except (TypeError, ValueError):
                colspan = rowspan = 1
            for k in range(colspan):
                grid.setdefault(c + k, []).append(label)
                if rowspan > 1:
                    occupied[c + k] = r + rowspan
            c += colspan

    if not grid:
        return []
    return [" ".join(grid.get(i, [])) for i in range(max(grid) + 1)]


# 컬럼 규칙: (필드명, 라벨에 반드시 있어야 할 키워드들, 있으면 안 되는 키워드들)
#
# 위치가 아니라 '이름'으로 컬럼을 찾는다 — 네이버가 컬럼을 추가하거나 순서를 바꿔도
# 값이 엉뚱한 자리로 들어가지 않는다. 이름을 못 찾으면 조용히 넘어가지 않고 예외를 던진다.
# must_not 은 '거래량' vs '전일거래량'처럼 한쪽이 다른 쪽을 포함할 때 필요하다.
ColumnRule = tuple[str, tuple[str, ...], tuple[str, ...]]


def _resolve_columns(table, rules: tuple[ColumnRule, ...], *, what: str) -> dict[str, int]:
    """표 헤더를 읽어 '필드 → 컬럼 인덱스'를 만든다. 특정 실패 시 NaverParseError.

    후보가 0개면 컬럼이 사라졌거나 이름이 바뀐 것이고, 2개 이상이면 규칙이 모호한
    것이다. 둘 다 '아무 값이나 집어서 계속 진행'하면 안 되는 상황이다.
    """
    labels = _flatten_header_labels(table)
    if not labels:
        raise NaverParseError(
            f"{what}의 헤더 행을 찾지 못했습니다 (네이버 페이지 구조 변경 가능성)."
        )
    mapping: dict[str, int] = {}
    for field, must, must_not in rules:
        hits = [
            i
            for i, lab in enumerate(labels)
            if all(k in lab for k in must) and not any(k in lab for k in must_not)
        ]
        if len(hits) != 1:
            raise NaverParseError(
                f"{what}에서 '{field}' 컬럼을 특정하지 못했습니다 "
                f"(일치 {len(hits)}개, 기대 1개). 실제 헤더: {labels}"
            )
        mapping[field] = hits[0]
    return mapping


_FLOW_COLUMN_RULES: tuple[ColumnRule, ...] = (
    ("date", ("날짜",), ()),
    ("close", ("종가",), ()),
    ("change", ("전일비",), ()),
    ("change_rate", ("등락률",), ()),
    ("volume", ("거래량",), ()),
    ("institutional", ("기관", "순매매"), ()),
    ("foreign", ("외국인", "순매매"), ()),
)

# 테마 목록(sise/theme.naver) — 2단 헤더. '전일대비'는 등락현황 묶음에도 들어 있어
# must_not 으로 갈라야 한다.
_THEME_LIST_RULES: tuple[ColumnRule, ...] = (
    ("name", ("테마명",), ()),
    ("change_rate", ("전일대비",), ("등락현황",)),
    ("recent_3d_rate", ("최근3일",), ()),
    ("up_count", ("등락현황", "상승"), ()),
    ("flat_count", ("등락현황", "보합"), ()),
    ("down_count", ("등락현황", "하락"), ()),
)

# 업종 목록(sise/sise_group.naver) — 테마와 같은 꼴에 '전체' 칸이 하나 더 있다.
_SECTOR_LIST_RULES: tuple[ColumnRule, ...] = (
    ("name", ("업종명",), ()),
    ("change_rate", ("전일대비",), ("등락현황",)),
    ("total_count", ("등락현황", "전체"), ()),
    ("up_count", ("등락현황", "상승"), ()),
    ("flat_count", ("등락현황", "보합"), ()),
    ("down_count", ("등락현황", "하락"), ()),
)

# 테마/업종 상세의 종목 표 — 헤더가 동일하다. 테마 쪽은 '종목명'이 colspan=2라
# 편입사유 칸까지 헤더가 이미 세어 주므로 인덱스가 저절로 맞는다.
# '거래량'은 '전일거래량'에도 들어 있어 must_not 이 필요하다.
_GROUP_STOCK_RULES: tuple[ColumnRule, ...] = (
    ("price", ("현재가",), ()),
    ("change_rate", ("등락률",), ()),
    ("volume", ("거래량",), ("전일",)),
)

# 랭킹 표(거래량/상승률/하락률) — 6번 칸부터 페이지마다 구성이 달라서 공용 위치를
# 쓰면 안 된다. 여기서 쓰는 다섯 칸은 어느 페이지에나 같은 이름으로 있다.
_RANKING_RULES: tuple[ColumnRule, ...] = (
    ("rank", ("N",), ()),
    ("name", ("종목명",), ()),
    ("price", ("현재가",), ()),
    ("change_rate", ("등락률",), ()),
    ("volume", ("거래량",), ()),
)

_MARKET_CAP_RULES: tuple[ColumnRule, ...] = _RANKING_RULES + (
    ("market_cap", ("시가총액",), ()),
)

_REPORT_RULES: tuple[ColumnRule, ...] = (
    ("stock", ("종목명",), ()),
    ("title", ("제목",), ()),
    ("broker", ("증권사",), ()),
    ("date", ("작성일",), ()),
    ("views", ("조회수",), ()),
)

_DISCLOSURE_RULES: tuple[ColumnRule, ...] = (
    ("title", ("제목",), ()),
    ("source", ("정보제공",), ()),
    ("date", ("날짜",), ()),
)


def _resolve_flow_columns(table) -> dict[str, int]:
    """수급 표 헤더를 읽어 '필드 → 컬럼 인덱스'를 만든다."""
    return _resolve_columns(table, _FLOW_COLUMN_RULES, what="수급 표")


def _parse_int_strict(text: str | None) -> int | None:
    """숫자로 못 읽으면 None. `_parse_int`는 실패 시 0을 돌려주는데, 수급에서 0은
    '순매매 0주'라는 실제 의미가 있어서 결측과 섞이면 안 된다."""
    if text is None:
        return None
    cleaned = text.strip().replace(",", "").replace("+", "")
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_rate(text: str | None) -> float | None:
    """'+2.43%' → 2.43, '-0.43%' → -0.43. 못 읽으면 None."""
    if text is None:
        return None
    cleaned = text.strip().replace("%", "").replace("+", "").replace(",", "")
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _signed_change(change_td, rate: float | None) -> int | None:
    """전일비에 부호를 붙인다.

    네이버는 전일비를 **절댓값**으로만 주고 방향은 따로 표시한다
    (`<em class="bu_pup|bu_pdn">` + `<span class="blind">상승|하락</span>`).
    그대로 숫자만 뽑으면 하락일도 양수가 되므로, 방향을 반드시 복원해야 한다.

    부호 출처는 등락률 텍스트('-0.43%')를 1순위로 쓴다 — CSS 클래스명보다 덜 바뀐다.
    방향 단서가 전혀 없고 값이 0도 아니면 추측하지 않고 None(결측)을 돌려준다.
    """
    if change_td is None:
        return None

    em = change_td.select_one("em")
    marker = " ".join(em.get("class", [])) if em is not None else ""
    blind = em.select_one("span.blind") if em is not None else None
    word = blind.text.strip() if blind is not None else ""

    # 방향 마커('상승'/'하락')를 걷어낸 뒤 숫자만 집는다. 태그 사이 공백 유무에
    # 기대지 않으려고 get_text(구분자)와 정규식을 쓴다 — 네이버가 공백을 없애도
    # 값이 결측으로 떨어지지 않아야 한다.
    text = change_td.get_text(" ", strip=True)
    if word:
        text = text.replace(word, " ")
    matched = re.search(r"[-+]?[\d,]+", text)
    magnitude = _parse_int_strict(matched.group()) if matched else None
    if magnitude is None:
        return None
    magnitude = abs(magnitude)
    if magnitude == 0:
        return 0

    if rate is not None and rate != 0:
        return -magnitude if rate < 0 else magnitude

    if "하락" in word or "하한" in word or "bu_pdn" in marker or "bu_pd" in marker:
        return -magnitude
    if "상승" in word or "상한" in word or "bu_pup" in marker or "bu_pu" in marker:
        return magnitude
    return None  # 방향을 모른 채 양수로 단정하지 않는다


@cached(ttl_market=300, ttl_closed=7200)  # 장중 5분, 장마감 2시간
async def get_investor_flow(code: str, days: int = 20) -> list[dict]:
    """투자자별 매매동향 (기관/외국인 순매매)을 가져옵니다.

    네이버 증권 frgn.naver 페이지 기준:
    - 두 번째 table.type2가 수급 데이터 테이블
    - 컬럼은 **헤더 이름으로 찾는다** (`_resolve_flow_columns`). 자리 번호로 읽으면
      네이버가 컬럼을 추가·재배치했을 때 기관 값이 외국인 자리로 들어가도 아무도 모른다.
    - 개인 순매매 컬럼은 이 페이지에 없음

    Raises:
        NaverParseError: 헤더에서 필요한 컬럼을 특정하지 못한 경우(구조 변경).
            '데이터 없음'(빈 리스트)과 구분하기 위해 예외로 알린다.
    """
    url = f"{BASE_URL}/item/frgn.naver"
    results = []
    page = 1

    while len(results) < days:
        params = {"code": code, "page": page}
        resp = await fetch(url, params=params)
        soup = BeautifulSoup(resp.text, "lxml")

        # 두 번째 table.type2가 수급 데이터 (첫 번째는 거래원)
        tables = soup.select("table.type2")
        if len(tables) < 2:
            break

        table = tables[1]
        idx = _resolve_flow_columns(table)  # 구조 검증 — 실패 시 NaverParseError
        max_idx = max(idx.values())
        rows = table.select("tr")
        found_in_page = 0
        for row in rows:
            cols = row.select("td")
            # 필요한 컬럼까지 있으면 된다. 총 개수를 고정하지 않으므로 네이버가
            # 컬럼을 덧붙여도 깨지지 않는다(위치는 헤더가 정해준다).
            if len(cols) <= max_idx:
                continue

            date_text = cols[idx["date"]].text.strip()
            # 날짜 형식(YYYY.MM.DD) 체크 — 헤더/빈 행 필터링
            if not date_text or "." not in date_text:
                continue

            rate = _parse_rate(cols[idx["change_rate"]].text)
            close = _parse_int_strict(cols[idx["close"]].text)
            volume = _parse_int_strict(cols[idx["volume"]].text)
            institutional = _parse_int_strict(cols[idx["institutional"]].text)
            foreign = _parse_int_strict(cols[idx["foreign"]].text)
            # 핵심 값이 하나라도 결측이면 0으로 메우지 않고 행을 버린다.
            # 수급에서 0은 '순매매 0주'라는 실제 의미라 결측과 섞이면 안 된다.
            if None in (close, volume, institutional, foreign):
                continue

            results.append({
                "date": date_text,
                "close": close,
                "change": _signed_change(cols[idx["change"]], rate),
                "change_rate": rate,
                "volume": volume,
                "institutional": institutional,
                "foreign": foreign,
            })
            found_in_page += 1

        if found_in_page == 0:
            break  # 더 이상 데이터 없음

        if len(results) >= days:
            break
        page += 1
        if page > 10:
            break

    return results[:days]


@cached(ttl_market=3600, ttl_closed=86400)  # 장중 1시간, 장마감 1일
async def get_financials(code: str) -> dict:
    """종목의 주요 재무지표를 가져옵니다."""
    url = f"{BASE_URL}/item/main.naver?code={code}"
    resp = await fetch(url)
    soup = BeautifulSoup(resp.text, "lxml")

    result = {"code": code}

    # 종목명
    name_tag = soup.select_one("div.wrap_company h2 a")
    if name_tag:
        result["name"] = name_tag.text.strip()

    # 투자정보 테이블 (PER, PBR, 배당수익률 등)
    # 구조: thead[0]은 대분류(연간/분기 colspan), thead[1]은 실제 기간 라벨
    cop_info = soup.select("div.cop_analysis table")
    if not cop_info and name_tag:
        # 종목명은 읽혔는데 재무 표가 통째로 없다 — 페이지 구조가 바뀐 것이다.
        # (종목명도 못 읽었으면 애초에 종목 페이지가 아닐 가능성이 높아 여기서
        #  단정하지 않는다.) 그냥 두면 화면엔 '재무지표 없음'으로만 뜬다.
        result[PARSE_MISS_KEY] = ["financial_table"]
    if cop_info:
        for table in cop_info:
            thead_rows = table.select("thead tr")
            annual_count, quarterly_count = 0, 0
            periods_flat: list[str] = []
            if len(thead_rows) >= 2:
                for th in thead_rows[0].select("th"):
                    label = th.text.strip()
                    try:
                        span = int(th.get("colspan", "1"))
                    except ValueError:
                        span = 1
                    if "연간" in label:
                        annual_count = span
                    elif "분기" in label:
                        quarterly_count = span
                periods_flat = [th.text.strip() for th in thead_rows[1].select("th")]
                if periods_flat and len(periods_flat) == annual_count + quarterly_count:
                    result["_periods"] = {
                        "annual": periods_flat[:annual_count],
                        "quarterly": periods_flat[annual_count:],
                    }
                    # thead 3행은 컬럼별 재무기준(IFRS연결/IFRS별도). 예전엔 통째로
                    # 버려서, 연결과 별도가 섞인 종목도 한 줄로 보여 비교 범위가
                    # 달라진 걸 알 수 없었다.
                    if len(thead_rows) >= 3:
                        bases = [th.text.strip() for th in thead_rows[2].select("th")]
                        if len(bases) == len(periods_flat):
                            result["_fs_basis"] = bases

            for row in table.select("tbody tr"):
                th = row.select_one("th")
                tds = row.select("td")
                if th and tds:
                    label = th.text.strip()
                    values = [td.text.strip() for td in tds]
                    if values:
                        result[label] = values

    # 시가총액, 상장주식수 등
    aside = soup.select("div.first table tr")
    for tr in aside:
        th = tr.select_one("th")
        td = tr.select_one("td")
        if th and td:
            label = th.get_text(strip=True)
            # td 내부 탭/줄바꿈 제거 후 공백 한 칸으로 정리
            value = " ".join(td.get_text(strip=True).split())
            if "시가총액" in label or "상장주식수" in label or "PER" in label or "PBR" in label:
                result[label] = value

    return result


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def list_themes(page: int = 1) -> list[dict]:
    """네이버 증권 테마 목록을 가져옵니다.

    한 페이지에 40개 테마, 총 7페이지 존재 (약 280개).

    Args:
        page: 페이지 번호 (1~7)

    Returns:
        [{name, theme_id, change_rate, recent_3d_rate, up_count, flat_count, down_count, leaders}]
    """
    url = f"{BASE_URL}/sise/theme.naver"
    resp = await fetch(url, params={"page": page})
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_1.theme")
    if not table:
        raise NaverParseError(
            "테마 목록 표(table.type_1.theme)를 찾지 못했습니다 (네이버 구조 변경 가능성)."
        )

    idx = _resolve_columns(table, _THEME_LIST_RULES, what="테마 목록")
    max_idx = max(idx.values())

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        # 테마명 링크(no=…)가 있는 칸을 행 안에서 찾는다. 첫 칸으로 못박으면
        # 앞에 칸이 하나 끼는 순간 전 행이 조용히 버려진다.
        name_tag = row.select_one('td a[href*="no="]')
        if not name_tag:
            continue

        theme_id_match = re.search(r"no=(\d+)", name_tag.get("href", ""))
        if not theme_id_match:
            continue

        # 주도주는 종목 링크(code=…)로 찾는다 — 자리 대신 내용으로.
        leaders = []
        for leader_a in row.select('td a[href*="code="]'):
            code_match = re.search(r"code=([A-Za-z0-9]{6})", leader_a.get("href", ""))
            leaders.append({
                "name": leader_a.text.strip(),
                "code": code_match.group(1) if code_match else "",
            })

        results.append({
            "name": name_tag.text.strip(),
            "theme_id": theme_id_match.group(1),
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "recent_3d_rate": cells[idx["recent_3d_rate"]].text.strip(),
            "up_count": _parse_int(cells[idx["up_count"]].text),
            "flat_count": _parse_int(cells[idx["flat_count"]].text),
            "down_count": _parse_int(cells[idx["down_count"]].text),
            "leaders": leaders,
        })

    return results


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_theme_stocks(
    theme_name: str,
    count: int = 30,
    include_reason: bool = True,
) -> dict:
    """특정 테마의 종목 리스트를 가져옵니다.

    테마명으로 먼저 검색해서 theme_id를 찾은 뒤, 상세 페이지에서 종목 추출.

    Args:
        theme_name: 테마명 (예: "선박", "AI반도체") - 부분 일치
        count: 반환할 최대 종목 수 (기본 30)
        include_reason: 편입사유 포함 여부 (False면 토큰 대폭 절감)

    Returns:
        {theme_name, theme_id, stocks: [{code, name, price, change_rate, volume, reason}]}
    """
    # 1) 모든 페이지에서 테마 검색 (부분 일치)
    theme_id = None
    matched_name = None
    for page in range(1, 8):
        resp = await fetch(
            f"{BASE_URL}/sise/theme.naver",
            params={"page": page},
        )
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.select_one("table.type_1.theme")
        if not table:
            continue

        for row in table.select("tr"):
            # 테마 링크(no=…)를 자리가 아니라 내용으로 찾는다.
            name_tag = row.select_one('td a[href*="no="]')
            if not name_tag:
                continue
            name = name_tag.text.strip()
            if theme_name in name or name.lower() == theme_name.lower():
                m = re.search(r"no=(\d+)", name_tag.get("href", ""))
                if m:
                    theme_id = m.group(1)
                    matched_name = name
                    break
        if theme_id:
            break

    if not theme_id:
        return {"theme_name": theme_name, "theme_id": None, "stocks": []}

    # 2) 테마 상세 페이지에서 종목 리스트 추출
    resp = await fetch(
        f"{BASE_URL}/sise/sise_group_detail.naver",
        params={"type": "theme", "no": theme_id},
    )
    soup = BeautifulSoup(resp.text, "lxml")

    # table.type_5가 종목 리스트
    tables = soup.select("table.type_5")
    if not tables:
        raise NaverParseError(
            f"테마 상세의 종목 표(table.type_5)를 찾지 못했습니다 (theme_id={theme_id})."
        )

    # 테마 상세는 '종목명' 헤더가 colspan=2라 편입사유 칸까지 헤더가 세어 준다.
    # 덕분에 업종 상세와 같은 규칙으로 인덱스가 각각 맞게 풀린다.
    idx = _resolve_columns(tables[0], _GROUP_STOCK_RULES, what="테마 상세 종목 표")
    max_idx = max(idx.values())

    stocks = []
    for row in tables[0].select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        name_a = row.select_one('td a[href*="code="]')
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        stock_info = {
            "code": code_match.group(1),
            "name": name_a.text.strip().rstrip("*").strip(),
            "price": _parse_int(cells[idx["price"]].text),
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "volume": _parse_int(cells[idx["volume"]].text),
        }

        if include_reason:
            # 편입사유는 헤더가 없는 칸이라 자리로 찾을 수 없다 — 클래스로 찾는다.
            reason_tag = row.select_one("td p.info_txt")
            reason = reason_tag.text.strip() if reason_tag else ""
            if len(reason) > 80:
                reason = reason[:78] + ".."
            stock_info["reason"] = reason

        stocks.append(stock_info)
        if len(stocks) >= count:
            break

    return {
        "theme_name": matched_name,
        "theme_id": theme_id,
        "stocks": stocks,
    }


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def list_sectors() -> list[dict]:
    """네이버 증권 업종(섹터) 목록을 가져옵니다.

    업종은 1페이지에 모두 존재 (약 79개).

    Returns:
        [{name, sector_id, change_rate, total_count, up_count, flat_count, down_count}]
    """
    url = f"{BASE_URL}/sise/sise_group.naver"
    resp = await fetch(url, params={"type": "upjong"})
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_1")
    if not table:
        raise NaverParseError(
            "업종 목록 표(table.type_1)를 찾지 못했습니다 (네이버 구조 변경 가능성)."
        )

    idx = _resolve_columns(table, _SECTOR_LIST_RULES, what="업종 목록")
    max_idx = max(idx.values())

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        name_tag = row.select_one('td a[href*="no="]')
        if not name_tag:
            continue

        sector_id_match = re.search(r"no=(\d+)", name_tag.get("href", ""))
        if not sector_id_match:
            continue

        results.append({
            "name": name_tag.text.strip(),
            "sector_id": sector_id_match.group(1),
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "total_count": _parse_int(cells[idx["total_count"]].text),
            "up_count": _parse_int(cells[idx["up_count"]].text),
            "flat_count": _parse_int(cells[idx["flat_count"]].text),
            "down_count": _parse_int(cells[idx["down_count"]].text),
        })

    return results


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_sector_stocks(sector_name: str, count: int = 30) -> dict:
    """특정 업종의 종목 리스트를 가져옵니다.

    Args:
        sector_name: 업종명 (예: "통신장비", "반도체") - 부분 일치
        count: 반환할 최대 종목 수 (기본 30)

    Returns:
        {sector_name, sector_id, stocks: [{code, name, price, change_rate, volume}]}
    """
    # 1) 업종 검색
    sectors = await list_sectors()
    matched = None
    for s in sectors:
        if sector_name in s["name"] or s["name"].lower() == sector_name.lower():
            matched = s
            break

    if not matched:
        return {"sector_name": sector_name, "sector_id": None, "stocks": []}

    # 2) 업종 상세 페이지에서 종목 리스트 추출
    resp = await fetch(
        f"{BASE_URL}/sise/sise_group_detail.naver",
        params={"type": "upjong", "no": matched["sector_id"]},
    )
    soup = BeautifulSoup(resp.text, "lxml")

    tables = soup.select("table.type_5")
    if not tables:
        raise NaverParseError(
            f"업종 상세의 종목 표(table.type_5)를 찾지 못했습니다 "
            f"(sector_id={matched['sector_id']})."
        )

    # 업종 상세는 편입사유 칸이 없어 테마 상세보다 한 칸 짧다. 헤더로 풀면
    # 두 경우가 각각 맞게 잡히므로 표별로 상수를 따로 둘 필요가 없다.
    idx = _resolve_columns(tables[0], _GROUP_STOCK_RULES, what="업종 상세 종목 표")
    max_idx = max(idx.values())

    stocks = []
    for row in tables[0].select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        name_a = row.select_one('td a[href*="code="]')
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        stocks.append({
            "code": code_match.group(1),
            "name": name_a.text.strip().rstrip("*").strip(),
            "price": _parse_int(cells[idx["price"]].text),
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "volume": _parse_int(cells[idx["volume"]].text),
        })
        if len(stocks) >= count:
            break

    return {
        "sector_name": matched["name"],
        "sector_id": matched["sector_id"],
        "stocks": stocks,
    }


def _raise_if_all_failed(
    ok: list, codes: list[str], failures: list[Exception]
) -> None:
    """배치에서 한 건도 못 건졌고 그게 전부 예외 때문이면, 그대로 올린다.

    배치 도구는 종목 하나가 실패해도 나머지를 살리려고 예외를 삼킨다 — 상장폐지·
    거래정지처럼 정상적인 이유가 있기 때문이다. 그런데 네트워크가 통째로 죽으면
    전부 삼켜져서 빈 리스트가 나오고, 화면에는 "조회 결과 없음"이 뜬다. 2026-08-13
    문의에서 실제로 이렇게 나갔다(get_multi_stocks 가 output_chars=18 로 기록됐고
    error 는 null 이었다 — 우리도 성공한 호출로 읽었다).

    '요청한 게 있는데 하나도 못 건졌고, 실패가 요청 수만큼 쌓였다'면 그건 결과가
    없는 게 아니라 조회가 안 된 것이다. safe_tool 이 원인을 붙여 안내한다.
    """
    if ok or not codes:
        return
    if len(failures) < len(codes):
        return  # 예외 없이 걸러진 것들 — 진짜로 자료가 없는 경우
    raise failures[0]


async def get_multi_stocks(codes: list[str]) -> list[dict]:
    """여러 종목의 기본 정보를 한 번에 병렬로 가져옵니다.

    Claude가 스크리닝 결과로 받은 N개 종목을 각각 get_price로 호출하는 것보다
    훨씬 토큰 효율적입니다. 개별 호출 시마다 MCP 도구 호출 오버헤드가 크거든요.

    Args:
        codes: 종목코드 리스트 (최대 30개)

    Returns:
        [{code, name, price, change, change_rate, volume}] 형태의 리스트
    """
    # 최대 30개 제한 (네이버 rate limit 및 응답 크기 제어)
    codes = codes[:30]

    failures: list[Exception] = []

    async def fetch_one(code: str) -> dict | None:
        try:
            data = await get_current_price(code)
            if not data or "price" not in data:
                return None

            # 등락률 계산 (close 대비 change)
            price = data["price"]
            change = data.get("change", 0)
            prev_close = price - change if change else price
            if prev_close > 0:
                change_rate = f"{change / prev_close * 100:+.2f}%"
            else:
                change_rate = "0.00%"

            return {
                "code": code,
                "name": data.get("name", ""),
                "price": price,
                "change": change,
                "change_rate": change_rate,
                "volume": data.get("volume", 0),
            }
        except Exception as e:
            # 한 종목이 실패했다고 배치 전체를 죽이지 않는다(상장폐지·거래정지 등).
            # 다만 무엇이 실패했는지는 남겨서, 아래에서 '전부 실패'를 가려낸다.
            failures.append(e)
            return None

    results = await asyncio.gather(*[fetch_one(code) for code in codes])
    ok = [r for r in results if r is not None]
    _raise_if_all_failed(ok, codes, failures)
    return ok


async def scan_stocks_to_snapshot(
    codes: list[str],
    days: int = 260,
    include_financial: bool = True,
) -> list[dict]:
    """여러 종목의 기본 정보 + 차트 통계 + 재무지표를 병렬로 수집.

    Excel 스냅샷을 만들기 위한 고수준 헬퍼. 필요한 모든 데이터를
    한 번에 수집해서 dict 리스트로 반환.

    Args:
        codes: 종목코드 리스트 (최대 500개)
        days: 차트 통계용 과거 일수 (기본 260)
        include_financial: 재무지표 포함 여부 (PER, PBR 등)

    Returns:
        각 종목마다 price/change/volume/chart_stats/financial이 합쳐진 dict
    """
    codes = codes[:500]

    # 1) 차트 통계 (병렬)
    chart_stats = await get_multi_chart_stats(codes, days=days)
    stats_map = {s["code"]: s for s in chart_stats}

    # 2) 기본 정보 (현재가/거래량) - 이미 chart_stats에 current_price 있음
    # 그래도 name을 가져오기 위해 multi_stocks 호출
    basic = await get_multi_stocks(codes[:30])  # 현재 get_multi_stocks는 30개 제한
    # 대량일 때는 개별로 병렬 호출
    if len(codes) > 30:
        rest_codes = codes[30:]
        basic_rest = []

        async def fetch_basic(code: str) -> dict | None:
            try:
                d = await get_current_price(code)
                if not d or "price" not in d:
                    return None
                return {
                    "code": code,
                    "name": d.get("name", ""),
                    "price": d["price"],
                    "volume": d.get("volume", 0),
                }
            except Exception:
                return None

        basic_rest = await asyncio.gather(*[fetch_basic(c) for c in rest_codes])
        basic_rest = [r for r in basic_rest if r is not None]
        basic = basic + basic_rest

    basic_map = {b["code"]: b for b in basic}

    # 3) 재무지표 (병렬) - 선택적
    financial_map: dict[str, dict] = {}
    if include_financial:
        async def fetch_fin(code: str) -> tuple[str, dict] | None:
            try:
                fin = await get_financials(code)
                return (code, fin) if fin else None
            except Exception:
                return None

        fin_results = await asyncio.gather(*[fetch_fin(c) for c in codes])
        for r in fin_results:
            if r:
                financial_map[r[0]] = r[1]

    # 4) 병합
    merged = []
    for code in codes:
        row: dict = {"code": code}

        if code in basic_map:
            b = basic_map[code]
            row["name"] = b.get("name", "")
            row["current_price"] = b.get("price", 0)
            row["volume"] = b.get("volume", 0)

        if code in stats_map:
            s = stats_map[code]
            row["high"] = s.get("high", 0)
            row["high_date"] = s.get("high_date", "")
            row["low"] = s.get("low", 0)
            row["low_date"] = s.get("low_date", "")
            row["drawdown_pct"] = s.get("drawdown_pct", 0)
            row["recovery_pct"] = s.get("recovery_pct", 0)
            row["period_return_pct"] = s.get("period_return_pct", 0)
            row["avg_volume"] = s.get("avg_volume", 0)

        if code in financial_map:
            f = financial_map[code]
            # 재무지표에서 숫자만 추출 (네이버는 문자열 반환하기도 함)
            for key in ("PER", "PBR", "시가총액", "배당수익률", "EPS", "BPS"):
                if key in f:
                    val = f[key]
                    if isinstance(val, str):
                        # 콤마, %, 원 등 제거 후 숫자 파싱 시도
                        cleaned = val.replace(",", "").replace("%", "").replace("원", "").strip()
                        try:
                            row[key.lower()] = float(cleaned)
                        except ValueError:
                            row[key.lower()] = val
                    else:
                        row[key.lower()] = val

        if row.get("name"):
            merged.append(row)

    return merged


async def get_multi_chart_stats(
    codes: list[str],
    days: int = 260,
) -> list[dict]:
    """여러 종목의 차트 통계를 병렬로 가져옵니다 (스크리닝 전용).

    각 종목에 대해 지정 기간 내 OHLCV를 받아서 통계만 계산해 반환한다.
    전체 OHLCV 데이터 대신 요약만 주기 때문에:
      - 100종목 × 260일 × 6필드 = 156,000개 숫자 → Claude 컨텍스트 폭발
      - 100종목 요약만 반환 → 약 100줄 텍스트

    활용 예시:
      - 52주 고점 대비 낙폭 스크리닝
      - 52주 신고가 돌파 종목 찾기
      - 가격 범위 내 횡보 종목 찾기
      - 변동성 비교

    Args:
        codes: 종목코드 리스트 (최대 100개)
        days: 조회할 과거 일수 (기본 260 = 52주)

    Returns:
        각 종목마다:
        {
            code, bars_count,
            current_price, current_date,
            high, high_date,              # 기간 내 최고가
            low, low_date,                # 기간 내 최저가
            drawdown_pct,                 # 현재가가 고점 대비 얼마나 내렸는지 (음수)
            recovery_pct,                 # 현재가가 저점에서 얼마나 올랐는지 (양수)
            period_return_pct,            # 첫 봉 시가 대비 현재 종가
            avg_volume,                   # 평균 거래량
        }
    """
    codes = codes[:100]  # 최대 100개
    failures: list[Exception] = []

    async def fetch_one(code: str) -> dict | None:
        try:
            ohlcv = await get_ohlcv(code, "day", days)
            if not ohlcv:
                return None

            # 날짜 오름차순 정렬 보장 (네이버는 오래된 것 먼저)
            # 마지막 행이 최신
            highs = [r["high"] for r in ohlcv]
            lows = [r["low"] for r in ohlcv]
            closes = [r["close"] for r in ohlcv]
            volumes = [r["volume"] for r in ohlcv]

            current_price = closes[-1]
            current_date = ohlcv[-1]["date"]

            high = max(highs)
            high_idx = highs.index(high)
            high_date = ohlcv[high_idx]["date"]

            low = min(lows)
            low_idx = lows.index(low)
            low_date = ohlcv[low_idx]["date"]

            drawdown_pct = ((current_price - high) / high * 100) if high > 0 else 0.0
            recovery_pct = ((current_price - low) / low * 100) if low > 0 else 0.0

            first_open = ohlcv[0]["open"]
            period_return_pct = (
                ((current_price - first_open) / first_open * 100) if first_open > 0 else 0.0
            )

            avg_volume = sum(volumes) // len(volumes) if volumes else 0

            return {
                "code": code,
                "bars_count": len(ohlcv),
                "current_price": current_price,
                "current_date": current_date,
                "high": high,
                "high_date": high_date,
                "low": low,
                "low_date": low_date,
                "drawdown_pct": round(drawdown_pct, 2),
                "recovery_pct": round(recovery_pct, 2),
                "period_return_pct": round(period_return_pct, 2),
                "avg_volume": avg_volume,
            }
        except Exception as e:
            failures.append(e)
            return None

    results = await asyncio.gather(*[fetch_one(c) for c in codes])
    ok = [r for r in results if r is not None]
    _raise_if_all_failed(ok, codes, failures)
    return ok


def _market_to_sosok(market: str) -> str | None:
    """시장 파라미터를 네이버 sosok 값으로 변환. None이면 전체."""
    m = market.upper()
    if m == "KOSPI":
        return "0"
    if m == "KOSDAQ":
        return "1"
    return None


@cached(ttl_market=60, ttl_closed=3600)  # 장중 1분, 장마감 1시간
async def _fetch_ranking_page(url: str, sosok: str | None, page: int = 1) -> list[dict]:
    """네이버 랭킹 페이지 HTML을 파싱해서 종목 리스트 반환 (한 페이지 = 50개).

    거래량/상승률/하락률 페이지 공통 구조 (12 cells).
    """
    params: dict[str, str | int] = {"page": page}
    if sosok is not None:
        params["sosok"] = sosok
    resp = await fetch(url, params=params)
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_2")
    if not table:
        raise NaverParseError(
            f"랭킹 표(table.type_2)를 찾지 못했습니다 (url={url}, page={page})."
        )

    # 거래량 페이지와 상승/하락률 페이지는 6번 칸부터 컬럼 구성이 서로 다르다
    # (거래대금 vs 매수호가). 자리를 공용으로 가정하면 안 되는 구조라 헤더로 푼다.
    idx = _resolve_columns(table, _RANKING_RULES, what="랭킹 표")
    max_idx = max(idx.values())

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        # 순위 숫자 확인 (헤더/광고 행 걸러냄)
        rank_text = cells[idx["rank"]].text.strip()
        if not rank_text.isdigit():
            continue

        name_a = cells[idx["name"]].find("a")
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        price = _parse_int(cells[idx["price"]].text)
        volume = _parse_int(cells[idx["volume"]].text)
        results.append({
            "rank": int(rank_text),
            "code": code_match.group(1),
            "name": name_a.text.strip(),
            "price": price,
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "volume": volume,
            # 현재가 × 거래량 추산. 실제 거래대금은 체결가 가중이라 살짝 다르다.
            "trade_value_est_krw": price * volume,
        })

    return results


async def _fetch_ranking_multi_page(
    url: str,
    sosok: str | None,
    count: int,
) -> list[dict]:
    """count에 맞춰 여러 페이지를 병렬로 가져옴 (네이버는 페이지당 50개)."""
    pages_needed = (count + 49) // 50  # 올림
    pages_needed = max(1, min(pages_needed, 10))  # 1~10 페이지 제한

    results_list = await asyncio.gather(
        *[_fetch_ranking_page(url, sosok, page=p) for p in range(1, pages_needed + 1)]
    )
    # 여러 페이지 병합
    merged = []
    for page_results in results_list:
        merged.extend(page_results)
    return merged[:count]


async def get_volume_ranking(
    market: str = "ALL",
    count: int = 50,
    sort_by: str = "volume",
) -> list[dict]:
    """거래량/거래대금 상위 종목을 가져옵니다.

    Args:
        market: "KOSPI" / "KOSDAQ" / "ALL" (기본 ALL = KOSPI+KOSDAQ 합산)
        count: 최대 반환 개수 (기본 50, 최대 500)
        sort_by: "volume"(거래량=주수) / "trade_value"(거래대금=원). 기본 volume.
    """
    count = min(count, 500)
    url = f"{BASE_URL}/sise/sise_quant.naver"
    sort_key = "trade_value_est_krw" if sort_by == "trade_value" else "volume"

    if market.upper() == "ALL":
        kospi, kosdaq = await asyncio.gather(
            _fetch_ranking_multi_page(url, "0", count),
            _fetch_ranking_multi_page(url, "1", count),
        )
        merged = sorted(kospi + kosdaq, key=lambda x: x.get(sort_key, 0), reverse=True)
        # 랭크 재부여 (병합 후 순위)
        for i, item in enumerate(merged[:count], 1):
            item["rank"] = i
        return merged[:count]
    else:
        sosok = _market_to_sosok(market)
        results = await _fetch_ranking_multi_page(url, sosok, count)
        if sort_by == "trade_value":
            results = sorted(results, key=lambda x: x.get("trade_value_est_krw", 0), reverse=True)
            for i, item in enumerate(results[:count], 1):
                item["rank"] = i
        return results[:count]


async def get_change_ranking(direction: str = "up", market: str = "ALL", count: int = 50) -> list[dict]:
    """등락률 상위/하위 종목을 가져옵니다.

    Args:
        direction: "up"(상승률) / "down"(하락률)
        market: "KOSPI" / "KOSDAQ" / "ALL"
        count: 최대 반환 개수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    page_url = "sise_rise.naver" if direction.lower() == "up" else "sise_fall.naver"
    url = f"{BASE_URL}/sise/{page_url}"

    if market.upper() == "ALL":
        kospi, kosdaq = await asyncio.gather(
            _fetch_ranking_multi_page(url, "0", count),
            _fetch_ranking_multi_page(url, "1", count),
        )
        merged = kospi + kosdaq

        def parse_rate(s: str) -> float:
            try:
                return float(s.replace("%", "").replace("+", ""))
            except ValueError:
                return 0.0

        reverse = direction.lower() == "up"
        merged.sort(key=lambda x: parse_rate(x["change_rate"]), reverse=reverse)
        return merged[:count]
    else:
        sosok = _market_to_sosok(market)
        return await _fetch_ranking_multi_page(url, sosok, count)


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def _fetch_market_cap_page(sosok: str, page: int = 1) -> list[dict]:
    """시가총액 페이지 1페이지(50개)를 파싱. cells=13 구조."""
    url = f"{BASE_URL}/sise/sise_market_sum.naver"
    resp = await fetch(url, params={"sosok": sosok, "page": page})
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_2")
    if not table:
        raise NaverParseError(
            f"시가총액 표(table.type_2)를 찾지 못했습니다 (sosok={sosok}, page={page})."
        )

    idx = _resolve_columns(table, _MARKET_CAP_RULES, what="시가총액 랭킹")
    max_idx = max(idx.values())

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        rank_text = cells[idx["rank"]].text.strip()
        if not rank_text.isdigit():
            continue

        name_a = cells[idx["name"]].find("a")
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        results.append({
            "rank": int(rank_text),
            "code": code_match.group(1),
            "name": name_a.text.strip(),
            "price": _parse_int(cells[idx["price"]].text),
            "change_rate": cells[idx["change_rate"]].text.strip(),
            "market_cap_billion": _parse_int(cells[idx["market_cap"]].text),  # 단위: 억원
            "volume": _parse_int(cells[idx["volume"]].text),
        })

    return results


async def get_market_cap_ranking(market: str = "KOSPI", count: int = 50) -> list[dict]:
    """시가총액 상위 종목을 가져옵니다.

    네이버 페이지당 50개이므로 count가 50 넘으면 여러 페이지 병렬 요청.

    Args:
        market: "KOSPI" / "KOSDAQ" (ALL 미지원)
        count: 최대 반환 개수 (기본 50, 최대 500)
    """
    count = min(count, 500)
    sosok = _market_to_sosok(market) or "0"

    pages_needed = max(1, min((count + 49) // 50, 10))
    results_list = await asyncio.gather(
        *[_fetch_market_cap_page(sosok, page=p) for p in range(1, pages_needed + 1)]
    )
    merged = []
    for page_results in results_list:
        merged.extend(page_results)
    return merged[:count]


@cached(ttl_market=30, ttl_closed=3600)  # 장중 30초, 장마감 1시간
async def get_market_index() -> list[dict]:
    """KOSPI, KOSDAQ 지수 현재값을 가져옵니다.

    네이버 증권의 `#now_value` (em), `#change_value_and_rate` (span) 파싱.
    """
    url_base = f"{BASE_URL}/sise/sise_index.naver"

    async def fetch_one(code: str) -> dict:
        resp = await fetch(url_base, params={"code": code})
        soup = BeautifulSoup(resp.text, "lxml")

        item: dict = {"index": code}
        missing: list[str] = []

        now_val = soup.select_one("#now_value")
        parsed = _parse_float(now_val.text, default=None) if now_val else None
        if parsed is None:
            # 예전엔 파싱 실패 시 원문 문자열을 그대로 value 에 넣었다. 숫자 자리에
            # 문자열이 앉으면 소비자는 그게 지수인 줄 알고 그대로 쓴다.
            missing.append("value")
        else:
            item["value"] = parsed

        change_val = soup.select_one("#change_value_and_rate")
        if change_val is None:
            missing.extend(["change_rate", "change_value"])
        else:
            # 구조: <span class="fluc"><span>164.60</span> +2.42%<span class="blind">상승</span></span>
            raw = change_val.get_text(" ", strip=True)
            item["change_raw"] = raw

            # 퍼센트에는 네이버가 부호를 직접 붙여 준다('+2.42%' / '-2.42%').
            rate_m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", raw)
            if rate_m:
                item["change_rate"] = float(rate_m.group(1))
            else:
                missing.append("change_rate")

            # 포인트 변화는 절댓값으로만 온다 — 방향은 .blind(상승/하락)에만 있다.
            blind = change_val.select_one("span.blind")
            word = blind.text.strip() if blind else ""
            point_tag = next(
                (s for s in change_val.select("span") if s is not blind), None
            )
            point = _parse_float(point_tag.get_text(strip=True), default=None) if point_tag else None
            if point is None:
                missing.append("change_value")
            else:
                item["change_value"] = -abs(point) if "하락" in word else abs(point)

        if missing:
            item[PARSE_MISS_KEY] = missing
        return item

    results = await asyncio.gather(fetch_one("KOSPI"), fetch_one("KOSDAQ"))
    return list(results)


# ---------------------------------------------------------------------------
# ETF 데이터
# ---------------------------------------------------------------------------

ETF_LIST_API = f"{BASE_URL}/api/sise/etfItemList.nhn"
ETF_WISEREPORT_URL = "https://navercomp.wisereport.co.kr/v2/ETF/index.aspx"

# etfTabCode → 카테고리명 매핑
_ETF_TAB_NAMES = {
    1: "국내 시장지수",
    2: "국내 업종/테마",
    3: "국내 파생",
    4: "해외 주식",
    5: "원자재",
    6: "채권/금리",
    7: "단기자금",
}


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_etf_list(
    category: str | None = None,
    keyword: str | None = None,
    sort_by: str = "marketSum",
    limit: int = 20,
) -> dict:
    """ETF 전체 목록을 가져옵니다.

    Args:
        category: 필터 카테고리 (None=전체, "국내 시장지수", "해외 주식" 등)
        keyword: ETF 이름에 포함된 키워드로 필터 (예: "우주", "반도체", "배당").
            카테고리를 몰라도 테마 이름으로 바로 찾을 수 있다. 이미 한 번에
            받아온 전체 목록을 대상으로 in-memory 필터링이라 추가 네트워크
            호출은 없다. category와 함께 쓰면 AND 조건.
        sort_by: 정렬 기준 ("marketSum", "quant", "threeMonthEarnRate", "nav")
        limit: 반환 개수 (기본 20, 최대 50)
    """
    resp = await fetch(ETF_LIST_API)
    # 네이버 ETF API는 EUC-KR 인코딩
    import json as _json
    data = _json.loads(resp.content.decode("euc-kr"))
    items = data.get("result", {}).get("etfItemList", [])
    if not items:
        # 국내 상장 ETF 전체 목록이 진짜로 빈 적은 없다 — 비었다면 응답 형식이
        # 바뀐 것이다. 빈 목록으로 돌려주면 화면엔 'ETF가 없습니다'로 뜬다.
        raise NaverParseError(
            "ETF 목록 응답에서 etfItemList 를 찾지 못했습니다 "
            f"(응답 키: {sorted(data.get('result', {}).keys()) or sorted(data.keys())})."
        )

    # 카테고리 필터
    if category:
        tab_code = None
        for code, name in _ETF_TAB_NAMES.items():
            if category in name or name in category:
                tab_code = code
                break
        if tab_code:
            items = [i for i in items if i.get("etfTabCode") == tab_code]

    # 이름 키워드 필터 (부분일치, 대소문자 무시)
    if keyword and keyword.strip():
        kw = keyword.strip().lower()
        items = [i for i in items if kw in (i.get("itemname") or "").lower()]

    # 정렬
    reverse = True
    if sort_by in ("threeMonthEarnRate",):
        items = [i for i in items if i.get(sort_by) is not None]
    items.sort(key=lambda x: abs(x.get(sort_by, 0) or 0), reverse=reverse)

    limit = min(limit, 50)
    result_items = []
    for it in items[:limit]:
        result_items.append({
            "code": it["itemcode"],
            "name": it["itemname"],
            "category": _ETF_TAB_NAMES.get(it.get("etfTabCode"), "기타"),
            "price": it.get("nowVal"),
            "change_rate": it.get("changeRate"),
            "nav": it.get("nav"),
            "return_3m": it.get("threeMonthEarnRate"),
            "volume": it.get("quant"),
            "market_cap": it.get("marketSum"),  # 억원
        })

    return {
        "items": result_items,
        "total": len(items),
        "categories": _ETF_TAB_NAMES,
    }


def _parse_float(text: str, default: float | None = 0.0) -> float | None:
    """쉼표·공백 등을 제거하고 float로 변환.

    default=None을 넘기면 결측을 0이 아니라 None으로 돌려준다 — 베타·보수율처럼
    0이 그 자체로 의미를 갖는 필드는 결측과 0을 반드시 구분해야 한다.
    """
    if not text:
        return default
    cleaned = text.strip().replace(",", "").replace("+", "")
    if not cleaned or cleaned in ("-", "N/A"):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


@cached(ttl_market=600, ttl_closed=86400)  # 장중 10분, 장마감 1일
async def get_etf_detail(code: str) -> dict:
    """ETF 상세 정보를 wisereport 페이지에서 가져옵니다.

    Returns:
        기초지수, 유형, 상장일, 보수율, 운용사, NAV, 수익률,
        구성종목 TOP10 등을 포함하는 dict.
    """
    resp = await fetch(ETF_WISEREPORT_URL, params={"cmp_cd": code})
    html = resp.text

    result = {"code": code}

    # wisereport 페이지에 임베딩된 JS 변수 파싱
    import json as _json

    def _extract_json_var(var_name: str) -> dict | None:
        import re as _re
        pattern = rf"var\s+{var_name}\s*=\s*(\{{.*?\}});"
        match = _re.search(pattern, html, _re.DOTALL)
        if match:
            try:
                return _json.loads(match.group(1))
            except (_json.JSONDecodeError, ValueError):
                return None
        return None

    summary = _extract_json_var("summary_data")
    product = _extract_json_var("product_summary_data")
    status = _extract_json_var("status_data")
    cu_raw = _extract_json_var("CU_data")

    if not any((summary, product, status)):
        # 페이지는 받았는데 임베딩된 JS 변수를 하나도 못 읽었다 — 형식이 바뀐 것이다.
        # 코드만 담긴 dict 를 돌려주면 화면엔 'ETF 정보 없음'으로 뜨고, 사용자는
        # 상장폐지됐거나 ETF가 아닌 줄 안다.
        raise NaverParseError(
            f"ETF 상세 페이지에서 summary/product/status 데이터를 하나도 읽지 "
            f"못했습니다 (code={code}). wisereport 페이지 형식 변경 가능성."
        )

    if summary:
        result["name"] = summary.get("CMP_KOR", "")
        result["name_eng"] = summary.get("CMP_ENG", "")
        result["base_index"] = summary.get("BASE_IDX_NM_KOR", "")
        result["issuer"] = summary.get("ISSUE_NM_KOR", "")
        result["etf_type"] = summary.get("ETF_TYP_SVC_NM", "")
        result["total_fee"] = _parse_float(summary.get("TOT_PAY", ""), None)

    if product:
        result["listing_date"] = product.get("LIST_DT", "")
        result["fund_type"] = product.get("FUND_TYP", "")
        result["fiscal_period"] = product.get("FIN_PRD", "")
        result["dividend_base"] = product.get("DIV_BASE_DT", "")
        result["lp_list"] = product.get("LP_NM_KOR", "")
        result["website"] = product.get("URL", "")

    if status:
        # 결측은 None으로 — 0으로 채우면 "보수 0%", "베타 0.00"처럼 읽힌다.
        result["price"] = _parse_float(status.get("CLS_PRC", ""), None)
        result["price_change"] = _parse_float(status.get("PRC_CHG", ""), None)
        result["price_change_rate"] = _parse_float(status.get("ADJ_CHG", ""), None)
        result["year_high"] = _parse_float(status.get("YR_HIGH", ""), None)
        result["year_low"] = _parse_float(status.get("YR_LOW", ""), None)
        result["market_cap"] = _parse_float(status.get("MKT_VAL", ""), None)  # 억원
        result["beta"] = _parse_float(status.get("YR_BETA", ""), None)
        result["foreign_rate"] = _parse_float(status.get("FRG_RT", ""), None)
        result["return_1m"] = _parse_float(status.get("ERN1", ""), None)
        result["return_3m"] = _parse_float(status.get("ERN3", ""), None)
        result["return_6m"] = _parse_float(status.get("ERN6", ""), None)
        result["return_1y"] = _parse_float(status.get("ERN12", ""), None)
        result["volume_20d_avg"] = _parse_float(status.get("AVG_TRD_QTY20", ""), None)
        # NAV(순자산가치)에 종가를 넣던 fallback을 제거했다. 종가와 NAV는 다르고
        # (둘의 차이가 괴리율이다) 'nav'라는 이름으로 종가를 내보내면 괴리율이
        # 항상 0으로 계산된다.

    # 구성종목
    if cu_raw and "grid_data" in cu_raw:
        holdings = []
        for h in cu_raw["grid_data"]:
            name = h.get("STK_NM_KOR", "")
            if not name:
                continue
            holdings.append({
                "name": name,
                "shares": h.get("AGMT_STK_CNT") or 0,
                "weight": h.get("ETF_WEIGHT") or 0,
            })
        # 비중 순 정렬, 비중 없으면 주식수 순
        has_weight = any(h["weight"] > 0 for h in holdings)
        sort_key = "weight" if has_weight else "shares"
        holdings.sort(key=lambda x: x[sort_key], reverse=True)
        result["holdings"] = holdings
        result["holdings_count"] = len(holdings)
        result["holdings_has_weight"] = has_weight

    return result


# ---------------------------------------------------------------------------
# 컨센서스 / 목표가
# ---------------------------------------------------------------------------

WISEREPORT_CONSENSUS_URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx"


@cached(ttl_market=600, ttl_closed=86400)  # 장중 10분, 장마감 1일
async def get_consensus(code: str) -> dict:
    """종목의 컨센서스 (투자의견, 목표주가, 실적 추정치)를 가져옵니다."""
    resp = await fetch(WISEREPORT_CONSENSUS_URL, params={"cmp_cd": code})
    html = resp.text

    import json as _json

    def _extract_json_var(var_name: str) -> dict | None:
        import re as _re
        pattern = rf"var\s+{var_name}\s*=\s*(\{{.*?\}});"
        match = _re.search(pattern, html, _re.DOTALL)
        if match:
            try:
                return _json.loads(match.group(1))
            except (_json.JSONDecodeError, ValueError):
                return None
        return None

    result = {"code": code}

    # 목표주가 추이
    chart2 = _extract_json_var("chartData2")
    if chart2 and "target_price" in chart2:
        targets = chart2["target_price"]
        valid = [t for t in targets if t.get("y") is not None]
        if valid:
            result["target_price"] = valid[-1]["y"]
        result["target_price_history"] = [
            {"date": t["x"], "price": t["y"]} for t in valid[-6:]
        ]

    # 투자의견 분포
    chart3 = _extract_json_var("chartData3")
    if chart3:
        today = chart3.get("today", [])
        result["opinion"] = {
            item["name"]: int(item["y"]) if item.get("y") else 0
            for item in today
        }
        ago = chart3.get("a_month_ago", [])
        result["opinion_1m_ago"] = {
            item["name"]: int(item["y"]) if item.get("y") else 0
            for item in ago
        }

    # 어닝 서프라이즈 (컨센서스 vs 잠정치)
    res_data = _extract_json_var("res")
    surprise = _parse_earnings_surprise(res_data) if res_data else None
    if surprise:
        result["earnings_surprise"] = surprise
        result["earnings_surprise_periods"] = res_data["yymm"]
        result["earnings_surprise_dates"] = res_data.get("yymmdd") or []

    if not any((chart2, chart3, res_data)):
        # 애널리스트 커버리지가 없는 종목도 이 세 변수 '선언' 자체는 페이지에 있다
        # (내용만 비어 있다). 셋 다 못 읽었다면 커버리지가 없는 게 아니라 페이지
        # 형식이 바뀐 것이다 — '컨센서스 없음'으로 뭉개면 구분이 사라진다.
        raise NaverParseError(
            f"컨센서스 페이지에서 chartData2/chartData3/res 를 하나도 읽지 "
            f"못했습니다 (code={code}). wisereport 형식 변경 가능성."
        )

    return result


# wisereport의 `var res`는 페이지 하단 '펀더멘털 > 어닝서프라이즈' 표(#earning_list)를
# 채우는 payload다. data 행에 이름이 없어 **위치로만** 구분된다:
#   0~4: 영업이익   (컨센서스 / 잠정치 / Surprise% / 전년동기대비 / 전분기대비)
#   5~9: 당기순이익 (동일 순서)
# 매출액은 이 표에 아예 없다. 예전 코드는 0~2를 [매출액, 영업이익, 영업이익률]로
# 라벨링해서 영업이익 컨센서스를 매출액으로, 어닝쇼크(-71.8%)를 영업이익률로
# 내보냈다(디오 2025.12 실측). 위치 가정이 깨지면 또 조용히 틀린 숫자가 나가므로
# 아래에서 Surprise 항등식으로 자가검증하고, 안 맞으면 아무것도 반환하지 않는다.
_SURPRISE_ROW_BASE = {"영업이익": 0, "당기순이익": 5}

# 반올림 표기 오차만 허용. 행이 밀리면 이 정도로는 절대 안 맞는다.
_SURPRISE_TOLERANCE_PCT = 0.5


def _parse_earnings_surprise(res_data: dict) -> dict | None:
    """어닝서프라이즈 payload → {지표: {기간: {consensus, actual, surprise}}}.

    행 위치 가정을 Surprise = (잠정치-컨센서스)/|컨센서스|×100 항등식으로 검증한다.
    한 건이라도 어긋나면(=행 배치가 바뀜) None을 돌려 섹션 자체를 생략한다.
    검증할 표본이 하나도 없어도 None — 검증 못 한 숫자는 내보내지 않는다.
    """
    periods = res_data.get("yymm") or []
    data = res_data.get("data") or []
    if not periods or len(data) < max(_SURPRISE_ROW_BASE.values()) + 3:
        return None

    out: dict[str, dict] = {}
    verified = 0
    for metric, base in _SURPRISE_ROW_BASE.items():
        rows: dict[str, dict] = {}
        for idx, period in enumerate(periods):
            key = str(idx + 1)
            consensus = data[base].get(key)
            actual = data[base + 1].get(key)
            surprise = data[base + 2].get(key)
            if consensus and actual is not None and surprise is not None:
                expected = (actual - consensus) / abs(consensus) * 100
                if abs(expected - surprise) > _SURPRISE_TOLERANCE_PCT:
                    return None
                verified += 1
            rows[period] = {
                "consensus": consensus,
                "actual": actual,
                "surprise": surprise,
            }
        out[metric] = rows
    return out if verified else None


# ---------------------------------------------------------------------------
# 증권사 리포트
# ---------------------------------------------------------------------------

REPORT_LIST_URL = f"{BASE_URL}/research/company_list.naver"
REPORT_READ_URL = f"{BASE_URL}/research/company_read.naver"


@cached(ttl_market=600, ttl_closed=3600)  # 장중 10분, 장마감 1시간
async def get_reports(code: str, count: int = 5) -> list[dict]:
    """종목의 최근 증권사 리포트 목록을 가져옵니다."""
    resp = await fetch(REPORT_LIST_URL, params={
        "searchType": "itemCode",
        "itemCode": code,
        "page": "1",
    })
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.select_one("table.type_1")
    if not table:
        raise NaverParseError(
            "리서치 리포트 표(table.type_1)를 찾지 못했습니다 (네이버 구조 변경 가능성)."
        )

    idx = _resolve_columns(table, _REPORT_RULES, what="리서치 리포트 목록")
    max_idx = max(idx.values())

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        title_a = row.select_one('td a[href*="nid="]')
        if not title_a:
            continue

        nid_match = re.search(r"nid=(\d+)", title_a.get("href", ""))
        if not nid_match:
            continue

        results.append({
            "nid": nid_match.group(1),
            "stock": cells[idx["stock"]].get_text(strip=True),
            "title": title_a.get_text(strip=True),
            "broker": cells[idx["broker"]].get_text(strip=True),
            "date": cells[idx["date"]].get_text(strip=True),
            "views": _parse_int(cells[idx["views"]].get_text(strip=True)),
        })
        if len(results) >= count:
            break

    return results


async def get_report_detail(nid: str) -> dict:
    """증권사 리포트 상세 (본문 요약 + PDF 링크 + 목표가/투자의견)."""
    resp = await fetch(REPORT_READ_URL, params={"nid": nid})
    soup = BeautifulSoup(resp.text, "lxml")

    result = {"nid": nid}

    # 목표가 / 투자의견
    for td in soup.select("td"):
        text = " ".join(td.get_text(strip=True).split())
        if "목표가" in text:
            import re as _re
            price_m = _re.search(r"목표가\s*([\d,]+)", text)
            if price_m:
                result["target_price"] = _parse_int(price_m.group(1))
            opinion_m = _re.search(r"투자의견\s*(\w+)", text)
            if opinion_m:
                result["opinion"] = opinion_m.group(1)

    # 본문 텍스트
    content_td = soup.select_one("td.view_cnt")
    if content_td:
        text = content_td.get_text(strip=True)
        if len(text) > 500:
            text = text[:500] + "..."
        result["summary"] = text
    else:
        # 본문 칸(td.view_cnt)이 없으면 요약을 조용히 빼지 않고 못 읽었다고 남긴다.
        # 여러 리포트를 한 번에 묶어 보여주는 자리라 예외 대신 표시로 알린다.
        result[PARSE_MISS_KEY] = ["summary"]

    # PDF 링크
    for a in soup.select("a"):
        href = a.get("href", "")
        if ".pdf" in href.lower():
            result["pdf_url"] = href
            break

    return result


# ---------------------------------------------------------------------------
# 공시 목록
# ---------------------------------------------------------------------------

DISCLOSURE_URL = f"{BASE_URL}/item/news_notice.naver"


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_disclosure_list(code: str, page: int = 1) -> list[dict]:
    """종목의 최근 공시 목록을 가져옵니다."""
    resp = await fetch(DISCLOSURE_URL, params={"code": code, "page": page})
    soup = BeautifulSoup(resp.text, "lxml")

    # 페이지에 표가 여럿이라 '제목·정보제공·날짜' 헤더가 풀리는 표를 골라 쓴다.
    # 아무 표나 훑으면서 앞 세 칸을 집으면, 다른 표가 끼어들 때 조용히 섞인다.
    table = None
    idx: dict[str, int] = {}
    for candidate in soup.select("table"):
        try:
            idx = _resolve_columns(candidate, _DISCLOSURE_RULES, what="공시 목록")
        except NaverParseError:
            continue
        table = candidate
        break

    if table is None:
        raise NaverParseError(
            "공시 목록 표를 찾지 못했습니다 — '제목·정보제공·날짜' 헤더를 가진 표가 "
            "페이지에 없습니다 (네이버 구조 변경 가능성)."
        )

    max_idx = max(idx.values())
    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) <= max_idx:
            continue

        title_a = cells[idx["title"]].find("a")
        if not title_a:
            continue

        title = title_a.get_text(strip=True)
        if not title:
            continue

        results.append({
            "title": title,
            "source": cells[idx["source"]].get_text(strip=True),
            "date": cells[idx["date"]].get_text(strip=True),
            "link": title_a.get("href", ""),
        })

    return results
