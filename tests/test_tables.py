"""TBL 파서와 엄격 코덱의 회귀 테스트."""

from __future__ import annotations

import unittest

from siok_patch.codec import (
    CodecError,
    SHIFT_JIS_FULLWIDTH_SPACE,
    TableCodec,
    UnmappedBytesError,
    UnmappedTextError,
    encode_shift_jis_strict,
)
from siok_patch.tables import (
    DuplicateByteSequenceError,
    DuplicateTextError,
    TableFormatError,
    parse_tbl,
)
from siok_patch.validation import LengthOverflowError


class CharacterTableTests(unittest.TestCase):
    def test_제어_토큰을_왕복한다(self) -> None:
        table = parse_tbl("00=㊦\n8140=⑲\n8141=、\n", source="시험.tbl")
        codec = TableCodec(table)

        encoded = codec.encode("⑲、㊦")

        self.assertEqual(encoded, bytes((0x81, 0x40, 0x81, 0x41, 0x00)))
        self.assertEqual(codec.decode(encoded), "⑲、㊦")

    def test_긴_문자_토큰을_먼저_선택한다(self) -> None:
        table = parse_tbl("41=가\n4243=가나\n44=나\n")
        codec = TableCodec(table)

        self.assertEqual(codec.encode("가나가"), b"\x42\x43\x41")

    def test_긴_바이트_토큰을_먼저_선택한다(self) -> None:
        table = parse_tbl("81=가\n8140=나\n40=다\n")
        codec = TableCodec(table)

        self.assertEqual(codec.decode(bytes((0x81, 0x40))), "나")

    def test_중복_바이트열을_거부한다(self) -> None:
        with self.assertRaises(DuplicateByteSequenceError):
            parse_tbl("8140=가\n8140=나\n", source="중복.tbl")

    def test_중복_문자_토큰을_거부한다(self) -> None:
        with self.assertRaises(DuplicateTextError):
            parse_tbl("8140=가\n8141=가\n", source="중복.tbl")

    def test_잘못된_TBL_행을_조용히_건너뛰지_않는다(self) -> None:
        invalid_tables = (
            "8140=가\n잘못된행\n",
            "814=가\n",
            "81ZZ=가\n",
            "8140=\n",
        )
        for contents in invalid_tables:
            with self.subTest(contents=contents):
                with self.assertRaises(TableFormatError):
                    parse_tbl(contents)

    def test_미등록_문자와_바이트를_거부한다(self) -> None:
        codec = TableCodec(parse_tbl("41=가\n"))

        with self.assertRaises(UnmappedTextError):
            codec.encode("가나")
        with self.assertRaises(UnmappedBytesError):
            codec.decode(b"\x41\x42")

    def test_인코딩_길이_초과를_거부한다(self) -> None:
        codec = TableCodec(parse_tbl("8140=가\n"))

        with self.assertRaises(LengthOverflowError):
            codec.encode("가", byte_limit=1)

    def test_shift_jis_전각_공백은_정확히_8140이다(self) -> None:
        encoded = encode_shift_jis_strict("　")

        self.assertEqual(SHIFT_JIS_FULLWIDTH_SPACE, bytes((0x81, 0x40)))
        self.assertEqual(encoded, bytes((0x81, 0x40)))
        self.assertEqual(len(encoded), 2)

    def test_shift_jis_미등록_문자를_거부한다(self) -> None:
        with self.assertRaises(CodecError):
            encode_shift_jis_strict("한")


if __name__ == "__main__":
    unittest.main()
