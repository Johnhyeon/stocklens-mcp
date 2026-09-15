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

# 2026-09 네이버가 finance.naver.com 의 HTML 화면을 전부 폐지하고 stock.naver.com
# (SPA)으로 302 를 태웠다. 우리 클라이언트는 follow_redirects=True 라 200 OK 로
# SPA 껍데기를 받아왔고, 셀렉터가 하나도 안 맞아 국내 도구가 통째로 죽었다.
# 그 화면들이 실제로 쓰는 JSON API 로 갈아탄다 — 리다이렉트를 막는 우회는
# 답이 아니다. 구주소에는 내용이 남아 있지 않고 302 만 온다.
#
# 아래 세 호스트가 서로 다른 것을 준다. 섞어 쓰면 안 된다:
#   MSTOCK_API   종목 단위 상세(시세·수급·재무)와 테마/업종 그룹
#   STOCK_API    시장 단위 목록·랭킹, 증권사 리포트
#   POLLING_API  종목 여러 개의 시세를 한 번에 (배치 전용)
MSTOCK_API = "https://m.stock.naver.com/api"
STOCK_API = "https://stock.naver.com/api"
POLLING_API = "https://polling.finance.naver.com/api"

# 시장 단위 목록 API 의 정렬 키. stock.naver.com 이 실제로 보내는 값만 쓴다
# (추측한 이름은 404 가 아니라 엉뚱한 목록을 돌려줄 수 있다).
_ORDER_VOLUME = "quantTop"
_ORDER_UP = "up"
_ORDER_DOWN = "down"
_ORDER_MARKET_SUM = "marketSum"
_ORDER_ALERT = "marketAlertType"


async def _api_json(url: str, *, what: str, params=None):
    """네이버 JSON API 를 호출해 파싱된 본문을 돌려준다.

    HTML 을 돌려받는 경우를 성공으로 세지 않는다 — 그게 이번(2026-09) 사고의
    모양이었다. 200 OK 에 SPA 껍데기가 실려 왔고, 파서는 '데이터 없음'을 돌려줬다.
    JSON 이 아니면 '못 읽었다'로 올린다.
    """
    resp = await fetch(url, params=params)
    try:
        return resp.json()
    except Exception as exc:
        raise NaverParseError(
            f"{what}: 네이버가 JSON 대신 다른 응답을 줬습니다 "
            f"(status={getattr(resp, 'status_code', '?')}, url={url}). 구조 변경 가능성."
        ) from exc


def _api_list(payload, *, what: str, key: str | None = None) -> list:
    """API 응답에서 행 목록을 꺼낸다. 모양이 다르면 파싱 실패로 올린다.

    빈 목록은 그대로 통과시킨다 — '조회 결과가 없다'와 '구조가 바뀌었다'는
    다른 사건이고, 여기서 섞으면 정상적으로 결과가 0건인 조회까지 오류가 된다.
    """
    rows = payload if key is None else (payload or {}).get(key)
    if not isinstance(rows, list):
        raise NaverParseError(
            f"{what}: 응답에서 목록({key or 'root'})을 찾지 못했습니다 (네이버 구조 변경 가능성)."
        )
    return rows


def _num(text, default=None):
    """'259,500' · '+3,643,746' · '46.65%' · 'N/A' → float. 못 읽으면 default.

    결측을 0으로 바꾸지 않는다. 거래량 0은 거래정지 종목의 실제 값이라,
    '못 읽었다'와 같은 자리에 두면 둘을 영영 구분할 수 없다.
    """
    if text is None:
        return default
    cleaned = str(text).strip().replace(",", "").replace("%", "").replace("+", "")
    if not cleaned or cleaned in ("-", "N/A"):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def _num_int(text, default=None):
    """_num 의 정수판. 소수점이 붙어 와도 잘라서 정수로 돌려준다."""
    val = _num(text, default=None)
    return default if val is None else int(val)


def _rate_text(value, default: str = "") -> str:
    """등락률 숫자를 부호 붙은 표시 문자열로. '7.69' → '+7.69%', '-3.14' → '-3.14%'."""
    rate = _num(value)
    return default if rate is None else f"{rate:+.2f}%"


def _signed_rate(row: dict) -> str:
    """등락률을 사람이 읽는 문자열로. 부호는 네이버가 주는 방향 코드로 붙인다.

    fluctuationsRatio 에는 보통 네이버가 부호를 붙여 주지만, 방향 코드
    (compareToPreviousPrice.code — 4=하한, 5=하락)와 어긋나면 코드를 따른다.
    """
    raw = _num(row.get("fluctuationsRatio"))
    if raw is None:
        return ""
    code = str((row.get("compareToPreviousPrice") or {}).get("code") or "")
    if code in ("4", "5") and raw > 0:      # 4=하한, 5=하락
        raw = -raw
    return f"{raw:+.2f}%"


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


# 네이버 분봉 API. fchart 일봉과 달리 세션 경계를 분 단위로 볼 수 있다. 약 1주치만
# 남는다(2026-09-15 실측: 한 달 범위를 요청해도 2026-09-07 부터만 온다).
MINUTE_CHART_URL = "https://api.stock.naver.com/chart/domestic/item/{code}/minute"
REGULAR_CLOSE_UNAVAILABLE = "no_1530_bar"


@cached(ttl_market=300, ttl_closed=3600)
async def get_regular_session_close(code: str, day: str) -> int | None:
    """그 거래일의 **정규장 종가**(15:30 종가 단일가 체결가). 확인할 수 없으면 None.

    2026-09-14 부터 네이버 일봉 종가는 20:00 애프터마켓 마지막 체결가다. 정규장
    종가는 분봉의 15:30 봉에서만 읽힌다. 그 날 80종목을 대조하니 15:30 봉 가격이
    다음 날 기준가와 79개 같았고, 나머지 1개는 15:30 봉 자체가 없었다(종가 단일가
    무체결). 봉이 없으면 직전 체결가로 메우지 않고 None 을 돌려준다.

    Args:
        code: 종목코드 6자리
        day: "YYYY-MM-DD" 또는 "YYYYMMDD"
    """
    digits = "".join(ch for ch in str(day) if ch.isdigit())[:8]
    if len(digits) != 8:
        return None
    what = f"분봉({code} {digits})"
    payload = await _api_json(
        MINUTE_CHART_URL.format(code=code),
        params={"startDateTime": f"{digits}1520", "endDateTime": f"{digits}1530"},
        what=what,
    )
    for row in _api_list(payload, what=what):
        if isinstance(row, dict) and str(row.get("localDateTime") or "") == f"{digits}153000":
            return _num_int(row.get("currentPrice"))
    return None


# 파싱 결과에 '무엇을 못 읽었나'를 실어 보내는 키. 소비자는 무시해도 되고,
# 메타 봉투를 만드는 쪽이 읽어서 data_completeness / warnings 로 바꾼다.
PARSE_MISS_KEY = "_parse_miss"

# 정상 종목 페이지라면 반드시 읽혀야 하는 시세 항목.
_RATE_INFO_REQUIRED = ("price", "change", "open", "high", "low", "volume")


# 시장경보 코드 → 라벨. stock.naver.com 이 쓰는 값 그대로다.
_MARKET_ALERT_LABELS = {"01": "투자주의", "02": "투자경고", "03": "투자위험"}


