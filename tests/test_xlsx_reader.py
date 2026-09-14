from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from siok_patch.translation_io import import_archive, read_tsv
from siok_patch.xlsx_reader import XlsxError, open_xlsx


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="ID00003-script" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""

_SHARED_STRINGS = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
  <si><t>원문</t></si>
  <si><r><t>⑲ 원문 </t></r><r><t>$n</t></r></si>
  <si><t xml:space="preserve">   </t></si>
</sst>"""

_SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="inlineStr"><is><t>제미니번역</t></is></c>
      <c r="C1" t="inlineStr"><is><t>한글폰트로</t></is></c>
      <c r="D1" t="inlineStr"><is><t>길이</t></is></c>
      <c r="E1" t="inlineStr"><is><t>구글번역</t></is></c>
      <c r="F1" t="inlineStr"><is><t>기존번역</t></is></c>
      <c r="J1" t="inlineStr"><is><t>시작주소</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>1</v></c>
      <c r="B2" t="inlineStr"><is><r><t>⑲ 번역 </t></r><r><t>$ｎ</t></r></is></c>
      <c r="C2" t="str"><f>CONCAT(B2)</f><v>⑲ 치환 $ｎ</v></c>
      <c r="D2"><f>LEN(C2)</f><v>12</v></c>
      <c r="E2" t="inlineStr"><is><t>구글 결과</t></is></c>
      <c r="F2" t="inlineStr"><is><t>기존 결과</t></is></c>
      <c r="H2" t="d"><v>2026-07-22T10:20:30Z</v></c>
      <c r="I2" t="b"><v>1</v></c>
      <c r="J2" t="inlineStr"><is><t>0000012A</t></is></c>
    </row>
    <row r="3"><c r="A3" t="s"><v>2</v></c></row>
  </sheetData>
</worksheet>"""


