"""외부 패키지 없이 XLSX의 셀 값을 읽는 작은 판독기.

번역 원고에서 필요한 데이터만 읽는다. 수식은 수식 자체를 계산하지 않고
XLSX에 저장된 마지막 계산 결과(`v`)를 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import posixpath
import re
from typing import Iterator
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOCUMENT_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE = re.compile(r"^([A-Za-z]+)([1-9][0-9]*)$")


class XlsxError(RuntimeError):
    """XLSX를 안전하게 해석할 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class XlsxSheet:
    """워크북 안의 워크시트 메타데이터."""

    name: str
    part_name: str
    state: str = "visible"


@dataclass(frozen=True, slots=True)
class XlsxRow:
    """원래 행 번호와, 빈 셀을 포함해 열 위치가 보존된 값 목록."""

    number: int
    values: tuple[str, ...]

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __getitem__(self, index: int) -> str:
        return self.values[index]


@dataclass(frozen=True, slots=True)
class XlsxWorkbook:
    """반복해서 시트 행을 스트리밍할 수 있는 XLSX 워크북."""

    path: Path
    file_sha256: str
    sheets: tuple[XlsxSheet, ...]
    _shared_strings: tuple[str, ...] = field(repr=False)

    @property
    def sheet_names(self) -> tuple[str, ...]:
        return tuple(sheet.name for sheet in self.sheets)

    def iter_rows(self, sheet: str | XlsxSheet) -> Iterator[XlsxRow]:
        """지정한 시트의 셀 값을 행 단위로 읽는다."""

        selected = sheet
        if isinstance(sheet, str):
            matches = [item for item in self.sheets if item.name == sheet]
            if not matches:
                raise KeyError(f"워크시트를 찾을 수 없습니다: {sheet}")
            selected = matches[0]

        try:
            with ZipFile(self.path) as archive:
                _require_safe_member(archive, selected.part_name)
                with archive.open(selected.part_name) as stream:
                    previous_row = 0
                    for _, element in ET.iterparse(stream, events=("end",)):
                        if element.tag != f"{{{_MAIN_NS}}}row":
                            continue
                        raw_number = element.get("r")
                        row_number = (
                            _positive_int(raw_number, "행 번호")
                            if raw_number
                            else previous_row + 1
                        )
                        previous_row = row_number
                        values: list[str] = []
                        next_column = 0
                        for cell in element.findall(f"{{{_MAIN_NS}}}c"):
                            reference = cell.get("r", "")
                            column = (
                                _column_index(reference)
                                if reference
                                else next_column
                            )
                            if column < next_column:
                                raise XlsxError(
                                    f"셀 열 순서가 올바르지 않습니다: "
                                    f"{selected.name}!{reference or row_number}"
                                )
                            if column >= len(values):
                                values.extend("" for _ in range(column + 1 - len(values)))
                            values[column] = _cell_value(cell, self._shared_strings)
                            next_column = column + 1
                        yield XlsxRow(row_number, tuple(values))
                        element.clear()
        except BadZipFile as error:
            raise XlsxError(f"손상되었거나 XLSX가 아닌 파일입니다: {self.path}") from error
        except ET.ParseError as error:
            raise XlsxError(f"XLSX XML을 해석할 수 없습니다: {self.path}") from error


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """파일의 SHA-256을 소문자 16진 문자열로 반환한다."""

    digest = sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def open_xlsx(path: str | Path) -> XlsxWorkbook:
    """XLSX 메타데이터와 공유 문자열을 읽어 워크북을 연다."""

    workbook_path = Path(path).resolve()
    if not workbook_path.is_file():
        raise FileNotFoundError(f"XLSX 파일을 찾을 수 없습니다: {workbook_path}")

    try:
        with ZipFile(workbook_path) as archive:
            workbook_part = _find_workbook_part(archive)
            relationship_part = _relationship_part(workbook_part)
            _require_safe_member(archive, workbook_part)
            _require_safe_member(archive, relationship_part)

            relationships = _read_relationships(
                archive, relationship_part, workbook_part
            )
            workbook_root = _parse_xml_member(archive, workbook_part)
            sheets: list[XlsxSheet] = []
            for node in workbook_root.findall(
                f".//{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"
            ):
                name = node.get("name", "")
                relationship_id = node.get(f"{{{_DOCUMENT_REL_NS}}}id", "")
                if not name or not relationship_id:
                    raise XlsxError("워크시트 이름 또는 관계 ID가 비어 있습니다.")
                part_name = relationships.get(relationship_id)
                if part_name is None:
                    raise XlsxError(
                        f"워크시트 관계를 찾을 수 없습니다: {name} ({relationship_id})"
                    )
                _require_safe_member(archive, part_name)
                sheets.append(
                    XlsxSheet(name, part_name, node.get("state", "visible"))
                )

            shared_part = _find_shared_strings_part(
                archive, relationship_part, workbook_part
            )
            shared_strings = (
                _read_shared_strings(archive, shared_part)
                if shared_part is not None
                else ()
            )
    except BadZipFile as error:
        raise XlsxError(f"손상되었거나 XLSX가 아닌 파일입니다: {workbook_path}") from error
    except ET.ParseError as error:
        raise XlsxError(f"XLSX XML을 해석할 수 없습니다: {workbook_path}") from error

    if not sheets:
        raise XlsxError(f"워크시트가 하나도 없습니다: {workbook_path}")
    return XlsxWorkbook(
        path=workbook_path,
        file_sha256=file_sha256(workbook_path),
        sheets=tuple(sheets),
        _shared_strings=tuple(shared_strings),
    )


