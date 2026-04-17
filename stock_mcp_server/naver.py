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
    try:
        resp = await fetch(url, params=params)
        data = resp.json()
    except Exception:
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
    for line in text.split("\n"):
        line = line.strip().strip(",")
        if not line or line.startswith("[") and "날짜" in line:
            continue
        if line == "]":
            continue
        # ['20250401', 67800, 68200, 67100, 67500, 12345678]
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

    return rows


@cached(ttl_market=30, ttl_closed=3600)  # 장중 30초, 장마감 1시간
async def get_current_price(code: str) -> dict:
    """종목의 현재가 정보를 가져옵니다."""
    url = f"{BASE_URL}/item/main.naver?code={code}"
    resp = await fetch(url)
    soup = BeautifulSoup(resp.text, "lxml")

    result = {"code": code}

    # 종목명
    name_tag = soup.select_one("div.wrap_company h2 a")
    if name_tag:
        result["name"] = name_tag.text.strip()

    # 현재가
    price_tag = soup.select_one("p.no_today span.blind")
    if price_tag:
        result["price"] = int(price_tag.text.replace(",", ""))

    # 전일대비
    diff_tag = soup.select_one("p.no_exday em span.blind")
    if diff_tag:
        diff_text = diff_tag.text.replace(",", "")
        # 상승/하락 판단
        icon = soup.select_one("p.no_exday em.no_up, p.no_exday em.no_down")
        if icon and "no_down" in icon.get("class", []):
            diff_text = "-" + diff_text
        result["change"] = int(diff_text)

    # 시세 정보 (전일/고가/저가/시가/거래량/거래대금)
    # 네이버 no_info 테이블 구조: td마다 span.sptxt(라벨) + em > span.blind(값)
    for td in soup.select("table.no_info td"):
        label_tag = td.select_one("span.sptxt")
        value_tag = td.select_one("em > span.blind")
        if not label_tag or not value_tag:
            continue

        label = label_tag.text.strip()
        value = _parse_int(value_tag.text)

        if "거래량" in label:
            result["volume"] = value
        elif "시가" in label:
            result["open"] = value
        elif "고가" in label and "상한" not in label:
            result["high"] = value
        elif "저가" in label and "하한" not in label:
            result["low"] = value

    return result