def _make_xlsx(path: Path, sheet_xml: str = _SHEET) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _RELATIONSHIPS)
        archive.writestr("xl/sharedStrings.xml", _SHARED_STRINGS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


class XlsxReaderTest(unittest.TestCase):
    def test_공유문자_inline_str_수식캐시와_일반값을_읽는다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.xlsx"
            _make_xlsx(path)

            workbook = open_xlsx(path)
            rows = list(workbook.iter_rows("ID00003-script"))

            self.assertEqual(workbook.sheet_names, ("ID00003-script",))
            self.assertEqual(workbook.file_sha256, sha256(path.read_bytes()).hexdigest())
            self.assertEqual(rows[0].values[:3], ("원문", "제미니번역", "한글폰트로"))
            self.assertEqual(rows[1].values[0], "⑲ 원문 $n")
            self.assertEqual(rows[1].values[1], "⑲ 번역 $ｎ")
            self.assertEqual(rows[1].values[2], "⑲ 치환 $ｎ")
            self.assertEqual(rows[1].values[3], "12")
            self.assertEqual(rows[1].values[6], "")
            self.assertEqual(rows[1].values[7], "2026-07-22T10:20:30Z")
            self.assertEqual(rows[1].values[8], "TRUE")

    def test_설정으로_가져오고_공백_원문을_제외한다(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            source_dir = root / "원고"
            source_dir.mkdir(parents=True)
            xlsx_path = source_dir / "STG0001(1화)-ID00004-script-통합.xlsx"
            _make_xlsx(xlsx_path)
            config_path = Path(directory) / "asset-groups.json"
            config_path.write_text(
                json.dumps(
                    {
                        "defaultGroups": ["stage"],
                        "groups": {
                            "stage": {
                                "root": "원고",
                                "pattern": "*.xlsx",
                                "recursive": False,
                                "expectedRecognizedFiles": 1,
                            }
                        },
                        "headerAliases": {
                            "source_text": ["원문", "문자열"],
                            "translation": ["제미니번역", "기존번역"],
                            "replacement_text": ["한글폰트로", "한글폰트"],
                            "google_translation": ["구글번역"],
                            "legacy_translation": ["기존번역"],
                            "source_offset": ["시작주소"],
                            "encoded_length": ["길이"],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_dir = Path(directory) / "output"

            result = import_archive(root, config_path, output_dir)

            self.assertEqual(result["counts"]["files"], 1)
            self.assertEqual(result["counts"]["recognized_files"], 1)
            self.assertEqual(result["counts"]["rows"], 1)
            row = result["rows"][0]
            self.assertEqual(
                row.entry_id,
                "STG0001/ID00003@FILE-ID00004/000002",
            )
            self.assertEqual(row.translation, "⑲ 번역 $ｎ")
            self.assertEqual(row.replacement_text, "⑲ 치환 $ｎ")
            self.assertEqual(row.google_translation, "구글 결과")
            self.assertEqual(row.legacy_translation, "기존 결과")
            self.assertEqual(row.encoded_length, 12)
            self.assertEqual(row.source_offset, 0x12A)
            self.assertEqual(row.status, "imported")
            self.assertEqual(row.control_signature, '["⑲","$n"]')
            self.assertEqual(
                row.source_artifact,
                "원고/STG0001(1화)-ID00004-script-통합.xlsx",
            )
            self.assertTrue(any("ID00004" in warning for warning in result["warnings"]))
            self.assertEqual(len(result["output_files"]), 1)
            self.assertEqual(read_tsv(result["output_files"][0]), [row])

    def test_분기명과_dlc_상위폴더로_중복_id를_막는다(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            stage_dir = root / "stage"
            (root / "dlc" / "DLC0301_cpk").mkdir(parents=True)
            (root / "dlc" / "DLC0302_cpk").mkdir(parents=True)
            stage_dir.mkdir()
            for filename in (
                "STG0210(8화후A분기)-ID00003-script.xlsx",
                "STG0210(8화후분기)-ID00003-script.xlsx",
            ):
                _make_xlsx(stage_dir / filename)
            for dlc_name in ("DLC0301_cpk", "DLC0302_cpk"):
                _make_xlsx(root / "dlc" / dlc_name / "ID00003-sjis.xlsx")

            config_path = Path(directory) / "asset-groups.json"
            config_path.write_text(
                json.dumps(
                    {
                        "defaultGroups": ["stage", "dlc"],
                        "groups": {
                            "stage": {
                                "root": "stage",
                                "pattern": "*.xlsx",
                                "recursive": False,
                            },
                            "dlc": {
                                "root": "dlc",
                                "pattern": "*.xlsx",
                                "recursive": True,
                            },
                        },
                        "headerAliases": {},
                    }
                ),
                encoding="utf-8",
            )

            result = import_archive(root, config_path)
            entry_ids = {row.entry_id for row in result["rows"]}

            self.assertEqual(len(entry_ids), 4)
            self.assertTrue(any(value.startswith("STG0210-8화후A분기/") for value in entry_ids))
            self.assertTrue(any(value.startswith("STG0210-8화후분기/") for value in entry_ids))
            self.assertTrue(any(value.startswith("DLC0301/") for value in entry_ids))
            self.assertTrue(any(value.startswith("DLC0302/") for value in entry_ids))

    def test_기록된_길이가_바이트_한도를_넘으면_차단한다(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "archive"
            source_dir = root / "원고"
            source_dir.mkdir(parents=True)
            overflow_sheet = _SHEET.replace(
                '<c r="J1" t="inlineStr"><is><t>시작주소</t></is></c>',
                '<c r="G1" t="inlineStr"><is><t>바이트길이</t></is></c>'
                '<c r="J1" t="inlineStr"><is><t>시작주소</t></is></c>',
            ).replace(
                '<c r="H2" t="d"><v>2026-07-22T10:20:30Z</v></c>',
                '<c r="G2"><v>10</v></c>'
                '<c r="H2" t="d"><v>2026-07-22T10:20:30Z</v></c>',
            )
            _make_xlsx(source_dir / "STG0001-ID00003.xlsx", overflow_sheet)
            config_path = Path(directory) / "asset-groups.json"
            config_path.write_text(
                json.dumps(
                    {
                        "defaultGroups": ["stage"],
                        "groups": {
                            "stage": {
                                "root": "원고",
                                "pattern": "*.xlsx",
                                "recursive": False,
                            }
                        },
                        "headerAliases": {
                            "source_text": ["원문"],
                            "translation": ["제미니번역"],
                            "replacement_text": ["한글폰트로"],
                            "byte_limit": ["바이트길이"],
                            "encoded_length": ["길이"],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = import_archive(root, config_path)
            row = result["rows"][0]

            self.assertEqual(row.byte_limit, 10)
            self.assertEqual(row.encoded_length, 12)
            self.assertEqual(row.status, "blocked")
            self.assertIn("초과", row.notes)

    def test_xlsx가_아닌_파일은_명확히_거부한다(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.xlsx"
            path.write_bytes(b"not an xlsx")

            with self.assertRaises(XlsxError):
                open_xlsx(path)


if __name__ == "__main__":
    unittest.main()