def read_workbook(path: str | Path) -> XlsxWorkbook:
    """`open_xlsx`의 읽기 쉬운 별칭."""

    return open_xlsx(path)


def _find_workbook_part(archive: ZipFile) -> str:
    if "xl/workbook.xml" in archive.namelist():
        return "xl/workbook.xml"

    root = _parse_xml_member(archive, "[Content_Types].xml")
    for override in root:
        content_type = override.get("ContentType", "")
        if "spreadsheetml.sheet.main+xml" in content_type or (
            "spreadsheetml.template.main+xml" in content_type
        ):
            part_name = override.get("PartName", "").lstrip("/")
            if part_name:
                return _normalize_member(part_name)
    raise XlsxError("워크북 XML 위치를 찾을 수 없습니다.")


def _relationship_part(part_name: str) -> str:
    directory, filename = posixpath.split(part_name)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _read_relationships(
    archive: ZipFile, relationship_part: str, source_part: str
) -> dict[str, str]:
    root = _parse_xml_member(archive, relationship_part)
    relationships: dict[str, str] = {}
    for relation in root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if relation.get("TargetMode") == "External":
            continue
        relation_id = relation.get("Id", "")
        target = relation.get("Target", "")
        if relation_id and target:
            relationships[relation_id] = _resolve_target(source_part, target)
    return relationships


def _find_shared_strings_part(
    archive: ZipFile, relationship_part: str, source_part: str
) -> str | None:
    root = _parse_xml_member(archive, relationship_part)
    for relation in root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if relation.get("TargetMode") == "External":
            continue
        if relation.get("Type", "").endswith("/sharedStrings"):
            part = _resolve_target(source_part, relation.get("Target", ""))
            _require_safe_member(archive, part)
            return part
    if "xl/sharedStrings.xml" in archive.namelist():
        return "xl/sharedStrings.xml"
    return None


def _read_shared_strings(archive: ZipFile, part_name: str) -> tuple[str, ...]:
    strings: list[str] = []
    with archive.open(part_name) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag == f"{{{_MAIN_NS}}}si":
                strings.append(_rich_text(element))
                element.clear()
    return tuple(strings)


def _cell_value(cell: ET.Element, shared_strings: tuple[str, ...]) -> str:
    cell_type = cell.get("t", "n")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{_MAIN_NS}}}is")
        return _rich_text(inline) if inline is not None else ""

    value_node = cell.find(f"{{{_MAIN_NS}}}v")
    raw_value = value_node.text if value_node is not None and value_node.text else ""
    if cell_type == "s":
        if raw_value == "":
            return ""
        try:
            index = int(raw_value)
            if index < 0:
                raise IndexError
            return shared_strings[index]
        except (ValueError, IndexError) as error:
            raise XlsxError(f"공유 문자열 번호가 올바르지 않습니다: {raw_value}") from error
    if cell_type == "b":
        if raw_value == "1":
            return "TRUE"
        if raw_value == "0":
            return "FALSE"
    # n, str, e, d 및 수식의 캐시 결과는 원문 문자열 그대로 보존한다.
    return raw_value


def _rich_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(
        node.text or "" for node in element.iter(f"{{{_MAIN_NS}}}t")
    )


def _parse_xml_member(archive: ZipFile, member: str) -> ET.Element:
    _require_safe_member(archive, member)
    data = archive.read(member)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise XlsxError(f"허용하지 않는 XML 선언이 있습니다: {member}")
    return ET.fromstring(data)


def _require_safe_member(archive: ZipFile, member: str) -> None:
    normalized = _normalize_member(member)
    if normalized != member or member not in archive.namelist():
        raise XlsxError(f"XLSX 내부 파일을 찾을 수 없거나 경로가 안전하지 않습니다: {member}")


def _normalize_member(member: str) -> str:
    decoded = unquote(member).replace("\\", "/").lstrip("/")
    normalized = posixpath.normpath(decoded)
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        raise XlsxError(f"XLSX 내부 경로가 안전하지 않습니다: {member}")
    return normalized


def _resolve_target(source_part: str, target: str) -> str:
    if not target:
        raise XlsxError("빈 XLSX 관계 대상입니다.")
    decoded = unquote(target).replace("\\", "/")
    if decoded.startswith("/"):
        return _normalize_member(decoded)
    return _normalize_member(posixpath.join(posixpath.dirname(source_part), decoded))


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference)
    if not match:
        raise XlsxError(f"셀 주소가 올바르지 않습니다: {reference}")
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _positive_int(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise XlsxError(f"{label}가 정수가 아닙니다: {value}") from error
    if result < 1:
        raise XlsxError(f"{label}가 1보다 작습니다: {value}")
    return result
