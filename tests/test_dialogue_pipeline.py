from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from siok_patch.cpk_tool import CpkEntry, member_filename
from siok_patch.dialogue_pipeline import (
    DialogueEntryRequest,
    build_dialogue_cpk,
)
from siok_patch.hashes import sha256_file
from siok_patch.stage_script import parse_stage_script


def _make_workbook(path: Path, rows: list[tuple[str, str]]) -> None:
    def cell(reference: str, value: str) -> str:
        return (
            f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
            f"{escape(value)}</t></is></c>"
        )

    sheet_rows = [f'<row r="1">{cell("A1", "원문")}{cell("B1", "한글폰트로")}</row>']
    for number, (source, replacement) in enumerate(rows, start=2):
        sheet_rows.append(
            f'<row r="{number}">{cell(f"A{number}", source)}'
            f'{cell(f"B{number}", replacement)}</row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="잘못된-ID00003" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


class FakeCpkTool:
    executable_sha256 = "f" * 64
    executable_version = "fake-test-tool"

    def __init__(self) -> None:
        self.archives: dict[Path, dict[int, bytes]] = {}

    def add_archive(self, path: Path, entries: dict[int, bytes]) -> None:
        self.archives[path.resolve()] = dict(entries)

    def extract(self, source_cpk: Path, output_dir: Path) -> dict[int, CpkEntry]:
        payloads = self.archives[source_cpk.resolve()]
        output_dir.mkdir(parents=True)
        result: dict[int, CpkEntry] = {}
        for member_id, data in sorted(payloads.items()):
            path = output_dir / member_filename(member_id)
            path.write_bytes(data)
            result[member_id] = CpkEntry(
                member_id,
                path.resolve(),
                len(data),
                sha256(data).hexdigest(),
            )
        return result

    def pack(self, payload_dir: Path, member_ids, output_cpk: Path) -> None:
        payloads = {
            member_id: (payload_dir / member_filename(member_id)).read_bytes()
            for member_id in member_ids
        }
        output_cpk.write_bytes(b"FAKE CPK OUTPUT")
        self.archives[output_cpk.resolve()] = payloads


class DialoguePipelineTest(unittest.TestCase):
    def test_추출부터_리팩_재추출_검증까지_한_프로세스로_수행한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_cpk = root / "input" / "STG0001a.cpk"
            source_cpk.parent.mkdir()
            source_cpk.write_bytes(b"FAKE SOURCE CPK")
            source_hash = sha256_file(source_cpk)
            original_script = (
                "header\r\n  [[シン\r\n「原文$n」]],\r\ntail\r\n"
            ).encode("cp932")
            workbook = root / "translation.xlsx"
            _make_workbook(workbook, [("シン", "シン"), ("原文$n", "翻訳$ｎ")])
            tool = FakeCpkTool()
            tool.add_archive(source_cpk, {0: b"other", 4: original_script})

            report = build_dialogue_cpk(
                source_cpk,
                [DialogueEntryRequest(4, workbook)],
                tool,
                workspace_root=root,
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["sourceMemberIds"], ["ID00000", "ID00004"])
            self.assertEqual(report["outputMemberIds"], ["ID00000", "ID00004"])
            self.assertEqual(sha256_file(source_cpk), source_hash)
            self.assertTrue(Path(str(report["outputCpk"])).is_file())
            self.assertTrue(Path(str(report["reportPath"])).is_file())
            built = Path(str(report["workDir"])) / "build" / source_cpk.name
            parsed = parse_stage_script(tool.archives[built.resolve()][4])
            self.assertEqual(parsed.source_rows, ("シン", "翻訳$n"))
            self.assertEqual(tool.archives[built.resolve()][0], b"other")

    def test_원본에_없는_ID는_원본_ID를_명시해야_추가할_수_있다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_cpk = root / "STG0001a.cpk"
            source_cpk.write_bytes(b"FAKE SOURCE CPK")
            original_script = "  [[シン\r\n「原文」]],\r\n".encode("cp932")
            workbook = root / "translation.xlsx"
            _make_workbook(workbook, [("シン", "シン"), ("原文", "翻訳")])
            tool = FakeCpkTool()
            tool.add_archive(source_cpk, {4: original_script})

            report = build_dialogue_cpk(
                source_cpk,
                [DialogueEntryRequest(3, workbook, source_id=4)],
                tool,
                workspace_root=root,
            )

            self.assertEqual(report["outputMemberIds"], ["ID00003", "ID00004"])
            built = Path(str(report["workDir"])) / "build" / source_cpk.name
            self.assertIn(3, tool.archives[built.resolve()])
            self.assertIn(4, tool.archives[built.resolve()])


if __name__ == "__main__":
    unittest.main()