def _status_flags(d: dict) -> list[str]:
    """상세 응답의 상태 플래그를 사람이 읽는 라벨로. 없으면 빈 리스트.

    라벨을 우리가 새로 짓지 않는다 — 관리종목·투자경고는 시장이 쓰는 말이고,
    여기서 말을 바꾸면 사용자가 HTS 에서 본 것과 대조할 수 없다.
    """
    flags: list[str] = []
    if str(d.get("manageStatusGb") or "0") != "0" or d.get("isManagement") == "Y":
        flags.append("관리종목")
    alert = _MARKET_ALERT_LABELS.get(str(d.get("marketAlertType") or "00"))
    if alert:
        flags.append(alert)
    if d.get("tradeStopYn") == "Y" or d.get("isTradingHalt") == "Y":
        flags.append("거래정지")
    return flags


def _rate_info(d: dict) -> tuple[dict, list[str]]:
    """상세 응답에서 현재가/전일대비/시가/고가/저가/거래량을 뽑는다.

    Returns:
        (읽어낸 값, 못 읽은 필드 이름 목록).

    결측을 0으로 채우지 않는다 — 거래량 0은 거래정지 종목의 **실제 값**이라
    '못 읽었다'와 같은 자리에 두면 둘을 영영 구분할 수 없다.
    """
    info: dict = {}
    for key, src in (
        ("price", "nowPrice"),
        ("change", "prevChangePrice"),
        ("open", "openPrice"),
        ("high", "highPrice"),
        ("low", "lowPrice"),
        ("volume", "tradeVolume"),
    ):
        val = _num_int(d.get(src))
        if val is not None:
            info[key] = val
    return info, [f for f in _RATE_INFO_REQUIRED if f not in info]


def _quote_date(trade_time) -> str | None:
    """'20260911161021' → '2026-09-11'. 못 읽으면 None(호출부가 캘린더로 대체)."""
    raw = str(trade_time or "")
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


# 네이버가 시세에 붙이는 거래 세션 표기 → 결과 메타 price_session. 2026-09-15 실측으로 본
# 값만 둔다. 종목 상세·시장 목록은 REGULAR_MARKET 식, 벌크 시세(polling)는 regularMarket 식.
# 16:00 에 KRX 가 애프터마켓으로 넘어가면 같은 필드가 AFTER_MARKET 으로 바뀌고 가격도 움직인다.
_SESSION_VALUES = {
    "REGULAR_MARKET": "regular",
    "AFTER_MARKET": "after_market",
    "regularMarket": "regular",
    "afterMarket": "after_market",
}


def _price_session(value) -> str | None:
    """원천 세션 표기 → regular / after_market. 값이 없으면 None, 처음 보는 값이면 unknown."""
    if value in (None, ""):
        return None
    return _SESSION_VALUES.get(str(value), "unknown")


async def _stock_detail(code: str, code_type: str = "KRX") -> dict:
    """종목 상세(시세·재무비율·업종·상태) 한 방. stock.naver.com 종목 화면의 소스다."""
    return await _api_json(
        f"{STOCK_API}/domestic/detail/{code}/detail",
        params={"codeType": code_type},
        what=f"종목 상세({code})",
    )


@cached(ttl_market=30, ttl_closed=3600)  # 장중 30초, 장마감 1시간
async def get_current_price(code: str) -> dict:
    """종목의 현재가 정보를 가져옵니다.

    코스피200/코스닥150 등 NXT 대상 종목은 KRX/NXT 시세가 따로 존재한다.
    KRX를 기본값으로 쓰고 NXT는 nxt_ 접두사 필드로 별도 반환해 두 시장 수치가
    섞이지 않게 한다. NXT 대상인지 여부는 추측하지 않고 sosok 응답의
    isNxtYn 을 그대로 따른다.
    """
    detail, sosok = await asyncio.gather(
        _stock_detail(code, "KRX"),
        _api_json(f"{STOCK_API}/domestic/detail/{code}/sosok", what=f"시장 구분({code})"),
        return_exceptions=True,
    )
    if isinstance(detail, BaseException):
        raise detail
    if not isinstance(detail, dict) or not detail.get("itemcode"):
        raise NaverParseError(
            f"종목 상세 응답에 시세가 없습니다 (code={code}). 네이버 구조 변경 가능성."
        )

    result: dict = {"code": code}
    if detail.get("itemname"):
        result["name"] = detail["itemname"]
    result["quote_date"] = _quote_date(detail.get("tradeTime"))
    result["status_flags"] = _status_flags(detail)
    # 가격이 어느 세션 체결인지와 전일대비의 기준(기준가). 기준가는 전일 정규장 종가라
    # 애프터마켓 중에도 바뀌지 않는다(prevClosePrice 는 20:00 마지막가라 쓰지 않는다).
    result["price_session"] = _price_session(detail.get("tradingSessionType"))
    base = _num_int(detail.get("stdPrice"))
    if base is not None:
        result["base_price"] = base

    krx_info, krx_missing = _rate_info(detail)
    result.update(krx_info)

    is_nxt = isinstance(sosok, dict) and sosok.get("isNxtYn") == "Y"
    if is_nxt:
        try:
            nxt = await _stock_detail(code, "NXT")
        except Exception:
            nxt = None          # NXT는 부가 정보다. 실패해도 본 조회를 막지 않는다.
        if isinstance(nxt, dict):
            nxt_info, _ = _rate_info(nxt)
            for key, value in nxt_info.items():
                result[f"nxt_{key}"] = value
            if nxt_info:
                result["nxt_price_session"] = _price_session(nxt.get("tradingSessionType"))

    # 호출부가 '값이 없다'와 '우리가 못 읽었다'를 구분할 수 있게 실어 보낸다.
    result[PARSE_MISS_KEY] = krx_missing
    return result


@cached(ttl_market=1800, ttl_closed=7200)  # 시장경보는 하루 단위로만 바뀐다
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
            rows = await _market_stock_list(
                order_type=_ORDER_ALERT, market="ALL", size=300, alert_type=alert_type
            )
        except Exception:
            return  # 경보 목록은 부가 정보다. 실패해도 본 조회를 막지 않는다.
        for row in rows:
            code = str(row.get("itemcode") or "")
            if len(code) == 6:
                out.setdefault(code, [])
                if label not in out[code]:
                    out[code].append(label)

    await asyncio.gather(*(one(t, lbl) for t, lbl in _MARKET_ALERT_LABELS.items()))
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


_DISCLOSURE_RULES: tuple[ColumnRule, ...] = (
    ("title", ("제목",), ()),
    ("source", ("정보제공",), ()),
    ("date", ("날짜",), ()),
)


