from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from siok_patch.cpk_tool import (
    CpkCommandResult,
    CpkMakerTool,
    CpkToolError,
    collect_id_entries,
    member_filename,
    parse_member_id,
)
from siok_patch.hashes import sha256_file


class CpkToolTest(unittest.TestCase):
    def test_멤버_ID_표기를_엄격하게_정규화한다(self) -> None:
        self.assertEqual(parse_member_id("ID00004"), 4)
        self.assertEqual(parse_member_id("00004"), 4)
        self.assertEqual(parse_member_id(4), 4)
        self.assertEqual(member_filename(4), "ID00004")
        for value in ("ID-4", "name", -1, 63355, True):
            with self.subTest(value=value), self.assertRaises(CpkToolError):
                parse_member_id(value)

    def test_추출_폴더에서_ID와_해시를_수집한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ID00004").write_bytes(b"script")
            (root / "ID00002").write_bytes(b"other")

            entries = collect_id_entries(root)

            self.assertEqual(tuple(entries), (2, 4))
            self.assertEqual(entries[4].size, 6)
            self.assertEqual(entries[4].sha256, sha256_file(root / "ID00004"))

    def test_ID_전용이_아닌_추출_결과를_거부한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "script.bin").write_bytes(b"x")
            with self.assertRaises(CpkToolError):
                collect_id_entries(root)

    def test_도구_해시가_다르면_실행하지_않는다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tool_path = root / "cpkmakec.exe"
            tool_path.write_bytes(b"dummy")
            source = root / "source.cpk"
            source.write_bytes(b"CPK " + bytes(0x800))
            tool = CpkMakerTool(tool_path, "0" * 64)

            with patch.object(tool, "_run") as runner:
                with self.assertRaises(CpkToolError):
                    tool.extract(source, root / "extract")
                runner.assert_not_called()

    def test_명시적인_ID_CSV를_만들어_리팩한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tool_path = root / "cpkmakec.exe"
            tool_path.write_bytes(b"dummy tool")
            payloads = root / "payloads"
            payloads.mkdir()
            (payloads / "ID00000").write_bytes(b"zero")
            (payloads / "ID00004").write_bytes(b"four")
            output = root / "result.cpk"
            tool = CpkMakerTool(tool_path, sha256_file(tool_path))

            def fake_run(arguments: list[str], *, cwd: Path) -> CpkCommandResult:
                (cwd / arguments[1]).write_bytes(b"CPK " + bytes(0x800))
                return CpkCommandResult(tuple(arguments), 0, "completed", "", 0.01)

            with patch.object(tool, "_run", side_effect=fake_run):
                tool.pack(payloads, (0, 4), output)

            rows = (root / "cpk-build.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(rows, ["payloads/ID00000,,0,UC", "payloads/ID00004,,4,UC"])
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
