from pathlib import Path

import pytest

from scripts.apply_stage_xlsx_translations import (
    ApplyError,
    XlsxTable,
    _choose_column,
    _find_table,
)


def test_제미니_열은_공백이_있어도_최우선이다() -> None:
    index, kind = _choose_column(["원문", "구글번역", "제미니 번역", "웹번역"])
    assert (index, kind) == (2, "gemini")


def test_고정슬롯은_번역_열을_fallback으로_선택한다() -> None:
    index, kind = _choose_column(["순서", "내용", "번역", "한글폰트"])
    assert (index, kind) == (2, "fixed-slot")


def test_제미니가_없는_분기는_웹번역을_선택한다() -> None:
    index, kind = _choose_column(["원문", "구글번역", "웹번역: 한줄 길이"])
    assert (index, kind) == (2, "web-fallback")


def test_원문이_다르면_조용히_덮어쓰지_않는다() -> None:
    table = XlsxTable(
        path=Path("stage.xlsx"),
        sheet_name="ID00003",
        source_column=0,
        translation_column=1,
        translation_kind="gemini",
        compiled_column=None,
        length_column=None,
        byte_limit_column=None,
        rows={2: ("일본어", "한국어", "", None, None)},
    )
    with pytest.raises(ApplyError):
        table.row(2, "다른 원문")


def test_같은_파일의_시트와_행을_원문으로_고른다() -> None:
    table = XlsxTable(
        path=Path("stage.xlsx"),
        sheet_name="ID00003",
        source_column=0,
        translation_column=1,
        translation_kind="gemini",
        compiled_column=None,
        length_column=None,
        byte_limit_column=None,
        rows={2: ("일본어", "한국어", "", None, None)},
    )
    tables = {"stage.xlsx": [table]}
    assert _find_table(tables, "folder/stage.xlsx", 2, "일본어") is table
