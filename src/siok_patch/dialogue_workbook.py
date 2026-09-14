"""대사 XLSX를 명시적인 원본 대사 행과 대조해 안전하게 읽는다.

이 모듈은 파일명이나 시트명에서 게임 자산 ID를 추론하지 않는다. 호출자가
검증된 원본 대사 행을 전달해야 하며, 워크북은 그 행과 순서대로 대조된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from .xlsx_reader import XlsxError, XlsxRow, XlsxSheet, open_xlsx


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SOURCE_HEADERS = frozenset(("원문", "문자열"))
_REPLACEMENT_HEADERS = frozenset(("한글폰트로", "한글폰트"))
_HEADER_SCAN_LIMIT = 30
_CELL_REFERENCE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")


class DialogueWorkbookError(ValueError):
    """대사 워크북을 모호함 없이 안전하게 사용할 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class DialogueRow:
    """검증을 마친 대사 워크북의 한 데이터 행."""

    dialogue_index: int
    workbook_row: int
    source_text: str
    replacement_text: str
    source_match: str


@dataclass(frozen=True, slots=True)
class DialogueWorkbookData:
    """한 시트만 인식되고 원본 행 대응 검사를 통과한 워크북."""

    path: Path
    file_sha256: str
    sheet_name: str
    header_row: int
    source_header: str
    replacement_header: str
    source_column: int
    replacement_column: int
    rows: tuple[DialogueRow, ...]
    edge_space_mismatch_count: int
    warnings: tuple[str, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class _RecognizedSheet:
    sheet: XlsxSheet
    header_row: int
    source_column: int
    replacement_column: int
    source_header: str
    replacement_header: str


def load_dialogue_workbook(
    path: str | Path,
    expected_source_lines: Sequence[str],
) -> DialogueWorkbookData:
    """대사 XLSX를 읽고 원본 대사 행의 개수와 순서를 검증한다.

    정확한 원문 일치를 우선한다. 불일치는 양끝의 전각 공백(U+3000) 개수만
    다를 때에만 허용하며 경고와 개수로 반환한다.
    """

    workbook_path = Path(path).resolve()
    expected = _validate_expected_lines(expected_source_lines)

    try:
        _reject_macros_and_external_links(workbook_path)
        workbook = open_xlsx(workbook_path)
        recognized = _recognize_sheets(workbook)
        if not recognized:
            raise DialogueWorkbookError(
                "원문/문자열 및 한글폰트로/한글폰트 헤더가 있는 시트를 "
                f"찾을 수 없습니다: {workbook_path}"
            )
        if len(recognized) > 1:
            names = ", ".join(item.sheet.name for item in recognized)
            raise DialogueWorkbookError(
                f"대사 시트가 여러 개 인식되었습니다: {names}"
            )

        selected = recognized[0]
        _reject_selected_column_formulas(workbook_path, selected)
        raw_rows = _read_data_rows(workbook, selected)
        if len(raw_rows) != len(expected):
            raise DialogueWorkbookError(
                f"대사 행 수가 다릅니다: 워크북 {len(raw_rows)}행, "
                f"기대 원본 {len(expected)}행"
            )

        rows: list[DialogueRow] = []
        warnings: list[str] = []
        mismatch_count = 0
        for dialogue_index, ((workbook_row, source, replacement), wanted) in enumerate(
            zip(raw_rows, expected), start=1
        ):
            if source == wanted:
                source_match = "exact"
            elif _edge_fullwidth_space_equal(source, wanted):
                source_match = "edge_fullwidth_space"
                mismatch_count += 1
                warnings.append(
                    f"대사 {dialogue_index}번(XLSX {workbook_row}행): "
                    "원문 양끝 전각 공백 개수만 다릅니다."
                )
            else:
                raise DialogueWorkbookError(
                    f"대사 {dialogue_index}번(XLSX {workbook_row}행)의 원문이 "
                    "기대 원본과 다릅니다."
                )
            rows.append(
                DialogueRow(
                    dialogue_index=dialogue_index,
                    workbook_row=workbook_row,
                    source_text=source,
                    replacement_text=replacement,
                    source_match=source_match,
                )
            )

        return DialogueWorkbookData(
            path=workbook_path,
            file_sha256=workbook.file_sha256,
            sheet_name=selected.sheet.name,
            header_row=selected.header_row,
            source_header=selected.source_header,
            replacement_header=selected.replacement_header,
            source_column=selected.source_column + 1,
            replacement_column=selected.replacement_column + 1,
            rows=tuple(rows),
            edge_space_mismatch_count=mismatch_count,
            warnings=tuple(warnings),
        )
    except DialogueWorkbookError:
        raise
    except (BadZipFile, ET.ParseError, XlsxError, OSError) as error:
        raise DialogueWorkbookError(
            f"대사 워크북을 읽을 수 없습니다: {workbook_path} ({error})"
        ) from error


def _validate_expected_lines(lines: Sequence[str]) -> tuple[str, ...]:
    if isinstance(lines, (str, bytes)) or not isinstance(lines, Sequence):
        raise DialogueWorkbookError("기대 원본 대사는 문자열 목록이어야 합니다.")
    result = tuple(lines)
    for index, line in enumerate(result, start=1):
        if not isinstance(line, str):
            raise DialogueWorkbookError(
                f"기대 원본 대사 {index}번이 문자열이 아닙니다."
            )
    return result


def _recognize_sheets(workbook) -> list[_RecognizedSheet]:
    recognized: list[_RecognizedSheet] = []
    for sheet in workbook.sheets:
        for inspected, row in enumerate(workbook.iter_rows(sheet), start=1):
            if inspected > _HEADER_SCAN_LIMIT:
                break
            source_columns = _header_columns(row, _SOURCE_HEADERS)
            replacement_columns = _header_columns(row, _REPLACEMENT_HEADERS)
            if not source_columns and not replacement_columns:
                continue
            if len(source_columns) > 1:
                raise DialogueWorkbookError(
                    f"{sheet.name} 시트 {row.number}행에 원문 계열 헤더가 "
                    "중복되었습니다."
                )
            if len(replacement_columns) > 1:
                raise DialogueWorkbookError(
                    f"{sheet.name} 시트 {row.number}행에 한글폰트 계열 헤더가 "
                    "중복되었습니다."
                )
            if not source_columns or not replacement_columns:
                missing = (
                    "원문/문자열"
                    if not source_columns
                    else "한글폰트로/한글폰트"
                )
                raise DialogueWorkbookError(
                    f"{sheet.name} 시트 {row.number}행에 {missing} 헤더가 없습니다."
                )
            recognized.append(
                _RecognizedSheet(
                    sheet=sheet,
                    header_row=row.number,
                    source_column=source_columns[0],
                    replacement_column=replacement_columns[0],
                    source_header=_normalized_header(row.values[source_columns[0]]),
                    replacement_header=_normalized_header(
                        row.values[replacement_columns[0]]
                    ),
                )
            )
            break
    return recognized


def _header_columns(row: XlsxRow, aliases: frozenset[str]) -> list[int]:
    result: list[int] = []
    for index, value in enumerate(row.values):
        if _normalized_header(value) in aliases:
            result.append(index)
    return result


def _normalized_header(value: str) -> str:
    return value.lstrip("\ufeff").strip()


def _read_data_rows(
    workbook,
    selected: _RecognizedSheet,
) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for row in workbook.iter_rows(selected.sheet):
        if row.number <= selected.header_row:
            continue
        source = _cell(row, selected.source_column)
        replacement = _cell(row, selected.replacement_column)
        if source == "" and replacement == "":
            continue
        if source == "" or replacement == "":
            missing = "원문" if source == "" else "한글폰트 치환문"
            raise DialogueWorkbookError(
                f"XLSX {row.number}행의 {missing}만 비어 있습니다."
            )
        _require_cp932(source, row.number, "원문")
        _require_cp932(replacement, row.number, "한글폰트 치환문")
        rows.append((row.number, source, replacement))
    return rows


def _cell(row: XlsxRow, column: int) -> str:
    return row.values[column] if column < len(row.values) else ""


def _require_cp932(value: str, row_number: int, field_name: str) -> None:
    try:
        value.encode("cp932", errors="strict")
    except UnicodeEncodeError as error:
        character = value[error.start : error.end]
        raise DialogueWorkbookError(
            f"XLSX {row_number}행 {field_name}의 {character!r} 문자는 "
            "CP932로 인코딩할 수 없습니다."
        ) from error


def _edge_fullwidth_space_equal(actual: str, expected: str) -> bool:
    return actual != expected and actual.strip("\u3000") == expected.strip("\u3000")


def _reject_macros_and_external_links(path: Path) -> None:
    if path.suffix.lower() == ".xlsm":
        raise DialogueWorkbookError(f"매크로 사용 워크북은 허용하지 않습니다: {path}")
    try:
        with ZipFile(path) as archive:
            names = tuple(info.filename.replace("\\", "/") for info in archive.infolist())
            lowered = tuple(name.casefold() for name in names)
            if any(name.endswith("/vbaproject.bin") for name in lowered):
                raise DialogueWorkbookError("XLSX에 VBA 매크로가 포함되어 있습니다.")
            if any(name.startswith("xl/externallinks/") for name in lowered):
                raise DialogueWorkbookError("XLSX에 외부 링크가 포함되어 있습니다.")

            if "[Content_Types].xml" in names:
                content_types = archive.read("[Content_Types].xml").lower()
                if b"vbaproject" in content_types or b"macroenabled" in content_types:
                    raise DialogueWorkbookError("XLSX에 매크로 콘텐츠 형식이 있습니다.")
                if b"externallink" in content_types:
                    raise DialogueWorkbookError("XLSX에 외부 링크 콘텐츠가 있습니다.")

            for name in names:
                if not name.casefold().endswith(".rels"):
                    continue
                root = ET.fromstring(archive.read(name))
                for relation in root.findall(
                    f"{{{_PACKAGE_REL_NS}}}Relationship"
                ):
                    relation_type = relation.get("Type", "").casefold()
                    target = relation.get("Target", "").replace("\\", "/").casefold()
                    target_mode = relation.get("TargetMode", "").casefold()
                    if (
                        target_mode == "external"
                        or relation_type.endswith("/externallink")
                        or "externallinks/" in target
                    ):
                        raise DialogueWorkbookError(
                            f"XLSX 관계 파일 {name}에 외부 링크가 있습니다."
                        )
    except FileNotFoundError:
        raise
    except BadZipFile as error:
        raise DialogueWorkbookError(f"XLSX ZIP 구조가 손상되었습니다: {path}") from error


def _reject_selected_column_formulas(
    path: Path,
    selected: _RecognizedSheet,
) -> None:
    selected_columns = frozenset(
        (selected.source_column, selected.replacement_column)
    )
    try:
        with ZipFile(path) as archive:
            with archive.open(selected.sheet.part_name) as stream:
                previous_row = 0
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag != f"{{{_MAIN_NS}}}row":
                        continue
                    raw_row_number = element.get("r", "")
                    row_number = (
                        int(raw_row_number)
                        if raw_row_number
                        else previous_row + 1
                    )
                    previous_row = row_number
                    next_column = 0
                    for cell in element.findall(f"{{{_MAIN_NS}}}c"):
                        reference = cell.get("r", "")
                        column = (
                            _column_index(reference)
                            if reference
                            else next_column
                        )
                        next_column = column + 1
                        if (
                            row_number >= selected.header_row
                            and column in selected_columns
                            and cell.find(f"{{{_MAIN_NS}}}f") is not None
                        ):
                            raise DialogueWorkbookError(
                                f"선택된 원문/한글폰트 열의 XLSX {row_number}행에 "
                                "수식이 있습니다. 캐시값은 입력으로 사용하지 않습니다."
                            )
                    element.clear()
    except KeyError as error:
        raise DialogueWorkbookError(
            f"인식된 시트 XML을 찾을 수 없습니다: {selected.sheet.part_name}"
        ) from error


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if not match:
        raise DialogueWorkbookError(f"셀 주소가 올바르지 않습니다: {reference}")
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1
