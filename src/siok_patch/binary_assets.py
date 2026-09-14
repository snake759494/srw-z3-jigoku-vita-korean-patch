"""원본과 분리된 메모리 또는 출력 파일에만 고정 범위 패치를 적용한다."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Iterable

from .validation import (
    ByteRange,
    ValidationError,
    validate_byte_length,
    validate_non_overlapping,
    validate_range,
    verify_sha256,
)


class BinaryPatchError(ValidationError):
    """바이너리 패치가 안전 조건을 만족하지 못할 때 발생한다."""


class OriginalBytesMismatchError(BinaryPatchError):
    """패치 범위의 원본 바이트가 기대값과 다를 때 발생한다."""


@dataclass(frozen=True, slots=True)
class BinaryPatch:
    """크기가 고정된 한 바이트 범위의 교체 명세."""

    offset: int
    allocated_size: int
    replacement: bytes
    expected_original: bytes | None = None
    pad_byte: int | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.offset, int)
            or isinstance(self.offset, bool)
            or not isinstance(self.allocated_size, int)
            or isinstance(self.allocated_size, bool)
        ):
            raise BinaryPatchError("offset과 allocated_size는 정수여야 합니다.")
        if not isinstance(self.replacement, bytes):
            raise BinaryPatchError("replacement는 bytes여야 합니다.")
        if self.expected_original is not None and not isinstance(
            self.expected_original, bytes
        ):
            raise BinaryPatchError("expected_original은 bytes 또는 None이어야 합니다.")

        byte_range = ByteRange(
            self.offset,
            self.offset + self.allocated_size,
            self.label,
        )
        validate_byte_length(
            len(self.replacement),
            byte_range.length,
            label=self.label or "바이너리 교체",
        )

        if (
            self.expected_original is not None
            and len(self.expected_original) != byte_range.length
        ):
            raise BinaryPatchError(
                f"{self.label or '바이너리 교체'}의 expected_original 길이는 "
                f"할당 크기 {byte_range.length}바이트와 같아야 합니다."
            )
        if self.pad_byte is not None and (
            not isinstance(self.pad_byte, int)
            or isinstance(self.pad_byte, bool)
            or not 0 <= self.pad_byte <= 0xFF
        ):
            raise BinaryPatchError("pad_byte는 0~255 정수 또는 None이어야 합니다.")
        if len(self.replacement) < byte_range.length and self.pad_byte is None:
            raise BinaryPatchError(
                f"{self.label or '바이너리 교체'}의 교체값이 할당 크기보다 짧습니다. "
                "남은 원본 바이트를 실수로 보존하지 않도록 pad_byte를 명시하거나 "
                "완성된 고정 길이 교체값을 제공해야 합니다."
            )

    @property
    def end(self) -> int:
        """끝을 포함하지 않는 패치 범위의 끝 주소."""

        return self.offset + self.allocated_size

    @property
    def byte_range(self) -> ByteRange:
        return ByteRange(self.offset, self.end, self.label)


def _validate_patches(
    source: bytes,
    patches: Iterable[BinaryPatch],
) -> tuple[BinaryPatch, ...]:
    materialized = tuple(patches)
    for index, patch in enumerate(materialized, start=1):
        if not isinstance(patch, BinaryPatch):
            raise BinaryPatchError(f"패치 #{index}가 BinaryPatch가 아닙니다.")
        validate_range(patch.byte_range, len(source))
    validate_non_overlapping(patch.byte_range for patch in materialized)

    for patch in materialized:
        if patch.expected_original is None:
            continue
        actual = source[patch.offset : patch.end]
        if actual != patch.expected_original:
            raise OriginalBytesMismatchError(
                f"{patch.label or '바이너리 교체'}의 원본 바이트가 기대값과 다릅니다: "
                f"주소 [0x{patch.offset:X}, 0x{patch.end:X}), "
                f"기대 {patch.expected_original.hex().upper()}, 실제 {actual.hex().upper()}"
            )
    return materialized


def apply_patches(
    original: bytes | bytearray | memoryview,
    patches: Iterable[BinaryPatch],
    *,
    expected_sha256: str,
) -> bytes:
    """원본 해시와 모든 조건을 확인한 뒤 크기가 같은 새 바이트열을 만든다."""

    try:
        source = bytes(original)
    except (TypeError, ValueError) as exc:
        raise BinaryPatchError("original은 바이트 계열이어야 합니다.") from exc
    verify_sha256(source, expected_sha256, label="패치 원본")

    materialized = _validate_patches(source, patches)
    output = bytearray(source)
    for patch in materialized:
        replacement_end = patch.offset + len(patch.replacement)
        output[patch.offset:replacement_end] = patch.replacement
        if patch.pad_byte is not None and replacement_end < patch.end:
            output[replacement_end : patch.end] = bytes((patch.pad_byte,)) * (
                patch.end - replacement_end
            )

    if len(output) != len(source):
        raise BinaryPatchError("내부 오류: 패치 결과의 파일 크기가 원본과 달라졌습니다.")
    return bytes(output)


def extract_range(
    data: bytes | bytearray | memoryview,
    start: int,
    end: int,
    *,
    label: str = "추출 범위",
) -> bytes:
    """검증된 반열린 주소 범위의 바이트만 돌려준다."""

    try:
        raw = bytes(data)
    except (TypeError, ValueError) as exc:
        raise BinaryPatchError("data는 바이트 계열이어야 합니다.") from exc
    byte_range = validate_range(ByteRange(start, end, label), len(raw))
    return raw[byte_range.start : byte_range.end]


def patch_file(
    source_path: str | Path,
    output_path: str | Path,
    patches: Iterable[BinaryPatch],
    *,
    expected_sha256: str,
    overwrite: bool = False,
) -> Path:
    """해시를 확인하고 원본과 다른 경로에 결과를 원자적으로 저장한다."""

    source = Path(source_path).resolve(strict=True)
    output = Path(output_path).resolve(strict=False)
    if source == output:
        raise BinaryPatchError("원본 파일 경로에 직접 덮어쓸 수 없습니다.")
    if output.exists() and not overwrite:
        raise FileExistsError(f"출력 파일이 이미 있습니다: {output}")

    original = source.read_bytes()
    result = apply_patches(original, patches, expected_sha256=expected_sha256)
    output.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output


__all__ = [
    "BinaryPatch",
    "BinaryPatchError",
    "OriginalBytesMismatchError",
    "apply_patches",
    "extract_range",
    "patch_file",
]
