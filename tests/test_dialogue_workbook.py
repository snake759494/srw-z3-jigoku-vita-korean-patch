from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from siok_patch.dialogue_workbook import (
    DialogueWorkbookError,
    load_dialogue_workbook,
)


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _inline(reference: str, value: str) -> str:
    return (
        f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
        f"{escape(value)}</t></is></c>"
    )


def _formula(reference: str, cached_value: str) -> str:
    return (
        f'<c r="{reference}" t="str"><f>CONCAT("x")</f>'
        f"<v>{escape(cached_value)}</v></c>"
    )


def _number_formula(reference: str, cached_value: str) -> str:
    return f'<c r="{reference}"><f>1+1</f><v>{cached_value}</v></c>'


def _sheet(*rows: tuple[int, str]) -> str:
    body = "".join(f'<row r="{number}">{cells}</row>' for number, cells in rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData>{body}</sheetData></worksheet>'
    )


def _make_xlsx(
    path: Path,
    sheets: list[tuple[str, str]],
    *,
    macro: bool = False,
    external_link: bool = False,
) -> None:
    sheet_nodes = []
    relationships = []
    overrides = []
    for index, (name, _) in enumerate(sheets, start=1):
        sheet_nodes.append(
            f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        relationships.append(
            '<Relationship '
            f'Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    if external_link:
        relationships.append(
            '<Relationship Id="rIdExternal" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" '
            'Target="https://example.invalid/source.xlsx" TargetMode="External"/>'
        )

    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{_MAIN_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheet_nodes)}</sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(relationships)}</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{"".join(overrides)}</Types>'
    )

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        for index, (_, xml) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
        if macro:
            archive.writestr("xl/vbaProject.bin", b"not executable test data")


def _normal_sheet(*data_rows: tuple[int, str]) -> str:
    header = _inline("A1", "원문") + _inline("B1", "한글폰트로")
    return _sheet((1, header), *data_rows)


class DialogueWorkbookTest(unittest.TestCase):
    def test_전각공백_행을_보존하고_양끝_차이만_경고한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dialogue.xlsx"
            xml = _sheet(
                (1, _inline("A1", "원문") + _inline("B1", "한글폰트로") + _inline("C1", "길이")),
                (2, _inline("A2", "原文") + _inline("B2", "置換")),
                (
                    3,
                    _inline("A3", "　　本文　")
                    + _inline("B3", "　置換　"),
                ),
                (4, _number_formula("C4", "2")),
                (5, _inline("A5", "　") + _inline("B5", "　")),
            )
            _make_xlsx(path, [("어떤 시트명", xml)])

            result = load_dialogue_workbook(path, ("原文", "　本文", "　"))

            self.assertEqual(result.sheet_name, "어떤 시트명")
            self.assertEqual(result.header_row, 1)
            self.assertEqual(result.row_count, 3)
            self.assertEqual(result.edge_space_mismatch_count, 1)
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual(result.rows[1].source_text, "　　本文　")
            self.assertEqual(result.rows[1].source_match, "edge_fullwidth_space")
            self.assertEqual(result.rows[2].source_text, "　")
            self.assertEqual(result.rows[2].workbook_row, 5)

    def test_문자열과_한글폰트_별칭을_인식한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.xlsx"
            xml = _sheet(
                (1, _inline("A1", "문자열") + _inline("D1", "한글폰트")),
                (2, _inline("A2", "台詞") + _inline("D2", "置換")),
            )
            _make_xlsx(path, [("Sheet1", xml)])

            result = load_dialogue_workbook(path, ("台詞",))

            self.assertEqual(result.rows[0].replacement_text, "置換")
            self.assertEqual(result.edge_space_mismatch_count, 0)
            self.assertEqual(result.source_header, "문자열")
            self.assertEqual(result.replacement_header, "한글폰트")
            self.assertEqual(result.source_column, 1)
            self.assertEqual(result.replacement_column, 4)

    def test_두_선택열_중_한쪽만_비면_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "one-empty.xlsx"
            _make_xlsx(
                path,
                [("Sheet1", _normal_sheet((2, _inline("A2", "原文"))))],
            )

            with self.assertRaisesRegex(DialogueWorkbookError, "2행"):
                load_dialogue_workbook(path, ("原文",))

    def test_의미가_같은_헤더가_둘이면_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.xlsx"
            xml = _sheet(
                (
                    1,
                    _inline("A1", "원문")
                    + _inline("B1", "문자열")
                    + _inline("C1", "한글폰트"),
                ),
                (2, _inline("A2", "原文") + _inline("C2", "置換")),
            )
            _make_xlsx(path, [("Sheet1", xml)])

            with self.assertRaisesRegex(DialogueWorkbookError, "중복"):
                load_dialogue_workbook(path, ("原文",))

    def test_선택된_열의_수식은_캐시값이_있어도_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "formula.xlsx"
            xml = _normal_sheet(
                (2, _formula("A2", "原文") + _inline("B2", "置換"))
            )
            _make_xlsx(path, [("Sheet1", xml)])

            with self.assertRaisesRegex(DialogueWorkbookError, "수식"):
                load_dialogue_workbook(path, ("原文",))

    def test_매크로와_외부링크를_거부한다(self):
        with TemporaryDirectory() as directory:
            for name, options, expected_message in (
                ("macro.xlsx", {"macro": True}, "매크로"),
                ("external.xlsx", {"external_link": True}, "외부 링크"),
            ):
                with self.subTest(name=name):
                    path = Path(directory) / name
                    xml = _normal_sheet(
                        (2, _inline("A2", "原文") + _inline("B2", "置換"))
                    )
                    _make_xlsx(path, [("Sheet1", xml)], **options)

                    with self.assertRaisesRegex(
                        DialogueWorkbookError, expected_message
                    ):
                        load_dialogue_workbook(path, ("原文",))

    def test_인식되는_시트가_여러개면_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "two-sheets.xlsx"
            xml = _normal_sheet(
                (2, _inline("A2", "原文") + _inline("B2", "置換"))
            )
            _make_xlsx(path, [("대사1", xml), ("대사2", xml)])

            with self.assertRaisesRegex(DialogueWorkbookError, "여러 개"):
                load_dialogue_workbook(path, ("原文",))

    def test_원본_행수와_순서가_다르면_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "order.xlsx"
            xml = _normal_sheet(
                (2, _inline("A2", "一") + _inline("B2", "壱")),
                (3, _inline("A3", "二") + _inline("B3", "弐")),
            )
            _make_xlsx(path, [("Sheet1", xml)])

            with self.assertRaisesRegex(DialogueWorkbookError, "행 수"):
                load_dialogue_workbook(path, ("一",))
            with self.assertRaisesRegex(DialogueWorkbookError, "1번"):
                load_dialogue_workbook(path, ("二", "一"))

    def test_cp932로_인코딩할수없는_문자는_행번호와_함께_실패한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "encoding.xlsx"
            xml = _normal_sheet(
                (2, _inline("A2", "原文") + _inline("B2", "置換😀"))
            )
            _make_xlsx(path, [("Sheet1", xml)])

            with self.assertRaisesRegex(DialogueWorkbookError, "2행.*CP932"):
                load_dialogue_workbook(path, ("原文",))


if __name__ == "__main__":
    unittest.main()
