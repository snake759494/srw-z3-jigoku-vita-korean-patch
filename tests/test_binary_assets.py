"""고정 범위 바이너리 패치의 안전성 회귀 테스트."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from siok_patch.binary_assets import (
    BinaryPatch,
    BinaryPatchError,
    OriginalBytesMismatchError,
    apply_patches,
    patch_file,
)
from siok_patch.validation import (
    AddressRangeError,
    HashMismatchError,
    LengthOverflowError,
    OverlapError,
)


class BinaryAssetTests(unittest.TestCase):
    @staticmethod
    def apply(original: bytes, patches: list[BinaryPatch]) -> bytes:
        """각 테스트도 실제 사용과 똑같이 원본 해시를 반드시 제공한다."""

        expected_hash = hashlib.sha256(original).hexdigest()
        return apply_patches(original, patches, expected_sha256=expected_hash)

    def test_원본을_바꾸지_않고_새_바이트열을_만든다(self) -> None:
        original = b"0123456789"
        patch = BinaryPatch(
            offset=2,
            allocated_size=4,
            replacement=b"AB",
            expected_original=b"2345",
            pad_byte=0,
            label="대사 1",
        )

        result = self.apply(original, [patch])

        self.assertEqual(original, b"0123456789")
        self.assertEqual(result, b"01AB\x00\x006789")
        self.assertEqual(len(result), len(original))

    def test_짧은_교체값은_패딩을_명시하지_않으면_거부한다(self) -> None:
        with self.assertRaises(BinaryPatchError):
            BinaryPatch(offset=1, allocated_size=3, replacement=b"X")

    def test_파일_밖의_주소를_거부한다(self) -> None:
        patch = BinaryPatch(offset=4, allocated_size=2, replacement=b"XY")

        with self.assertRaises(AddressRangeError):
            self.apply(b"12345", [patch])

    def test_음수_주소를_거부한다(self) -> None:
        with self.assertRaises(AddressRangeError):
            BinaryPatch(offset=-1, allocated_size=1, replacement=b"X")

    def test_bool을_주소나_크기로_받지_않는다(self) -> None:
        with self.assertRaises(BinaryPatchError):
            BinaryPatch(offset=False, allocated_size=1, replacement=b"X")
        with self.assertRaises(BinaryPatchError):
            BinaryPatch(offset=0, allocated_size=True, replacement=b"X")

    def test_겹치는_할당_범위를_거부한다(self) -> None:
        patches = [
            BinaryPatch(offset=1, allocated_size=3, replacement=b"ABC"),
            BinaryPatch(offset=3, allocated_size=2, replacement=b"DE"),
        ]

        with self.assertRaises(OverlapError):
            self.apply(b"012345", patches)

    def test_교체_길이_초과를_거부한다(self) -> None:
        with self.assertRaises(LengthOverflowError):
            BinaryPatch(offset=0, allocated_size=2, replacement=b"ABC")

    def test_원본_바이트_불일치를_거부한다(self) -> None:
        patch = BinaryPatch(
            offset=1,
            allocated_size=2,
            replacement=b"XY",
            expected_original=b"ZZ",
        )

        with self.assertRaises(OriginalBytesMismatchError):
            self.apply(b"0123", [patch])

    def test_원본_해시_불일치를_거부한다(self) -> None:
        patch = BinaryPatch(offset=0, allocated_size=1, replacement=b"X")

        with self.assertRaises(HashMismatchError):
            apply_patches(b"0123", [patch], expected_sha256="0" * 64)

    def test_파일_패치는_원본과_다른_경로에만_쓴다(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "원본.bin"
            output = root / "작업" / "결과.bin"
            source.write_bytes(b"0123")
            expected_hash = hashlib.sha256(b"0123").hexdigest()
            patch = BinaryPatch(offset=1, allocated_size=2, replacement=b"AB")

            written = patch_file(
                source,
                output,
                [patch],
                expected_sha256=expected_hash,
            )

            self.assertEqual(written, output.resolve())
            self.assertEqual(source.read_bytes(), b"0123")
            self.assertEqual(output.read_bytes(), b"0AB3")

            with self.assertRaises(BinaryPatchError):
                patch_file(
                    source,
                    source,
                    [patch],
                    expected_sha256=expected_hash,
                )


if __name__ == "__main__":
    unittest.main()