def _parse_int_strict(text: str | None) -> int | None:
    """숫자로 못 읽으면 None. 네이버 숫자는 결측이 0과 구분돼야 한다 — 수급에서 0은
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


@cached(ttl_market=300, ttl_closed=7200)  # 장중 5분, 장마감 2시간
async def get_investor_flow(code: str, days: int = 20) -> list[dict]:
    """투자자별 매매동향 (개인/기관/외국인 순매매)을 가져옵니다.

    출처는 stock.naver.com 종목 화면이 쓰는 매매동향 API 다. 예전 HTML 표와 달리
    **개인 순매매도 함께 온다** — 자리 번호가 아니라 필드 이름으로 읽으므로
    네이버가 컬럼을 더해도 기관 값이 외국인 자리로 들어갈 일이 없다.

    Args:
        code: 종목코드 6자리
        days: 조회할 일수 (한 번에 받는다 — 페이지를 도는 루프가 없다)

    Raises:
        NaverParseError: 응답이 JSON 목록이 아닌 경우(구조 변경).
            '데이터 없음'(빈 리스트)과 구분하기 위해 예외로 알린다.
    """
    days = max(1, min(days, 100))
    payload = await _api_json(
        f"{MSTOCK_API}/stock/{code}/trend",
        params={"pageSize": days},
        what=f"투자자 매매동향({code})",
    )
    rows = _api_list(payload, what=f"투자자 매매동향({code})")

    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bizdate = str(row.get("bizdate") or "")
        if len(bizdate) < 8 or not bizdate[:8].isdigit():
            continue

        close = _parse_int_strict(row.get("closePrice"))
        volume = _parse_int_strict(row.get("accumulatedTradingVolume"))
        institutional = _parse_int_strict(row.get("organPureBuyQuant"))
        foreign = _parse_int_strict(row.get("foreignerPureBuyQuant"))
        # 핵심 값이 하나라도 결측이면 0으로 메우지 않고 행을 버린다.
        # 수급에서 0은 '순매매 0주'라는 실제 의미라 결측과 섞이면 안 된다.
        if None in (close, volume, institutional, foreign):
            continue

        change = _parse_int_strict(row.get("compareToPreviousClosePrice"))
        # 전일 종가 = 종가 - 전일대비. 등락률은 이 둘에서 나온 **계산값**이다
        # (네이버가 이 응답에 등락률을 싣지 않는다).
        prev_close = close - change if change is not None else None
        change_rate = (
            round(change / prev_close * 100, 2)
            if change is not None and prev_close
            else None
        )

        results.append({
            "date": f"{bizdate[:4]}.{bizdate[4:6]}.{bizdate[6:8]}",
            "close": close,
            "change": change,
            "change_rate": change_rate,
            "volume": volume,
            "institutional": institutional,
            "foreign": foreign,
            "individual": _parse_int_strict(row.get("individualPureBuyQuant")),
        })

    return results[:days]


# 새 재무 API 의 행 이름 → 기존 키. 단위·기준이 붙은 쪽이 우리 계약이고,
# 소비자(get_financial_batch·재무건전성·스냅샷)가 이 이름으로 값을 찾는다.
# 이름만 짧아졌을 뿐 같은 표의 같은 줄이다.
_FIN_ROW_ALIASES = {
    "ROE": "ROE(지배주주)",
    "EPS": "EPS(원)",
    "PER": "PER(배)",
    "BPS": "BPS(원)",
    "PBR": "PBR(배)",
    "주당배당금": "주당배당금(원)",
}

# 기간 라벨에서 추정치를 표시하는 꼬리표. 소비자가 이걸 보고 확정치와 가른다.
_ESTIMATE_SUFFIX = "(E)"


def _fin_period_labels(title_list) -> tuple[list[str], list[str]]:
    """trTitleList → (라벨, 컬럼 키). '2026.12.' + isConsensus=Y → '2026.12(E)'."""
    labels, keys = [], []
    for t in title_list or []:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").rstrip(".")
        key = str(t.get("key") or "")
        if not title or not key:
            continue
        labels.append(title + (_ESTIMATE_SUFFIX if t.get("isConsensus") == "Y" else ""))
        keys.append(key)
    return labels, keys


def _fin_rows(payload) -> tuple[list[str], list[str], dict[str, dict]]:
    """재무 API 응답 → (기간 라벨, 컬럼 키, {행 이름: {컬럼 키: 값}})."""
    info = (payload or {}).get("financeInfo") or {}
    labels, keys = _fin_period_labels(info.get("trTitleList"))
    table: dict[str, dict] = {}
    for row in info.get("rowList") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        cols = row.get("columns") or {}
        table[title] = {
            k: (v or {}).get("value") for k, v in cols.items() if isinstance(v, dict)
        }
    return labels, keys, table


@cached(ttl_market=3600, ttl_closed=86400)  # 장중 1시간, 장마감 1일
async def get_financials(code: str) -> dict:
    """종목의 주요 재무지표를 가져옵니다.

    연간·분기 표를 각각 받아 [연간 …, 분기 …] 한 줄로 이어 붙이고, 어느 구간이
    어디까지인지는 `_periods` 로 함께 넘긴다. 값만 보고 위치로 집으면 분기값이
    연간 확정치 자리에 들어간다.

    네이버가 이 표에 **시가배당률·배당성향을 더 이상 싣지 않는다**. 없는 줄은
    만들어 내지 않고, 시가배당률은 종목 요약이 기간(`valueDesc`)과 함께 주는
    단일 값만 그 기간 칸에 넣는다 — 나머지 칸은 '-'(결측)로 둔다.
    """
    annual, quarter, summary = await asyncio.gather(
        _api_json(f"{MSTOCK_API}/stock/{code}/finance/annual", what=f"연간 재무({code})"),
        _api_json(f"{MSTOCK_API}/stock/{code}/finance/quarter", what=f"분기 재무({code})"),
        _api_json(f"{MSTOCK_API}/stock/{code}/integration", what=f"종목 요약({code})"),
        return_exceptions=True,
    )
    if isinstance(annual, BaseException):
        raise annual

    result: dict = {"code": code}
    if isinstance(summary, dict) and summary.get("stockName"):
        result["name"] = summary["stockName"]

    a_labels, a_keys, a_rows = _fin_rows(annual)
    q_labels, q_keys, q_rows = ([], [], {})
    if isinstance(quarter, dict):
        q_labels, q_keys, q_rows = _fin_rows(quarter)

    if not a_labels and not q_labels:
        # 실재하는 종목인데 재무 표가 통째로 비었다면 구조가 바뀐 것이다. 그냥
        # 두면 화면엔 '재무지표 없음'으로만 뜨고, 조회는 성공으로 기록된다.
        # 종목명조차 못 받았으면 잘못된 코드일 수 있어 파싱 실패로 단정하지 않는다.
        if result.get("name"):
            result[PARSE_MISS_KEY] = ["financial_table"]
        return result

    result["_periods"] = {"annual": a_labels, "quarterly": q_labels}

    for title in list(a_rows) + [t for t in q_rows if t not in a_rows]:
        key = _FIN_ROW_ALIASES.get(title, title)
        values = [a_rows.get(title, {}).get(k) or "-" for k in a_keys]
        values += [q_rows.get(title, {}).get(k) or "-" for k in q_keys]
        result[key] = values

    if isinstance(summary, dict):
        for info in summary.get("totalInfos") or []:
            if not isinstance(info, dict):
                continue
            if info.get("code") == "marketValue" and info.get("value"):
                result["시가총액"] = info["value"]
            elif info.get("code") == "dividendYieldRatio":
                # 이 값은 valueDesc 가 말하는 **그 기간**의 것이다. 기간이 우리
                # 라벨에 없으면 아무 칸에도 넣지 않는다 — 자리를 맞추려고 최신
                # 칸에 밀어 넣는 순간 라벨과 값이 어긋난다.
                period = str(info.get("valueDesc") or "").rstrip(".")
                all_labels = a_labels + q_labels
                if period and period in all_labels and info.get("value"):
                    series = ["-"] * len(all_labels)
                    series[all_labels.index(period)] = str(info["value"]).replace("%", "")
                    result["시가배당률(%)"] = series

    return result


# 테마·업종 그룹 API 는 한 번에 100개까지만 준다. 그 이상을 요구하면 JSON 이
# 아니라 빈 응답이 온다 — 실측(pageSize=300)으로 확인했다.
_GROUP_PAGE_MAX = 100


def _group_row(row: dict) -> dict | None:
    """테마·업종 구성종목 한 줄 → {code, name, price, change_rate, volume}."""
    code = str(row.get("itemCode") or "")
    if len(code) != 6:
        return None
    return {
        "code": code,
        "name": str(row.get("stockName") or "").strip(),
        "price": _num_int(row.get("closePriceRaw") or row.get("closePrice"), default=0),
        "change_rate": _signed_rate(row),
        "volume": _num_int(
            row.get("accumulatedTradingVolumeRaw") or row.get("accumulatedTradingVolume"),
            default=0,
        ),
    }


async def _group_page(kind: str, page: int, size: int) -> dict:
    """테마/업종 목록 한 페이지. kind는 'theme' 또는 'industry'."""
    return await _api_json(
        f"{MSTOCK_API}/stocks/{kind}",
        params={"page": page, "pageSize": min(size, _GROUP_PAGE_MAX)},
        what=("테마 목록" if kind == "theme" else "업종 목록"),
    )


async def _group_members(kind: str, no: str, count: int) -> tuple[list[dict], dict]:
    """테마/업종 구성종목을 count 개까지. (행 목록, 편입사유 맵)."""
    what = "테마 상세" if kind == "theme" else "업종 상세"
    rows: list[dict] = []
    reasons: dict = {}
    page = 1
    while len(rows) < count and page <= 10:
        payload = await _api_json(
            f"{MSTOCK_API}/stocks/{kind}/{no}",
            params={"page": page, "pageSize": _GROUP_PAGE_MAX},
            what=f"{what}({no})",
        )
        chunk = _api_list(payload, what=f"{what}({no})", key="stocks")
        if page == 1 and isinstance(payload.get("themeItemInfoMap"), dict):
            reasons = payload["themeItemInfoMap"]
        rows.extend(chunk)
        if len(chunk) < _GROUP_PAGE_MAX:
            break
        page += 1
    return rows[:count], reasons


async def _find_group(kind: str, name: str) -> dict | None:
    """이름 부분일치로 테마/업종을 찾는다. 정확히 같은 이름이 있으면 그쪽이 우선."""
    partial = None
    page = 1
    while page <= 10:
        payload = await _group_page(kind, page, _GROUP_PAGE_MAX)
        groups = _api_list(
            payload, what=("테마 목록" if kind == "theme" else "업종 목록"), key="groups"
        )
        for g in groups:
            gname = str(g.get("name") or "")
            if gname.lower() == name.lower():
                return g
            if partial is None and name in gname:
                partial = g
        if len(groups) < _GROUP_PAGE_MAX:
            break
        page += 1
    return partial


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def list_themes(page: int = 1) -> list[dict]:
    """네이버 증권 테마 목록을 가져옵니다.

    한 페이지에 40개 테마 (전체 약 270개).

    Returns:
        [{name, theme_id, change_rate, total_count, up_count, flat_count, down_count}]

    네이버가 목록에서 '최근 3일 등락률'과 '주도주'를 더 이상 주지 않는다.
    없는 칸을 만들어 내지 않고 뺐다 — 주도주는 get_theme_stocks 로 확인한다.
    """
    payload = await _group_page("theme", page, 40)
    groups = _api_list(payload, what="테마 목록", key="groups")

    results = []
    for g in groups:
        if not isinstance(g, dict) or g.get("no") is None:
            continue
        results.append({
            "name": str(g.get("name") or "").strip(),
            "theme_id": str(g["no"]),
            "change_rate": _rate_text(g.get("changeRate")),
            "total_count": _num_int(g.get("totalCount"), default=0),
            "up_count": _num_int(g.get("riseCount"), default=0),
            "flat_count": _num_int(g.get("steadyCount"), default=0),
            "down_count": _num_int(g.get("fallCount"), default=0),
        })
    return results


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_theme_stocks(
    theme_name: str,
    count: int = 30,
    include_reason: bool = True,
) -> dict:
    """특정 테마의 종목 리스트를 가져옵니다.

    테마명으로 먼저 검색해서 theme_id를 찾은 뒤, 상세에서 종목을 추출한다.

    Args:
        theme_name: 테마명 (예: "선박", "AI반도체") - 부분 일치
        count: 반환할 최대 종목 수 (기본 30)
        include_reason: 편입사유 포함 여부 (False면 토큰 대폭 절감)

    Returns:
        {theme_name, theme_id, stocks: [{code, name, price, change_rate, volume, reason}]}
    """
    group = await _find_group("theme", theme_name)
    if not group:
        return {"theme_name": theme_name, "theme_id": None, "stocks": []}

    theme_id = str(group["no"])
    rows, reasons = await _group_members("theme", theme_id, count)

    stocks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = _group_row(row)
        if not info:
            continue
        if include_reason:
            reason = str(reasons.get(info["code"]) or "").strip()
            if len(reason) > 80:
                reason = reason[:78] + ".."
            info["reason"] = reason
        stocks.append(info)

    return {
        "theme_name": str(group.get("name") or theme_name),
        "theme_id": theme_id,
        "stocks": stocks,
    }


@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def list_sectors() -> list[dict]:
    """네이버 증권 업종(섹터) 목록을 가져옵니다 (약 79개, 한 번에 전부).

    Returns:
        [{name, sector_id, change_rate, total_count, up_count, flat_count, down_count}]
    """
    payload = await _group_page("industry", 1, _GROUP_PAGE_MAX)
    groups = _api_list(payload, what="업종 목록", key="groups")

    results = []
    for g in groups:
        if not isinstance(g, dict) or g.get("no") is None:
            continue
        results.append({
            "name": str(g.get("name") or "").strip(),
            "sector_id": str(g["no"]),
            "change_rate": _rate_text(g.get("changeRate")),
            "total_count": _num_int(g.get("totalCount"), default=0),
            "up_count": _num_int(g.get("riseCount"), default=0),
            "flat_count": _num_int(g.get("steadyCount"), default=0),
            "down_count": _num_int(g.get("fallCount"), default=0),
        })
    return results


# 업종 이름은 거의 안 바뀌지만 per_ttm 은 현재가에서 나온다. 장중에 30분 캐시를
# 걸면 PER 이 낡은 채로 업종 비교에 들어간다 — 시세 쪽 TTL 을 따른다.
@cached(ttl_market=300, ttl_closed=3600)  # 장중 5분, 장마감 1시간
async def get_stock_sector(code: str) -> dict:
    """종목 → 소속 업종(네이버 기준). 역방향 조회가 없어 비교 기준을 못 잡던 구멍.

    Returns:
        {"code", "sector_name", "sector_id", "per_ttm", "sector_per_naver"}
        — 못 찾으면 sector_name 이 None.

    `per_ttm` 은 네이버가 종목 화면에 쓰는 PER 이다(현재가 ÷ 최근 4분기 합산 EPS).
    연간 확정 재무로만 PER 을 계산하면 실적이 급변한 기업에서 1년 가까이 낡은
    값이 나온다(한화오션: 연간 27.94 vs TTM 12.84 — 할증/할인이 뒤집힌다).

    `sector_per_naver`(동일업종 PER)는 네이버가 새 화면에서 더 이상 주지 않아
    항상 None 이다. 계산 방식이 다른 비슷한 수치로 대체하지 않는다 — 그 값은
    업종 집계(합산 시총÷합산 순이익)라 우리 중앙값과 애초에 다른 물건이다.
    """
    out: dict = {"code": code, "sector_name": None, "sector_id": None,
                 "per_ttm": None, "sector_per_naver": None}

    detail, industry = await asyncio.gather(
        _stock_detail(code, "KRX"),
        _api_json(
            f"{STOCK_API}/domestic/detail/{code}/stock/industry",
            params={"page": 1, "pageSize": 1, "marketType": "ALL"},
            what=f"업종 조회({code})",
        ),
        return_exceptions=True,
    )
    if isinstance(detail, dict):
        name = str(detail.get("upJongName") or "").strip()
        if name:
            out["sector_name"] = name
        out["per_ttm"] = _num(detail.get("per"))
    if isinstance(industry, dict) and industry.get("upjongNo"):
        out["sector_id"] = str(industry["upjongNo"])

    # 업종 ID 가 없으면 소속 업종이 없는 종목이다 — ETF·ETN 이 이 경로로 들어온다.
    # 이름만 남기면 오독한다.
    if not out["sector_id"]:
        out["sector_name"] = None
        out["reason"] = "업종 정보 없음 (ETF·ETN이거나 업종 미분류)"
    if out["sector_name"] is None:
        out[PARSE_MISS_KEY] = ["sector_name"]
    return out


async def get_sector_stocks(sector_name: str, count: int = 30) -> dict:
    """특정 업종의 종목 리스트를 가져옵니다.

    Args:
        sector_name: 업종명 (예: "통신장비", "반도체") - 부분 일치
        count: 반환할 최대 종목 수 (기본 30)

    Returns:
        {sector_name, sector_id, stocks: [{code, name, price, change_rate, volume}]}
    """
    group = await _find_group("industry", sector_name)
    if not group:
        return {"sector_name": sector_name, "sector_id": None, "stocks": []}

    sector_id = str(group["no"])
    rows, _ = await _group_members("industry", sector_id, count)

    stocks = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = _group_row(row)
        if info:
            stocks.append(info)

    return {
        "sector_name": str(group.get("name") or sector_name),
        "sector_id": sector_id,
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
    """여러 종목의 기본 정보를 **한 번의 요청으로** 가져옵니다.

    네이버가 종목 시세를 콤마로 이어 붙여 한꺼번에 주는 API 를 쓴다. 예전에는
    종목마다 페이지를 열어 30번 요청했다.

    Args:
        codes: 종목코드 리스트 (최대 30개)

    Returns:
        [{code, name, price, change, change_rate, volume}] 형태의 리스트

    조회 자체가 실패하면 빈 리스트가 아니라 예외로 알린다. 예전엔 종목별 실패를
    전부 삼켜서, 네트워크가 통째로 죽어도 화면엔 '조회 결과 없음'만 떴다
    (2026-08-13 문의에서 실제로 이렇게 나갔다).
    """
    wanted = [str(c).strip() for c in codes[:30] if str(c).strip()]
    if not wanted:
        return []

    payload = await _api_json(
        f"{POLLING_API}/realtime/domestic/stock/{','.join(wanted)}", what="벌크 시세"
    )
    rows = _api_list(payload, what="벌크 시세", key="datas")

    by_code: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("itemCode") or "")
        price = _num_int(row.get("closePriceRaw") or row.get("closePrice"))
        if len(code) != 6 or price is None:
            continue
        change = _num_int(
            row.get("compareToPreviousClosePriceRaw") or row.get("compareToPreviousClosePrice"),
            default=0,
        )
        by_code[code] = {
            "code": code,
            "name": str(row.get("stockName") or "").strip(),
            "price": price,
            "change": change,
            "change_rate": _signed_rate(row) or "0.00%",
            "volume": _num_int(
                row.get("accumulatedTradingVolumeRaw") or row.get("accumulatedTradingVolume"),
                default=0,
            ),
            "price_session": _price_session(row.get("marketSessionType")),
        }

    # 요청한 순서를 지킨다 — 호출부가 입력 리스트와 짝지어 읽는 경우가 있다.
    # 응답에 없는 코드(상장폐지·오타)는 조용히 빠진다.
    return [by_code[c] for c in wanted if c in by_code]


def _ttm_eps(fin: dict) -> tuple[float | None, str | None]:
    """최근 **확정 4개 분기 EPS 합** = TTM EPS 와 마지막 분기 라벨.

    네이버가 종목 화면에 쓰는 PER 과 같은 기준이다("EPS 는 지배기업귀속 최근
    4분기 합산 순이익을 수정평균발행주식수로 나눈 값"). 연간 확정치로 PER 을
    내면 실적이 급변한 기업에서 1년 가까이 낡은 값이 나오고, 분기 하나만 쓰면
    계절성에 흔들린다. 실측에서 이 방식은 네이버 값과 오차 0.0~0.3% 로 맞았다.

    4개가 안 되면(신규 상장 등) None 을 돌려준다 — 3개로 합쳐 12개월인 척하지 않는다.
    """
    meta = fin.get("_periods") or {}
    annual = meta.get("annual") or []
    quarterly = meta.get("quarterly") or []
    series = fin.get("EPS(원)") or []
    if not isinstance(series, list) or not quarterly:
        return None, None
    offset = len(annual)
    confirmed: list[tuple[str, float]] = []
    for i, label in enumerate(quarterly):
        idx = offset + i
        if idx >= len(series) or "(E)" in str(label):
            continue
        raw = str(series[idx]).replace(",", "").strip()
        if not raw or raw == "-":
            continue
        try:
            confirmed.append((str(label), float(raw)))
        except ValueError:
            continue
    if len(confirmed) < 4:
        return None, None
    last4 = confirmed[-4:]
    total = sum(v for _, v in last4)
    if total <= 0:          # 적자면 PER 이 음수/무의미 — 계산하지 않는다
        return None, last4[-1][0]
    return total, last4[-1][0]


def _latest_confirmed_annual(fin: dict, key: str) -> tuple[float | None, str | None]:
    """재무 시계열에서 **가장 최근 확정값**과 그 기준 기간을 뽑는다(분기 우선).

    ⚠️ 연간 확정치만 쓰면 실적이 급변한 기업에서 1년 가까이 낡은 값이 나온다.
    한화오션은 2025.12 연간 PER 이 27.94 인데 2026.06 분기 기준은 14.82,
    네이버가 쓰는 TTM 기준은 12.84 다 — 같은 종목이 '할증'과 '할인' 양쪽으로
    읽힌다. 그래서 분기 구간에 확정치가 있으면 그쪽을 먼저 쓴다.

    네이버 재무 dict의 값은 [연간 n개 … 분기 m개] 로 이어 붙은 리스트이고,
    `_periods`가 그 구간 이름을 준다(annual/quarterly). 뒤에서부터 집으면
    분기값이나 '(E)' 추정치가 섞여 들어오므로, **annual 구간만** 보고
    추정·결측을 건너뛰며 최신 확정치를 찾는다. 기준 기간을 함께 돌려주는 이유는
    숫자만 남으면 "언제 것인지" 모르는 값이 되기 때문이다.
    """
    meta = fin.get("_periods") or {}
    annual = meta.get("annual") or []
    quarterly = meta.get("quarterly") or []
    series = fin.get(key) or []
    if not isinstance(series, list):
        return None, None

    def pick(labels, offset):
        """labels 구간을 뒤에서부터 훑어 (E)·결측을 건너뛴 최신 확정치."""
        for i in range(len(labels) - 1, -1, -1):
            idx = offset + i
            if idx >= len(series):
                continue
            label = str(labels[i])
            if "(E)" in label:          # 추정치는 확정 수치로 쓰지 않는다
                continue
            raw = str(series[idx]).replace(",", "").replace("%", "").strip()
            if not raw or raw == "-":   # 결측은 0이 아니다
                continue
            try:
                return float(raw), label
            except ValueError:
                continue
        return None, None

    # 값 배열은 [연간 …, 분기 …] 순으로 이어 붙어 온다.
    val, label = pick(quarterly, len(annual))
    if val is not None:
        return val, label
    return pick(annual, 0)


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
            # 실제 키에는 단위가 붙어 온다 — 'PER(배)', 'EPS(원)'. 예전에는 'PER'로
            # 찾아서 단위 없는 '시가총액'만 걸리고 PER·PBR은 통째로 빠졌다.
            # 값도 단일 숫자가 아니라 [연간…, 분기…] 시계열이라, 위치로 집으면
            # 추정치((E))나 엉뚱한 기간이 확정 수치로 들어간다.
            # PER 은 TTM(최근 4분기 EPS 합)으로 직접 계산한다 — 네이버 종목 화면과
            # 같은 기준이라 사용자가 대조할 수 있고, 실적 급변 기업에서도 안 낡는다.
            ttm, ttm_label = _ttm_eps(f)
            cur_price = row.get("current_price")
            if ttm and isinstance(cur_price, (int, float)) and cur_price > 0:
                row["per"] = round(cur_price / ttm, 2)
                row["per_basis"] = f"TTM~{ttm_label}"

            for out_key, src_key in (
                ("per", "PER(배)"), ("pbr", "PBR(배)"),
                ("eps", "EPS(원)"), ("bps", "BPS(원)"),
                ("roe", "ROE(지배주주)"), ("배당수익률", "시가배당률(%)"),
            ):
                if out_key == "per" and row.get("per_basis"):
                    continue        # TTM 으로 이미 계산했다 — 분기 단일값으로 덮지 않는다
                val, period = _latest_confirmed_annual(f, src_key)
                if val is not None:
                    row[out_key] = val
                    row.setdefault("fin_period", period)
            mc = f.get("시가총액")
            if mc is not None:
                row["시가총액"] = mc

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

            # 거래정지 placeholder(가격 0)를 빼지 않으면 저가 0이 52주 저가가
            # 되고 낙폭·회복률이 통째로 허구가 된다.
            from stock_mcp_server._indicators import split_valid_bars

            ohlcv, _excluded = split_valid_bars(ohlcv)
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
            excluded_bars = len(_excluded)

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
                "excluded_bars": excluded_bars,
            }
        except Exception as e:
            failures.append(e)
            return None

    results = await asyncio.gather(*[fetch_one(c) for c in codes])
    ok = [r for r in results if r is not None]
    _raise_if_all_failed(ok, codes, failures)
    return ok


# 시장 단위 목록 API 가 한 번에 주는 최대 개수. 500까지는 실측으로 확인했고,
# 우리 도구의 상한(count<=500)과 같아 페이지를 돌 필요가 없다.
_MARKET_LIST_MAX = 500


def _market_type(market: str) -> str:
    """시장 파라미터를 API 값으로. KOSPI/KOSDAQ 이 아니면 전체."""
    m = (market or "").upper()
    return m if m in ("KOSPI", "KOSDAQ") else "ALL"


@cached(ttl_market=60, ttl_closed=3600)  # 장중 1분, 장마감 1시간
async def _market_stock_list(
    order_type: str,
    market: str = "ALL",
    size: int = 50,
    alert_type: str | None = None,
) -> list[dict]:
    """시장 단위 종목 목록 API. 랭킹·시장경보가 모두 이 하나를 쓴다.

    정렬 키(order_type)만 바꾸면 시총·상승·하락·거래량·시장경보가 같은 스키마로
    나온다. 화면마다 표 구조가 달라 컬럼을 따로 풀어야 했던 예전 HTML 과 달리,
    필드 이름이 목록 종류와 무관하게 같다.
    """
    params: dict = {
        "tradeType": "KRX",
        "marketType": _market_type(market),
        "orderType": order_type,
        "startIdx": 0,
        "pageSize": max(1, min(size, _MARKET_LIST_MAX)),
    }
    if alert_type:
        params["alertType"] = alert_type
    payload = await _api_json(
        f"{STOCK_API}/domestic/market/stock/default",
        params=params,
        what=f"종목 목록({order_type})",
    )
    return _api_list(payload, what=f"종목 목록({order_type})")


def _rank_rows(rows: list, count: int) -> list[dict]:
    """목록 API 응답 → 랭킹 행. 순위는 응답 순서(=네이버가 정렬한 순서)다."""
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("itemcode") or "")
        if len(code) != 6:
            continue
        price = _num_int(row.get("nowPrice"), default=0)
        volume = _num_int(row.get("tradeVolume"), default=0)
        results.append({
            "rank": len(results) + 1,
            "code": code,
            "name": str(row.get("itemname") or "").strip(),
            "price": price,
            "change_rate": _rate_text(row.get("prevChangeRate"), default="0.00%"),
            "volume": volume,
            # 현재가 × 거래량 추산. 실제 거래대금은 체결가 가중이라 살짝 다르다.
            "trade_value_est_krw": price * volume,
            "price_session": _price_session(row.get("tradingSessionType")),
        })
        if len(results) >= count:
            break
    return results


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
    count = max(1, min(count, _MARKET_LIST_MAX))
    rows = await _market_stock_list(_ORDER_VOLUME, market=market, size=count)
    results = _rank_rows(rows, count)

    if sort_by == "trade_value":
        results.sort(key=lambda x: x.get("trade_value_est_krw", 0), reverse=True)
        for i, item in enumerate(results, 1):
            item["rank"] = i
    return results


async def get_change_ranking(
    direction: str = "up", market: str = "ALL", count: int = 50
) -> list[dict]:
    """등락률 상위/하위 종목을 가져옵니다.

    Args:
        direction: "up"(상승률) / "down"(하락률)
        market: "KOSPI" / "KOSDAQ" / "ALL"
        count: 최대 반환 개수 (기본 50, 최대 500)
    """
    count = max(1, min(count, _MARKET_LIST_MAX))
    order = _ORDER_UP if (direction or "").lower() == "up" else _ORDER_DOWN
    rows = await _market_stock_list(order, market=market, size=count)
    return _rank_rows(rows, count)


async def get_market_cap_ranking(market: str = "KOSPI", count: int = 50) -> list[dict]:
    """시가총액 상위 종목을 가져옵니다.

    Args:
        market: "KOSPI" / "KOSDAQ" (ALL 미지원)
        count: 최대 반환 개수 (기본 50, 최대 500)
    """
    count = max(1, min(count, _MARKET_LIST_MAX))
    market_type = _market_type(market)
    if market_type == "ALL":
        market_type = "KOSPI"
    rows = await _market_stock_list(_ORDER_MARKET_SUM, market=market_type, size=count)

    results = []
    for base, row in zip(_rank_rows(rows, count), rows):
        # 시가총액은 원 단위로 온다. 이 키의 라벨이 '억원'이므로 여기서 억으로 맞춘다.
        cap_won = _num(row.get("marketSum"))
        base["market_cap_billion"] = int(cap_won / 100_000_000) if cap_won else 0
        results.append(base)
    return results


@cached(ttl_market=30, ttl_closed=3600)  # 장중 30초, 장마감 1시간
async def get_market_index() -> list[dict]:
    """KOSPI, KOSDAQ 지수 현재값을 가져옵니다.

    두 지수를 한 번에 받는다. 값·전일대비·등락률이 모두 **부호가 붙은 채로**
    오므로, 예전처럼 방향 라벨(상승/하락)에서 부호를 복원할 필요가 없다 —
    그 라벨은 값과 어긋난 채 내려온 적이 있었다(코스닥 -0.41%인 날 '상승').
    """
    payload = await _api_json(
        f"{POLLING_API}/realtime/domestic/index/KOSPI,KOSDAQ", what="시장 지수"
    )
    rows = _api_list(payload, what="시장 지수", key="datas")
    by_code = {
        str(r.get("itemCode") or ""): r for r in rows if isinstance(r, dict)
    }

    results = []
    for code in ("KOSPI", "KOSDAQ"):
        item: dict = {"index": code}
        missing: list[str] = []
        row = by_code.get(code) or {}
        for key, src in (
            ("value", "closePrice"),
            ("change_value", "compareToPreviousClosePrice"),
            ("change_rate", "fluctuationsRatio"),
        ):
            parsed = _num(row.get(src))
            if parsed is None:
                missing.append(key)
            else:
                item[key] = parsed
        if missing:
            item[PARSE_MISS_KEY] = missing
        results.append(item)
    return results


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

# 증권사 리포트. 새 API 는 목록에 본문·목표주가·투자의견·PDF 까지 실어 준다
# (예전 HTML 은 목록과 본문 페이지가 따로였다).
RESEARCH_API = f"{STOCK_API}/stockSecurity/researches/v2"

# 사람이 열어 보는 리포트 화면. 본문·목표가가 같이 있고 PDF 가 없는 리포트도 열린다.
REPORT_PAGE_URL = "https://stock.naver.com/research/company"


def _report_row(row: dict) -> dict:
    """리포트 한 건 → 목록용 필드."""
    return {
        "nid": str(row.get("nid") or ""),
        "stock": str(row.get("itemName") or "").strip(),
        "title": str(row.get("title") or "").strip(),
        "broker": str(row.get("brokerName") or "").strip(),
        "date": str(row.get("writeDate") or "").strip(),
        "views": _num_int(row.get("readCount"), default=0),
    }


@cached(ttl_market=600, ttl_closed=3600)  # 장중 10분, 장마감 1시간
async def get_reports(code: str, count: int = 5) -> list[dict]:
    """종목의 최근 증권사 리포트 목록을 가져옵니다."""
    count = max(1, min(count, 10))
    payload = await _api_json(
        f"{RESEARCH_API}/company/by-items",
        params={"itemCodes": code, "size": count},
        what=f"증권사 리포트 목록({code})",
    )
    if not isinstance(payload, dict):
        raise NaverParseError(
            f"증권사 리포트 목록({code}): 응답 형식이 예상과 다릅니다 (네이버 구조 변경 가능성)."
        )
    # 종목별로 묶여 오고, 리포트가 없는 종목은 키 자체가 없다 — 그건 '없음'이다.
    rows = payload.get(code) or []
    if not isinstance(rows, list):
        raise NaverParseError(f"증권사 리포트 목록({code}): 목록을 찾지 못했습니다.")

    return [_report_row(r) for r in rows if isinstance(r, dict) and r.get("nid")][:count]


async def get_report_detail(nid: str) -> dict:
    """증권사 리포트 상세 (본문 요약 + PDF 링크 + 목표가/투자의견)."""
    payload = await _api_json(
        f"{RESEARCH_API}/company/{nid}", what=f"증권사 리포트({nid})"
    )
    if not isinstance(payload, dict):
        raise NaverParseError(
            f"증권사 리포트({nid}): 응답 형식이 예상과 다릅니다 (네이버 구조 변경 가능성)."
        )

    result: dict = {"nid": nid}

    target = _num_int(payload.get("goalPrice"))
    if target is not None:
        result["target_price"] = target
    opinion = str(payload.get("opinionText") or "").strip()
    if opinion:
        result["opinion"] = opinion
    if payload.get("attachUrl"):
        result["pdf_url"] = payload["attachUrl"]

    # 본문은 HTML 조각으로 온다. 태그를 걷어내고 요약 길이로 자른다.
    content = payload.get("content")
    if content:
        text = BeautifulSoup(str(content), "lxml").get_text(" ", strip=True)
        text = " ".join(text.split())
        if len(text) > 500:
            text = text[:500] + "..."
        result["summary"] = text
    else:
        # 본문이 없으면 요약을 조용히 빼지 않고 못 읽었다고 남긴다. 여러 리포트를
        # 한 번에 묶어 보여주는 자리라 예외 대신 표시로 알린다.
        result[PARSE_MISS_KEY] = ["summary"]

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


# ---------------------------------------------------------------------------
# 2026-09 개편으로 새로 생긴 자료
#
# 구 화면에는 없던 것들이다. 개편 대응을 하면서 새 화면이 쓰는 API 를 훑다가
# 찾았고, 값을 직접 확인한 뒤 옮겼다. 단위·라벨은 추측하지 않고 네이버 화면이
# 쓰는 말을 그대로 쓴다.
# ---------------------------------------------------------------------------

IPO_URL = f"{STOCK_API}/domestic/market/ipo/progress"
DEPOSIT_URL = f"{STOCK_API}/domestic/market/trendDeposit"
RESEARCH_LATEST_URL = f"{STOCK_API}/stockSecurity/researches/v2/latestResearch"

# 공모 단계. 네이버가 목록을 단계별로 나눠 주고, 각 목록 안의 ipoStatus 가
# 더 세분화된 상태를 말한다(예: 심사 단계 안에 '예비심사청구서제출',
# '예비심사청구승인', '증권신고서제출'). 단계 이름은 우리가 짓지 않고
# 화면이 쓰는 말을 따른다.
IPO_STAGES: dict[str, str] = {
    "examinationList": "심사",
    "demandForecastingList": "수요예측",
    "forecastingCompleteList": "수요예측완료",
    "subscriptionList": "청약",
    "subscriptionCompleteList": "청약완료",
    "listingList": "상장예정",
}

# 예탁금 항목 → 네이버 화면 표기. 값의 단위는 **억원**이다
# (차트 툴팁이 `값.toLocaleString() + "억원"` 으로 찍는다, 2026-09 실측).
DEPOSIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("customerDeposit", "고객예탁금"),
    ("creditLoan", "신용잔고"),
    ("beneficiaryCertificateStock", "주식형펀드"),
    ("beneficiaryCertificateBond", "채권형펀드"),
    ("beneficiaryCertificateMixing", "혼합형펀드"),
)
DEPOSIT_UNIT = "억원"

# 리포트 갈래 → 사람이 읽는 이름. 종목(company)만 쓰던 것을 여섯 갈래로 넓힌다.
RESEARCH_KINDS: dict[str, str] = {
    "company": "종목",
    "industry": "산업",
    "market": "시황",
    "invest": "투자전략",
    "economy": "경제",
    "debenture": "채권",
}


def _ipo_date(value) -> str | None:
    """'2026-09-15' 그대로. 빈 값은 None (0 이나 '-' 로 채우지 않는다)."""
    text = str(value or "").strip()
    return text or None


@cached(ttl_market=1800, ttl_closed=7200)  # 공모 일정은 하루 단위로 움직인다
async def get_ipo_schedule() -> dict:
    """공모주 일정을 단계별로 가져옵니다.

    Returns:
        {"stages": {단계명: [{code, name, market, industry, status, ...}]},
         "total": 건수}

    희망가·확정가·경쟁률은 그 단계에 도달하지 않았으면 None 이다. 0 으로
    채우지 않는다 - '아직 안 정해졌다'와 '0원'은 다른 말이다.
    """
    payload = await _api_json(IPO_URL, what="공모주 일정")
    if not isinstance(payload, dict):
        raise NaverParseError("공모주 일정: 응답이 dict 가 아닙니다 (구조 변경 가능성).")

    stages: dict[str, list[dict]] = {}
    total = 0
    for key, stage_name in IPO_STAGES.items():
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            items.append({
                "ipo_code": row.get("ipoCode"),
                "name": row.get("compName"),
                "market": row.get("marketType"),
                "industry": row.get("compUpjong"),
                "status": row.get("ipoStatus"),
                # 수요예측 → 청약 → 배정/환불 → 상장 순서로 날짜가 붙는다
                "forecast_start": _ipo_date(row.get("dfStartDate")),
                "forecast_end": _ipo_date(row.get("dfEndDate")),
                "subscribe_start": _ipo_date(row.get("poStartDate")),
                "subscribe_end": _ipo_date(row.get("poEndDate")),
                "alloc_date": _ipo_date(row.get("allocDate")),
                "refund_date": _ipo_date(row.get("refundDate")),
                "listing_date": _ipo_date(row.get("lcalDate")),
                # 가격은 원 단위. 희망가는 범위, 확정가는 단일값이다.
                "hope_price_low": _num_int(row.get("hopePubStart")),
                "hope_price_high": _num_int(row.get("hopePubEnd")),
                "fixed_price": _num_int(row.get("fixPubPrice")),
                "shares": _num_int(row.get("poExptShares")),
                # 수요예측 경쟁률(배). 예측 전이면 None.
                "forecast_competition": _num(row.get("fnlCmptRatio")),
                "underwriters": row.get("orgNm"),
            })
        if items:
            stages[stage_name] = items
            total += len(items)
    return {"stages": stages, "total": total}


@cached(ttl_market=1800, ttl_closed=7200)  # 하루 한 번 갱신되는 자료다
async def get_investor_deposit(days: int = 20) -> dict:
    """투자자예탁금 추이를 가져옵니다.

    시장 전체에 들어와 있는 돈이다. 종목 수급(누가 샀나)과 달리 "살 돈이
    얼마나 대기 중인가"를 본다.

    Args:
        days: 조회할 거래일 수 (최대 100)

    Returns:
        {"unit": "억원", "rows": [{date, 항목별 값과 전일대비}], "labels": {...}}

    값의 단위는 **억원**이다. 네이버 화면이 그렇게 찍는다(2026-09 실측).
    단위를 떼고 숫자만 옮기지 말 것.
    """
    days = max(1, min(days, 100))
    payload = await _api_json(
        DEPOSIT_URL, params={"size": days}, what="투자자예탁금")
    rows_raw = _api_list(payload, what="투자자예탁금", key="content")

    rows = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        bizdate = str(row.get("bizdate") or "")
        if len(bizdate) < 8 or not bizdate[:8].isdigit():
            continue
        item: dict = {
            "date": f"{bizdate[:4]}-{bizdate[4:6]}-{bizdate[6:8]}"}
        for field, label in DEPOSIT_FIELDS:
            value = _num_int(row.get(field))
            if value is None:
                continue        # 결측은 0 으로 채우지 않는다
            item[label] = value
            # 전일대비는 부호가 실려 온다('-3287'). 절댓값 필드는 쓰지 않는다.
            diff = _num_int(row.get(f"{field}Diff"))
            if diff is not None:
                item[f"{label}_전일대비"] = diff
        rows.append(item)

    return {
        "unit": DEPOSIT_UNIT,
        "labels": [label for _, label in DEPOSIT_FIELDS],
        "rows": rows[:days],
    }


@cached(ttl_market=600, ttl_closed=3600)
async def get_research_by_kind(kind: str = "company", count: int = 5) -> dict:
    """갈래별 최신 증권사 리포트.

    기존 `get_reports` 는 **종목** 리포트만 본다. 이 함수는 시황·투자전략·
    경제·채권·산업까지 여섯 갈래를 다룬다. 종목을 안 가리는 질문
    ("오늘 증권가가 시장을 어떻게 보나")에 답하려면 이쪽이 필요하다.

    Args:
        kind: RESEARCH_KINDS 의 키 (company/industry/market/invest/economy/debenture)
        count: 갈래당 건수 (1~20)

    Returns:
        {"kind": 키, "kind_label": 한글 이름, "reports": [...]}
    """
    if kind not in RESEARCH_KINDS:
        raise ValueError(
            f"지원하지 않는 리포트 갈래: {kind!r} "
            f"(지원: {', '.join(RESEARCH_KINDS)})")
    count = max(1, min(count, 20))
    payload = await _api_json(
        RESEARCH_LATEST_URL, params={"size": count},
        what=f"{RESEARCH_KINDS[kind]} 리포트")
    if not isinstance(payload, dict):
        raise NaverParseError("리포트 갈래: 응답이 dict 가 아닙니다 (구조 변경 가능성).")
    rows = payload.get(kind)
    if not isinstance(rows, list):
        raise NaverParseError(
            f"리포트 갈래: 응답에 '{kind}' 가 없습니다 (네이버 구조 변경 가능성).")

    reports = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            continue
        reports.append({
            "nid": str(row.get("nid") or ""),
            "title": row.get("title") or "",
            "broker": row.get("brokerName") or "",
            "date": row.get("writeDate") or "",
            "views": _num_int(row.get("readCount"), default=0),
            # 산업 리포트에만 붙는다. 없으면 키 자체를 만들지 않는다.
            **({"industry": row["industryKoreanName"]}
               if row.get("industryKoreanName") else {}),
            **({"analyst": row["analystName"]} if row.get("analystName") else {}),
        })
    return {"kind": kind, "kind_label": RESEARCH_KINDS[kind], "reports": reports}