@cached(ttl_market=300, ttl_closed=7200)  # 장중 5분, 장마감 2시간
async def get_investor_flow(code: str, days: int = 20) -> list[dict]:
    """투자자별 매매동향 (기관/외국인 순매매)을 가져옵니다.

    네이버 증권 frgn.naver 페이지 기준:
    - 두 번째 table.type2가 수급 데이터 테이블
    - 컬럼 순서: 날짜 | 종가 | 전일비 | 등락률 | 거래량 | 기관 순매매 | 외국인 순매매 | 보유주수 | 지분율
    - 개인 순매매 컬럼은 이 페이지에 없음
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
        rows = table.select("tr")
        found_in_page = 0
        for row in rows:
            cols = row.select("td")
            # 수급 데이터 행은 정확히 9개 td를 가짐
            if len(cols) != 9:
                continue

            date_text = cols[0].text.strip()
            # 날짜 형식(YYYY.MM.DD) 체크 — 헤더/빈 행 필터링
            if not date_text or "." not in date_text:
                continue

            try:
                result = {
                    "date": date_text,
                    "close": _parse_int(cols[1].text),
                    "change": _parse_int(cols[2].text.split()[-1] if cols[2].text.strip() else "0"),
                    "volume": _parse_int(cols[4].text),
                    "institutional": _parse_int(cols[5].text),
                    "foreign": _parse_int(cols[6].text),
                }
                results.append(result)
                found_in_page += 1
            except (ValueError, IndexError):
                continue

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
        return []

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) != 8:
            continue

        name_tag = cells[0].find("a")
        if not name_tag:
            continue

        href = name_tag.get("href", "")
        theme_id_match = re.search(r"no=(\d+)", href)
        if not theme_id_match:
            continue

        leaders = []
        for leader_cell in cells[6:8]:
            leader_a = leader_cell.find("a")
            if leader_a:
                code_match = re.search(r"code=([A-Za-z0-9]{6})", leader_a.get("href", ""))
                leaders.append({
                    "name": leader_a.text.strip(),
                    "code": code_match.group(1) if code_match else "",
                })

        results.append({
            "name": name_tag.text.strip(),
            "theme_id": theme_id_match.group(1),
            "change_rate": cells[1].text.strip(),
            "recent_3d_rate": cells[2].text.strip(),
            "up_count": _parse_int(cells[3].text),
            "flat_count": _parse_int(cells[4].text),
            "down_count": _parse_int(cells[5].text),
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
            cells = row.select("td")
            if len(cells) != 8:
                continue
            name_tag = cells[0].find("a")
            if not name_tag:
                continue
            name = name_tag.text.strip()
            if theme_name in name or name_tag.text.strip().lower() == theme_name.lower():
                href = name_tag.get("href", "")
                m = re.search(r"no=(\d+)", href)
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
    stocks = []
    if tables:
        for row in tables[0].select("tr"):
            cells = row.select("td")
            if len(cells) < 11:
                continue

            name_a = cells[0].find("a")
            if not name_a:
                continue
            code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
            if not code_match:
                continue

            stock_info = {
                "code": code_match.group(1),
                "name": name_a.text.strip().rstrip("*").strip(),
                "price": _parse_int(cells[2].text),
                "change_rate": cells[4].text.strip(),
                "volume": _parse_int(cells[7].text),
            }

            if include_reason:
                reason_tag = cells[1].select_one("p.info_txt")
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
        return []

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 6:
            continue

        name_tag = cells[0].find("a")
        if not name_tag:
            continue

        href = name_tag.get("href", "")
        sector_id_match = re.search(r"no=(\d+)", href)
        if not sector_id_match:
            continue

        results.append({
            "name": name_tag.text.strip(),
            "sector_id": sector_id_match.group(1),
            "change_rate": cells[1].text.strip(),
            "total_count": _parse_int(cells[2].text),
            "up_count": _parse_int(cells[3].text),
            "flat_count": _parse_int(cells[4].text),
            "down_count": _parse_int(cells[5].text),
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
    stocks = []
    if tables:
        for row in tables[0].select("tr"):
            cells = row.select("td")
            # 업종 상세는 10 cells (테마와 달리 편입사유 없음)
            if len(cells) < 10:
                continue

            name_a = cells[0].find("a")
            if not name_a:
                continue
            code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
            if not code_match:
                continue

            stocks.append({
                "code": code_match.group(1),
                "name": name_a.text.strip().rstrip("*").strip(),
                "price": _parse_int(cells[1].text),
                "change_rate": cells[3].text.strip(),
                "volume": _parse_int(cells[6].text),
            })
            if len(stocks) >= count:
                break

    return {
        "sector_name": matched["name"],
        "sector_id": matched["sector_id"],
        "stocks": stocks,
    }


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
        except Exception:
            return None

    results = await asyncio.gather(*[fetch_one(code) for code in codes])
    return [r for r in results if r is not None]


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
        except Exception:
            return None

    results = await asyncio.gather(*[fetch_one(c) for c in codes])
    return [r for r in results if r is not None]


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
        return []

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 12:
            continue

        # 순위 숫자 확인 (헤더/광고 행 걸러냄)
        rank_text = cells[0].text.strip()
        if not rank_text.isdigit():
            continue

        name_a = cells[1].find("a")
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        price = _parse_int(cells[2].text)
        volume = _parse_int(cells[5].text)
        results.append({
            "rank": int(rank_text),
            "code": code_match.group(1),
            "name": name_a.text.strip(),
            "price": price,
            "change_rate": cells[4].text.strip(),
            "volume": volume,
            "trade_value_krw": price * volume,
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
    sort_key = "trade_value_krw" if sort_by == "trade_value" else "volume"

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
            results = sorted(results, key=lambda x: x.get("trade_value_krw", 0), reverse=True)
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
        return []

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 13:
            continue

        rank_text = cells[0].text.strip()
        if not rank_text.isdigit():
            continue

        name_a = cells[1].find("a")
        if not name_a:
            continue
        code_match = re.search(r"code=([A-Za-z0-9]{6})", name_a.get("href", ""))
        if not code_match:
            continue

        results.append({
            "rank": int(rank_text),
            "code": code_match.group(1),
            "name": name_a.text.strip(),
            "price": _parse_int(cells[2].text),
            "change_rate": cells[4].text.strip(),
            "market_cap_billion": _parse_int(cells[6].text),  # 단위: 억원
            "volume": _parse_int(cells[9].text),
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

        now_val = soup.select_one("#now_value")
        if now_val:
            try:
                item["value"] = float(now_val.text.strip().replace(",", ""))
            except ValueError:
                item["value"] = now_val.text.strip()

        change_val = soup.select_one("#change_value_and_rate")
        if change_val:
            # 구조: <span class="fluc">...<span>값</span> +X.XX%<span class="blind">상승/하락</span></span>
            raw = change_val.get_text(" ", strip=True)
            item["change_raw"] = raw
            # 수치 분리 시도
            parts = raw.replace("상승", "+").replace("하락", "-").split()
            for p in parts:
                if "%" in p:
                    try:
                        item["change_rate"] = float(p.replace("%", "").replace("+", ""))
                    except ValueError:
                        pass

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
    sort_by: str = "marketSum",
    limit: int = 20,
) -> dict:
    """ETF 전체 목록을 가져옵니다.

    Args:
        category: 필터 카테고리 (None=전체, "국내 시장지수", "해외 주식" 등)
        sort_by: 정렬 기준 ("marketSum", "quant", "threeMonthEarnRate", "nav")
        limit: 반환 개수 (기본 20, 최대 50)
    """
    resp = await fetch(ETF_LIST_API)
    # 네이버 ETF API는 EUC-KR 인코딩
    import json as _json
    data = _json.loads(resp.content.decode("euc-kr"))
    items = data.get("result", {}).get("etfItemList", [])
    if not items:
        return {"items": [], "total": 0, "categories": _ETF_TAB_NAMES}

    # 카테고리 필터
    if category:
        tab_code = None
        for code, name in _ETF_TAB_NAMES.items():
            if category in name or name in category:
                tab_code = code
                break
        if tab_code:
            items = [i for i in items if i.get("etfTabCode") == tab_code]

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


def _parse_float(text: str, default: float = 0.0) -> float:
    """쉼표·공백 등을 제거하고 float로 변환."""
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

    if summary:
        result["name"] = summary.get("CMP_KOR", "")
        result["name_eng"] = summary.get("CMP_ENG", "")
        result["base_index"] = summary.get("BASE_IDX_NM_KOR", "")
        result["issuer"] = summary.get("ISSUE_NM_KOR", "")
        result["etf_type"] = summary.get("ETF_TYP_SVC_NM", "")
        result["total_fee"] = _parse_float(summary.get("TOT_PAY", ""))

    if product:
        result["listing_date"] = product.get("LIST_DT", "")
        result["fund_type"] = product.get("FUND_TYP", "")
        result["fiscal_period"] = product.get("FIN_PRD", "")
        result["dividend_base"] = product.get("DIV_BASE_DT", "")
        result["lp_list"] = product.get("LP_NM_KOR", "")
        result["website"] = product.get("URL", "")

    if status:
        result["price"] = _parse_float(status.get("CLS_PRC", ""))
        result["price_change"] = _parse_float(status.get("PRC_CHG", ""))
        result["price_change_rate"] = _parse_float(status.get("ADJ_CHG", ""))
        result["year_high"] = _parse_float(status.get("YR_HIGH", ""))
        result["year_low"] = _parse_float(status.get("YR_LOW", ""))
        result["market_cap"] = _parse_float(status.get("MKT_VAL", ""))  # 억원
        result["nav"] = _parse_float(status.get("CLS_PRC", ""))  # fallback
        result["beta"] = _parse_float(status.get("YR_BETA", ""))
        result["foreign_rate"] = _parse_float(status.get("FRG_RT", ""))
        result["return_1m"] = _parse_float(status.get("ERN1", ""))
        result["return_3m"] = _parse_float(status.get("ERN3", ""))
        result["return_6m"] = _parse_float(status.get("ERN6", ""))
        result["return_1y"] = _parse_float(status.get("ERN12", ""))
        result["volume_20d_avg"] = _parse_float(status.get("AVG_TRD_QTY20", ""))

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

    # 실적 추정치
    res_data = _extract_json_var("res")
    if res_data and "yymm" in res_data:
        result["estimate_periods"] = res_data["yymm"]
        # wisereport res.data 인덱스: 0=매출액, 1=영업이익, 2=영업이익률(%)
        # 3번 이후는 페이지마다 다를 수 있어 안전한 것만 사용
        labels = ["매출액", "영업이익", "영업이익률"]
        estimates = {}
        for i, row in enumerate(res_data.get("data", [])):
            if i < len(labels):
                vals = {}
                for period_idx, period in enumerate(res_data["yymm"]):
                    vals[period] = row.get(str(period_idx + 1))
                estimates[labels[i]] = vals
        result["estimates"] = estimates

    return result


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
        return []

    results = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 5:
            continue

        title_a = cells[1].find("a")
        if not title_a:
            continue

        href = title_a.get("href", "")
        nid_match = re.search(r"nid=(\d+)", href)
        if not nid_match:
            continue

        results.append({
            "nid": nid_match.group(1),
            "stock": cells[0].get_text(strip=True),
            "title": title_a.get_text(strip=True),
            "broker": cells[2].get_text(strip=True),
            "date": cells[4].get_text(strip=True),
            "views": _parse_int(cells[5].get_text(strip=True)) if len(cells) > 5 else 0,
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

    results = []
    for row in soup.select("table tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue

        title_a = cells[0].find("a")
        if not title_a:
            continue

        title = title_a.get_text(strip=True)
        if not title:
            continue

        results.append({
            "title": title,
            "source": cells[1].get_text(strip=True),
            "date": cells[2].get_text(strip=True),
            "link": title_a.get("href", ""),
        })

    return results
