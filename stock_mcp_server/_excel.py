"""Excel 파일 저장/조회 유틸리티.

사용자의 Downloads/kstock/ 폴더에 xlsx 파일을 저장하여
로컬 캐시 역할을 한다. DB 설치 없이도 반복 쿼리를 즉시 처리할 수 있다.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


def get_snapshot_dir() -> Path:
    """OS별 기본 저장 폴더. ~/Downloads/kstock/"""
    if sys.platform == "win32":
        base = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
    elif sys.platform == "darwin":
        base = Path.home() / "Downloads"
    else:
        base = Path.home() / "Downloads"

    folder = base / "kstock"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def generate_filename(prefix: str, ext: str = "xlsx") -> str:
    """타임스탬프 붙은 파일명 생성."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{ext}"


# 열 이름 → 한글 표시명. 파일을 여는 사람은 drawdown_pct 가 뭔지 모른다.
# 내부 필터(query_excel)는 영문 키를 그대로 받으므로 별칭으로 이어 준다.
COLUMN_LABELS_KO: dict[str, str] = {
    "code": "종목코드", "name": "종목명",
    "current_price": "현재가", "price": "현재가", "volume": "거래량",
    "high": "기간최고가", "high_date": "최고가일",
    "low": "기간최저가", "low_date": "최저가일",
    "drawdown_pct": "고점대비낙폭(%)", "recovery_pct": "저점대비회복(%)",
    "period_return_pct": "기간수익률(%)", "avg_volume": "평균거래량",
    "bars_count": "집계봉수",
    "per": "PER(배)", "pbr": "PBR(배)", "eps": "EPS(원)", "bps": "BPS(원)",
    "roe": "ROE(%)", "per_basis": "PER기준", "fin_period": "재무기준기간",
    "sector": "업종", "market_cap": "시가총액",
    "ma_phase": "이평배열", "rsi": "RSI", "vol_ratio": "거래량배수",
    "date": "날짜", "open": "시가", "close": "종가",
    "inst_net": "기관순매매", "foreign_net": "외국인순매매",
}

# 숫자 서식 — 정수형 금액/수량과 소수형 비율을 나눈다.
_INT_LIKE = ("현재가", "거래량", "기간최고가", "기간최저가", "평균거래량",
             "시가총액", "EPS(원)", "BPS(원)", "시가", "종가",
             "기관순매매", "외국인순매매", "집계봉수")
_DEC_LIKE = ("PER(배)", "PBR(배)", "배당수익률")
# 등락·수익률 계열은 부호가 곧 의미다. 국내 관례대로 오름 빨강 / 내림 파랑으로
# 칠한다(숫자 서식에 색을 넣으면 조건부 서식 없이 그대로 적용된다).
_SIGNED_PCT = ("고점대비낙폭(%)", "저점대비회복(%)", "기간수익률(%)", "ROE(%)",
               "기관순매매", "외국인순매매")
_SIGNED_FMT = '[Red]+#,##0.00;[Blue]-#,##0.00;0.00'
_SIGNED_INT_FMT = '[Red]+#,##0;[Blue]-#,##0;0'


# 한글 표시명 → 내부 키. 파일은 한글로 저장하지만 필터·정렬은 영문 키로 받는다
# (기존 사용자가 쓰던 per_max 같은 조건이 그대로 동작해야 한다).
LABEL_TO_KEY: dict[str, str] = {}
for _k, _v in COLUMN_LABELS_KO.items():
    LABEL_TO_KEY.setdefault(_v, _k)


# 열 이름을 미리 다 알 수 없다 — AI 가 정리한 표는 "등락률", "기관2일" 처럼
# 그때그때 다른 이름을 쓴다. 고정 목록으로만 판정하면 서식이 통째로 빠지므로,
# 이름의 힌트와 값의 성격을 함께 보고 정한다.
_PCT_HINTS = ("%", "률", "낙폭", "수익", "등락", "증감", "비율", "rsi", "RSI")
_MONEY_HINTS = ("가", "금액", "시총", "시가총액", "순매매", "거래량", "주식", "잔량", "봉")


