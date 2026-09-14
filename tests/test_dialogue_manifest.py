"""JSON 기반 대사 매니페스트의 결정성·변조 방어·왕복 회귀 테스트."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from siok_patch.cpk_tool import CpkEntry, member_filename
from siok_patch.dialogue_manifest import (
    DialogueManifest,
    DialogueManifestError,
    apply_manifest_script,
    compile_manifest_script,
    compose_dialogue_manifest,
    dump_dialogue_manifest,
    load_dialogue_manifest,
    parse_dialogue_manifest_dict,
)
from siok_patch.dialogue_pipeline import (
    DialogueEntryRequest,
    build_dialogue_cpk,
    build_dialogue_cpk_from_manifest,
)
from siok_patch.dialogue_workbook import load_dialogue_workbook
from siok_patch.hashes import sha256_file
from siok_patch.stage_script import parse_stage_script


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_TOOL_SHA256 = "f" * 64
_TOOL_VERSION = "fake-json-test-tool"


def _cell(reference: str, value: str) -> str:
    return (
        f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
        f"{escape(value)}</t></is></c>"
    )


def _make_workbook(path: Path, rows: list[tuple[str, str]]) -> None:
    """외부 라이브러리 없이 테스트 전용 최소 XLSX를 만든다."""

    sheet_rows = [
        f'<row r="1">{_cell("A1", "원문")}{_cell("B1", "한글폰트로")}</row>'
    ]
    for number, (source, replacement) in enumerate(rows, start=2):
        sheet_rows.append(
            f'<row r="{number}">{_cell(f"A{number}", source)}'
            f'{_cell(f"B{number}", replacement)}</row>'
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData>'
        f'{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{_MAIN_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="잘못된 ID00003" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
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
        "</Types>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _source_payload() -> bytes:
    return "header\r\n  [[シン\r\n「原文$n」]],\r\ntail\r\n".encode("cp932")


@dataclass(frozen=True)
class _ManifestFixture:
    manifest: DialogueManifest
    source_cpk: Path
    source_payload: bytes
    workbook: Path
    payload_path: Path


def _make_manifest_fixture(
    root: Path,
    *,
    replacement_token: str = "$n",
    approved_rows: tuple[int, ...] = (),
) -> _ManifestFixture:
    root.mkdir(parents=True, exist_ok=True)
    source_cpk = root / "STGTEST.cpk"
    source_cpk.write_bytes(b"FAKE SOURCE CPK")
    payload = _source_payload()
    payload_path = root / "ID00004"
    payload_path.write_bytes(payload)
    workbook_path = root / "translation.xlsx"
    _make_workbook(
        workbook_path,
        [("シン", "シン"), ("原文$n", f"翻訳{replacement_token}")],
    )
    parsed = parse_stage_script(payload)
    workbook = load_dialogue_workbook(workbook_path, parsed.source_rows)
    payload_hash = sha256(payload).hexdigest()
    entry = CpkEntry(4, payload_path.resolve(), len(payload), payload_hash)
    script = compile_manifest_script(
        4,
        payload,
        payload_hash,
        workbook,
        approved_workbook_rows=approved_rows,
    )
    manifest = compose_dialogue_manifest(
        source_cpk_name=source_cpk.name,
        source_cpk_size=source_cpk.stat().st_size,
        source_cpk_sha256=sha256_file(source_cpk),
        original_entries={4: entry},
        tool_sha256=_TOOL_SHA256,
        tool_version=_TOOL_VERSION,
        compiled_outputs=[(4, script)],
    )
    return _ManifestFixture(
        manifest=manifest,
        source_cpk=source_cpk,
        source_payload=payload,
        workbook=workbook_path,
        payload_path=payload_path,
    )


class _FakeCpkTool:
    executable_sha256 = _TOOL_SHA256
    executable_version = _TOOL_VERSION

    def __init__(self) -> None:
        self.archives: dict[Path, dict[int, bytes]] = {}
        self.extract_calls = 0
        self.pack_calls = 0

    def add_archive(self, path: Path, entries: dict[int, bytes]) -> None:
        self.archives[path.resolve()] = dict(entries)

    def extract(self, source_cpk: Path, output_dir: Path) -> dict[int, CpkEntry]:
        self.extract_calls += 1
        payloads = self.archives[source_cpk.resolve()]
        output_dir.mkdir(parents=True)
        entries: dict[int, CpkEntry] = {}
        for member_id, data in sorted(payloads.items()):
            path = output_dir / member_filename(member_id)
            path.write_bytes(data)
            entries[member_id] = CpkEntry(
                member_id=member_id,
                path=path.resolve(),
                size=len(data),
                sha256=sha256(data).hexdigest(),
            )
        return entries

    def pack(self, payload_dir: Path, member_ids, output_cpk: Path) -> None:
        self.pack_calls += 1
        payloads = {
            member_id: (payload_dir / member_filename(member_id)).read_bytes()
            for member_id in member_ids
        }
        # CPK 자체도 입력 payload에 대해 결정적이어야 JSON/XLSX 왕복을 비교할 수 있다.
        encoded = bytearray(b"FAKE-CPK\0")
        for member_id, data in sorted(payloads.items()):
            encoded.extend(member_id.to_bytes(4, "little"))
            encoded.extend(sha256(data).digest())
        output_cpk.write_bytes(bytes(encoded))
        self.archives[output_cpk.resolve()] = payloads


class DialogueManifestTest(unittest.TestCase):
    def test_정규_JSON_dump_load가_결정적이다(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _make_manifest_fixture(Path(directory))
            first = dump_dialogue_manifest(fixture.manifest)
            second = dump_dialogue_manifest(fixture.manifest)
            path = Path(directory) / "dialogue-manifest.json"
            path.write_bytes(first)
            loaded = load_dialogue_manifest(path)

            self.assertEqual(first, second)
            self.assertEqual(dump_dialogue_manifest(loaded), first)
            self.assertTrue(first.endswith(b"\n"))
            self.assertNotIn(b"\r\n", first)
            self.assertIn("잘못된 ID00003".encode("utf-8"), first)
            self.assertEqual(
                loaded.scripts[0].workbook.sha256,
                sha256_file(fixture.workbook),
            )

    def test_unknown_key와_중복_key를_거부한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _make_manifest_fixture(root)
            normal = dump_dialogue_manifest(fixture.manifest).decode("utf-8")

            unknown = fixture.manifest.to_dict()
            unknown["sourceCPK"] = unknown["sourceCpk"]
            unknown_path = root / "unknown.json"
            unknown_path.write_text(
                json.dumps(unknown, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaises(DialogueManifestError) as caught:
                load_dialogue_manifest(unknown_path)
            self.assertEqual(caught.exception.code, "manifest-unknown-key")
            self.assertEqual(caught.exception.json_path, "$")

            duplicate = normal.replace(
                '  "schemaVersion": 1,\n',
                '  "schemaVersion": 1,\n  "schemaVersion": 1,\n',
                1,
            )
            duplicate_path = root / "duplicate.json"
            duplicate_path.write_text(duplicate, encoding="utf-8")
            with self.assertRaises(DialogueManifestError) as caught:
                load_dialogue_manifest(duplicate_path)
            self.assertEqual(caught.exception.code, "manifest-duplicate-key")

    def test_비표준_JSON_타입과_중첩_unknown을_거부한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _make_manifest_fixture(root)

            boolean_version = fixture.manifest.to_dict()
            boolean_version["schemaVersion"] = True
            with self.assertRaises(DialogueManifestError) as caught:
                parse_dialogue_manifest_dict(boolean_version)
            self.assertEqual(caught.exception.code, "manifest-type")

            nested_unknown = fixture.manifest.to_dict()
            nested_unknown["scripts"][0]["rows"][0]["control"]["allow"] = True
            with self.assertRaises(DialogueManifestError) as caught:
                parse_dialogue_manifest_dict(nested_unknown)
            self.assertEqual(caught.exception.code, "manifest-unknown-key")

            nonstandard_number = dump_dialogue_manifest(fixture.manifest).decode(
                "utf-8"
            ).replace('"schemaVersion": 1', '"schemaVersion": NaN', 1)
            path = root / "nan.json"
            path.write_text(nonstandard_number, encoding="utf-8")
            with self.assertRaises(DialogueManifestError) as caught:
                load_dialogue_manifest(path)
            self.assertEqual(caught.exception.code, "manifest-type")

    def test_행_순서_태그와_해시_변조를_거부한다(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _make_manifest_fixture(Path(directory))
            original = fixture.manifest.to_dict()

            swapped = deepcopy(original)
            rows = swapped["scripts"][0]["rows"]
            rows[0], rows[1] = rows[1], rows[0]
            with self.assertRaises(DialogueManifestError) as caught:
                parse_dialogue_manifest_dict(swapped)
            self.assertEqual(caught.exception.code, "manifest-row-order")

            changed_tag = deepcopy(original)
            changed_tag["scripts"][0]["rows"][1]["tag"] = "S2"
            loaded = parse_dialogue_manifest_dict(changed_tag)
            with self.assertRaises(DialogueManifestError) as caught:
                apply_manifest_script(loaded.scripts[0], fixture.source_payload)
            self.assertEqual(caught.exception.code, "expected-line-binding-mismatch")

            changed_raw_hash = deepcopy(original)
            changed_raw_hash["scripts"][0]["rows"][1][
                "sourceRawLineSha256"
            ] = "0" * 64
            loaded = parse_dialogue_manifest_dict(changed_raw_hash)
            with self.assertRaises(DialogueManifestError) as caught:
                apply_manifest_script(loaded.scripts[0], fixture.source_payload)
            self.assertEqual(caught.exception.code, "expected-line-binding-mismatch")

            changed_output_hash = deepcopy(original)
            changed_output_hash["scripts"][0]["expectedPayloadSha256"] = "0" * 64
            loaded = parse_dialogue_manifest_dict(changed_output_hash)
            with self.assertRaises(DialogueManifestError) as caught:
                apply_manifest_script(loaded.scripts[0], fixture.source_payload)
            self.assertEqual(caught.exception.code, "expected-payload-hash-mismatch")

    def test_제어_토큰_승인은_필수이고_남으면_실패한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(DialogueManifestError) as caught:
                _make_manifest_fixture(root / "required", replacement_token="$l")
            self.assertEqual(caught.exception.code, "approval-required")

            with self.assertRaises(DialogueManifestError) as caught:
                _make_manifest_fixture(
                    root / "stale",
                    replacement_token="$n",
                    approved_rows=(3,),
                )
            self.assertEqual(caught.exception.code, "approval-stale")

    def test_승인은_행_번호_raw_해시와_토큰에_정확히_결박된다(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _make_manifest_fixture(
                Path(directory), replacement_token="$l", approved_rows=(3,)
            )
            original = fixture.manifest.to_dict()

            changes = (
                ("ordinal", 9),
                ("workbookRow", 9),
                ("sourceRawLineSha256", "0" * 64),
                ("sourceTokens", ["$l"]),
                ("replacementTokens", ["$n"]),
            )
            for key, value in changes:
                with self.subTest(key=key):
                    changed = deepcopy(original)
                    changed["scripts"][0]["rows"][1]["control"]["approval"][
                        key
                    ] = value
                    with self.assertRaises(DialogueManifestError) as caught:
                        parse_dialogue_manifest_dict(changed)
                    self.assertEqual(
                        caught.exception.code, "approval-binding-mismatch"
                    )

            missing = deepcopy(original)
            missing["scripts"][0]["rows"][1]["control"]["approval"] = None
            with self.assertRaises(DialogueManifestError) as caught:
                parse_dialogue_manifest_dict(missing)
            self.assertEqual(caught.exception.code, "approval-required")

    def test_XLSX가_만든_JSON으로_직접_재빌드하면_동일한_payload다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _make_manifest_fixture(root / "inputs")
            tool = _FakeCpkTool()
            tool.add_archive(
                inputs.source_cpk,
                {0: b"unchanged member", 4: inputs.source_payload},
            )
            workspace = root / "workspace"

            xlsx_report = build_dialogue_cpk(
                inputs.source_cpk,
                (
                    DialogueEntryRequest(3, inputs.workbook, source_id=4),
                    DialogueEntryRequest(4, inputs.workbook),
                ),
                tool,
                workspace_root=workspace,
            )
            manifest_path = Path(str(xlsx_report["dialogueManifest"]))
            manifest = load_dialogue_manifest(manifest_path)

            self.assertEqual(len(manifest.scripts), 1)
            self.assertEqual(
                tuple(item.target_member_id for item in manifest.outputs),
                ("ID00003", "ID00004"),
            )
            self.assertEqual(
                {item.script_id for item in manifest.outputs}, {"script-001"}
            )

            first_built = (
                Path(str(xlsx_report["workDir"])) / "build" / inputs.source_cpk.name
            ).resolve()
            first_payloads = tool.archives[first_built]
            self.assertEqual(first_payloads[3], first_payloads[4])

            # JSON 직접 빌드가 XLSX를 다시 읽지 않는다는 것도 함께 검증한다.
            inputs.workbook.unlink()
            json_report = build_dialogue_cpk_from_manifest(
                inputs.source_cpk,
                manifest_path,
                tool,
                workspace_root=workspace,
            )
            second_built = (
                Path(str(json_report["workDir"])) / "build" / inputs.source_cpk.name
            ).resolve()
            second_payloads = tool.archives[second_built]

            self.assertEqual(json_report["inputMode"], "json")
            self.assertEqual(first_payloads, second_payloads)
            self.assertEqual(xlsx_report["outputSha256"], json_report["outputSha256"])
            self.assertEqual(
                xlsx_report["dialogueManifestSha256"],
                json_report["dialogueManifestSha256"],
            )
            parsed = parse_stage_script(second_payloads[4])
            self.assertEqual(parsed.source_rows, ("シン", "翻訳$n"))

    def test_잘못된_JSON은_CPK_도구를_호출하기_전에_중단한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _make_manifest_fixture(root / "inputs")
            invalid = fixture.manifest.to_dict()
            invalid["unknown"] = True
            invalid_path = root / "invalid.json"
            invalid_path.write_text(
                json.dumps(invalid, ensure_ascii=False), encoding="utf-8"
            )
            tool = _FakeCpkTool()

            with self.assertRaises(DialogueManifestError) as caught:
                build_dialogue_cpk_from_manifest(
                    fixture.source_cpk,
                    invalid_path,
                    tool,
                    workspace_root=root / "workspace",
                )

            self.assertEqual(caught.exception.code, "manifest-unknown-key")
            self.assertEqual(tool.extract_calls, 0)
            self.assertEqual(tool.pack_calls, 0)
            self.assertFalse((root / "workspace" / "work").exists())


if __name__ == "__main__":
    unittest.main()
