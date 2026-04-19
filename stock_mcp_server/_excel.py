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

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)

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
    df = pd.read_excel(file_path, sheet_name=sheet_name, dtype={"code": str})
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.zfill(6)
    return df


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