def _guess_number_format(col_name, values) -> str | None:
    """열 하나에 맞는 숫자 서식. 판단이 안 서면 None(서식 없음)."""
    name = str(col_name)
    if name in _SIGNED_PCT:
        return _SIGNED_FMT
    if name in ("기관순매매", "외국인순매매"):
        return _SIGNED_INT_FMT
    if name in _INT_LIKE:
        return "#,##0"
    if name in _DEC_LIKE:
        return "#,##0.00"

    nums = [v for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    has_neg = any(v < 0 for v in nums)
    has_frac = any(float(v) != int(v) for v in nums)
    low = name.lower()
    pct_like = any(h.lower() in low for h in _PCT_HINTS)
    money_like = any(h in name for h in _MONEY_HINTS)

    if pct_like:
        return _SIGNED_FMT if has_neg else "#,##0.00"
    if has_neg and money_like:
        return _SIGNED_INT_FMT
    if has_frac:
        return "#,##0.00"
    if money_like or max(abs(v) for v in nums) >= 1000:
        return "#,##0"
    return None


def _chartable_columns(df) -> list[str]:
    """종목 간 비교가 되는 숫자 열. 가격·거래량처럼 자릿수가 제각각인 값은 뺀다."""
    out = []
    for c in df.columns:
        name = str(c)
        vals = [v for v in df[c]
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(vals) < 2:
            continue
        low = name.lower()
        if not any(h.lower() in low for h in _PCT_HINTS + ("per", "pbr", "roe", "배수")):
            continue
        if max(abs(v) for v in vals) > 100000:   # 금액 단위는 비교 차트에 안 맞는다
            continue
        out.append(name)
    return out


def _polish_sheet(ws, df) -> None:
    """열자마자 쓸 수 있는 상태로 만든다 — 필터·헤더고정·열너비·숫자서식.

    지금까지는 df.to_excel() 만 불러서, 파일을 열면 열 이름이 잘리고 숫자에
    자릿점이 없으며 정렬하려면 사용자가 직접 필터를 걸어야 했다.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    if ws.max_row < 1:
        return
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 첫 행 고정 + 자동 필터
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    for idx, col_name in enumerate(df.columns, start=1):
        letter = get_column_letter(idx)
        # 열 너비: 헤더와 값 중 긴 쪽에 맞추되 한글 폭을 감안해 넉넉히
        try:
            longest = max((len(str(v)) for v in df[col_name].head(200)), default=0)
        except Exception:
            longest = 0
        width = min(max(len(str(col_name)) * 1.6, longest * 1.3, 8), 30)
        ws.column_dimensions[letter].width = width

        try:
            fmt = _guess_number_format(col_name, list(df[col_name]))
        except Exception:
            fmt = None
        if fmt:
            for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                for cell in row:
                    cell.number_format = fmt


# 그래프로 그렸을 때 의미가 있는 열 — 종목 간 비교가 되는 값만 고른다.
# 가격·거래량처럼 종목마다 단위가 크게 다른 값은 한 차트에 섞으면 못 읽는다.
_CHART_CANDIDATES = (
    ("기간수익률(%)", "기간 수익률 비교 (%)"),
    ("고점대비낙폭(%)", "고점 대비 낙폭 (%)"),
    ("PER(배)", "PER 비교 (배)"),
    ("ROE(%)", "ROE 비교 (%)"),
    ("RSI", "RSI 비교"),
)
_FLOW_PAIR = ("기관순매매", "외국인순매매")


def _add_charts(wb, ws_data, df, sheet_name: str) -> None:
    """비교용 막대그래프를 '차트' 시트에 넣는다.

    숫자만 있는 표는 종목이 몇 개만 넘어가도 눈으로 비교가 안 된다. 파일을 여는
    사람이 바로 알아볼 수 있도록, 종목 간 비교가 되는 열만 골라 그린다.
    종목이 너무 많으면(20개 초과) 축이 뭉개져 오히려 안 보이므로 그리지 않는다.
    """
    from openpyxl.chart import BarChart, Reference

    n = len(df)
    if n < 2 or n > 20:
        return
    cols = list(df.columns)
    if "종목명" not in cols:
        return
    name_idx = cols.index("종목명") + 1

    picked: list[tuple[list[int], str]] = []

    # 기간수익률·낙폭은 늘 따라오는 기본 항목이다. 사용자가 일부러 고른 항목
    # (수급·재무·지표)이 있으면 그쪽을 먼저 그려야 원하는 그림이 나온다.
    if all(c in cols for c in _FLOW_PAIR):
        picked.append(([cols.index(c) + 1 for c in _FLOW_PAIR],
                       "기관·외국인 순매매 (20일 누적, 주)"))
    for label, title in (("PER(배)", "PER 비교 (배)"),
                         ("ROE(%)", "ROE 비교 (%)"),
                         ("RSI", "RSI 비교")):
        if len(picked) >= 2:
            break
        if label in cols:
            picked.append(([cols.index(label) + 1], title))
    for label, title in _CHART_CANDIDATES:
        if len(picked) >= 2:
            break
        if label in cols and not any(label in ttl for _, ttl in picked):
            picked.append(([cols.index(label) + 1], title))
    # 고정 목록에 없는 이름(AI 가 정리한 표)도 비교 가능한 열이면 그린다.
    if len(picked) < 2:
        used = {c for cs, _ in picked for c in cs}
        for name in _chartable_columns(df):
            if len(picked) >= 2:
                break
            idx = cols.index(name) + 1
            if idx in used:
                continue
            picked.append(([idx], f"{name} 비교"))
    if not picked:
        return

    ws = wb.create_sheet("차트")
    ws["A1"] = "아래 그래프는 왼쪽 데이터 시트를 그대로 읽습니다 — 값이 바뀌면 함께 바뀝니다."
    anchor_row = 3
    for value_cols, title in picked:
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = title
        chart.y_axis.title = None
        chart.x_axis.title = None
        chart.width = 24
        chart.height = 11
        for ci in value_cols:
            ref = Reference(ws_data, min_col=ci, min_row=1, max_row=n + 1)
            chart.add_data(ref, titles_from_data=True)
        cats = Reference(ws_data, min_col=name_idx, min_row=2, max_row=n + 1)
        chart.set_categories(cats)
        ws.add_chart(chart, f"A{anchor_row}")
        anchor_row += 23


def add_detail_sheet(wb, sheet_title: str, summary: list[tuple],
                     bars: list[dict], flows: list[dict]) -> None:
    """종목 한 개의 '흐름' 시트 — 요약 + 종가 추이 + 수급 추이 + 원자료.

    목록만 있는 파일은 "누가 후보인가"까지만 답한다. 그 종목이 어떻게 움직여
    왔는지, 누가 사고팔았는지를 같은 파일에서 보지 못하면 결국 다시 조회해야
    한다. 탭만 넘기며 훑고 다음에 뭘 볼지 정할 수 있게 만든다.
    """
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(sheet_title[:31])
    ws["A1"] = sheet_title
    ws["A1"].font = Font(bold=True, size=13)

    r = 3
    for label, value in summary:
        ws.cell(row=r, column=1, value=str(label)).font = Font(bold=True)
        ws.cell(row=r, column=2, value=value)
        r += 1
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 22

    # 원자료는 오른쪽에 둔다 — 차트가 왼쪽 위를 차지한다.
    head_row = 3
    cols = ["날짜", "종가", "거래량", "기관순매매", "외국인순매매"]
    for i, c in enumerate(cols):
        cell = ws.cell(row=head_row, column=5 + i, value=c)
        cell.font = Font(bold=True)
        ws.column_dimensions[get_column_letter(5 + i)].width = 14

    flow_by_date = {str(f.get("date", "")).replace(".", "-"): f for f in (flows or [])}
    n = 0
    for b in bars or []:
        day = str(b.get("date", ""))
        if len(day) == 8 and day.isdigit():
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        fl = flow_by_date.get(day, {})
        row = head_row + 1 + n
        ws.cell(row=row, column=5, value=day)
        ws.cell(row=row, column=6, value=b.get("close"))
        ws.cell(row=row, column=7, value=b.get("volume"))
        # 수급은 없는 날이 있다 — 0 으로 채우면 '순매매 0'과 구분되지 않는다.
        inst, forg = fl.get("institutional"), fl.get("foreign")
        if inst is not None:
            ws.cell(row=row, column=8, value=inst)
        if forg is not None:
            ws.cell(row=row, column=9, value=forg)
        n += 1
    if n < 2:
        return

    last = head_row + n
    for col, fmt in ((6, "#,##0"), (7, "#,##0"),
                     (8, _SIGNED_INT_FMT), (9, _SIGNED_INT_FMT)):
        for row in ws.iter_rows(min_row=head_row + 1, max_row=last,
                                min_col=col, max_col=col):
            for cell in row:
                cell.number_format = fmt

    cats = Reference(ws, min_col=5, min_row=head_row + 1, max_row=last)

    price = LineChart()
    price.title = "종가 추이"
    price.height, price.width = 8, 20
    price.add_data(Reference(ws, min_col=6, min_row=head_row, max_row=last),
                   titles_from_data=True)
    price.set_categories(cats)
    price.y_axis.title = None
    ws.add_chart(price, "A12")

    if any(ws.cell(row=x, column=8).value is not None
           for x in range(head_row + 1, last + 1)):
        flow_chart = BarChart()
        flow_chart.type = "col"
        flow_chart.grouping = "clustered"
        flow_chart.title = "기관·외국인 순매매 (주)"
        flow_chart.height, flow_chart.width = 8, 20
        flow_chart.add_data(Reference(ws, min_col=8, max_col=9,
                                      min_row=head_row, max_row=last),
                            titles_from_data=True)
        flow_chart.set_categories(cats)
        ws.add_chart(flow_chart, "A29")


def save_dataframe_to_excel(
    df: pd.DataFrame,
    file_path: Path | str,
    sheet_name: str = "Data",
    metadata: dict | None = None,
    source: str = "네이버 증권",
) -> str:
    """DataFrame을 Excel로 저장. 메타데이터 시트도 함께 기록.

    종목코드 같은 선행 0이 있는 문자열은 엑셀이 자동으로 숫자 변환하지 않도록
    강제로 문자열 타입을 유지.

    Args:
        df: 저장할 DataFrame
        file_path: 저장 경로
        sheet_name: 메인 시트 이름
        metadata: 추가 정보 (수집 시간, 소스 등)
        source: 데이터 소스 이름 (KR: "네이버 증권", US: "Yahoo Finance" 등)

    Returns:
        저장된 파일의 절대 경로 (문자열)
    """
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    df = df.copy()
    # 종목코드 컬럼은 문자열로 강제 (선행 0 보존)
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.zfill(6)

    # 영문 열 이름을 한글로 바꿔 저장한다(파일을 여는 사람 기준).
    # query_excel 은 영문 키를 계속 받으므로 필터 호환은 별칭으로 처리한다.
    df = df.rename(columns={c: COLUMN_LABELS_KO.get(c, c) for c in df.columns})

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        _polish_sheet(writer.sheets[sheet_name], df)
        try:
            _add_charts(writer.book, writer.sheets[sheet_name], df, sheet_name)
        except Exception:
            pass    # 그래프는 부가 기능이다 — 실패해도 데이터 저장은 살린다

        # 메타데이터 시트
        meta_rows = [
            {"key": "수집 시간", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            {"key": "소스", "value": source},
            {"key": "행 수", "value": len(df)},
            {"key": "컬럼 수", "value": len(df.columns)},
        ]
        if metadata:
            for k, v in metadata.items():
                meta_rows.append({"key": k, "value": str(v)})

        meta_df = pd.DataFrame(meta_rows)
        meta_df.to_excel(writer, sheet_name="Metadata", index=False)

    return str(file_path.resolve())


def load_excel(file_path: Path | str, sheet_name: str | int = 0) -> pd.DataFrame:
    """Excel 파일을 DataFrame으로 로드.

    종목코드는 문자열로 강제 로드 (선행 0 보존).
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")
    # 파일은 한글 헤더로 저장되지만 내부 처리는 영문 키로 통일한다.
    df = pd.read_excel(file_path, sheet_name=sheet_name,
                       dtype={"code": str, "종목코드": str})
    df = df.rename(columns={c: LABEL_TO_KEY.get(c, c) for c in df.columns})
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.zfill(6)
    return df


def unknown_filter_columns(df: pd.DataFrame, filters: dict | None) -> list[str]:
    """필터에 적혔지만 이 파일에 없는 컬럼 목록.

    없는 컬럼은 조용히 무시되므로, 알려주지 않으면 "조건이 걸린 결과"로 읽힌다.
    PER 컬럼이 없는 스냅샷에 per_max=10 을 걸면 전 종목이 그대로 나오는데,
    사용자는 그게 저PER 목록인 줄 안다.
    """
    cols = set(df.columns)
    unknown: list[str] = []
    for key, cond in (filters or {}).items():
        base = key
        if not isinstance(cond, dict) and (key.endswith("_max") or key.endswith("_min")):
            base = key[:-4]
        if base not in cols:
            unknown.append(key)
    return unknown


def apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """DataFrame에 필터 조건을 적용.

    filters 형식:
        {"per": {"max": 10, "min": 0}, "drawdown_pct": {"max": -30}}
        {"foreign_net_5d": {"min": 0}}

    또는 간단한 형태:
        {"per_max": 10, "pbr_max": 1.5}  ← 이것도 지원
    """
    result = df.copy()

    for key, cond in filters.items():
        if isinstance(cond, dict):
            # {"per": {"max": 10, "min": 0}} 형태
            if key not in result.columns:
                continue
            if "min" in cond and cond["min"] is not None:
                result = result[result[key] >= cond["min"]]
            if "max" in cond and cond["max"] is not None:
                result = result[result[key] <= cond["max"]]
            if "equals" in cond:
                result = result[result[key] == cond["equals"]]
        else:
            # 간단한 형태: "per_max": 10
            if key.endswith("_max"):
                col = key[:-4]
                if col in result.columns:
                    result = result[result[col] <= cond]
            elif key.endswith("_min"):
                col = key[:-4]
                if col in result.columns:
                    result = result[result[col] >= cond]
            elif key in result.columns:
                result = result[result[key] == cond]

    return result
