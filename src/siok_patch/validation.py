"""패치 입력을 변경하기 전에 공통으로 수행하는 엄격한 검증 도구."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable


class ValidationError(ValueError):
    """안전하게 계속할 수 없는 입력을 발견했을 때 발생한다."""


class AddressRangeError(ValidationError):
    """바이트 주소가 잘못되었거나 파일 범위를 벗어났을 때 발생한다."""


class OverlapError(ValidationError):
    """서로 겹치는 바이트 범위를 발견했을 때 발생한다."""


class LengthOverflowError(ValidationError):
    """인코딩 또는 교체 결과가 허용 바이트 수를 넘을 때 발생한다."""


class HashMismatchError(ValidationError):
    """입력 데이터의 SHA-256이 기대값과 다를 때 발생한다."""


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_plain_int(value: object) -> bool:
    """bool을 주소로 잘못 받지 않도록 진짜 정수만 판별한다."""

    return isinstance(value, int) and not isinstance(value, bool)


def _display_label(label: str) -> str:
    return f"'{label}'" if label else "이름 없는 항목"


@dataclass(frozen=True, slots=True)
class ByteRange:
    """끝 주소를 포함하지 않는 반열린 바이트 범위 ``[start, end)``."""

    start: int
    end: int
    label: str = ""

    def __post_init__(self) -> None:
        if not _is_plain_int(self.start) or not _is_plain_int(self.end):
            raise AddressRangeError(
                f"{_display_label(self.label)}의 시작/끝 주소는 정수여야 합니다."
            )
        if self.start < 0:
            raise AddressRangeError(
                f"{_display_label(self.label)}의 시작 주소가 음수입니다: {self.start}"
            )
        if self.end <= self.start:
            raise AddressRangeError(
                f"{_display_label(self.label)}의 끝 주소는 시작 주소보다 커야 합니다: "
                f"0x{self.start:X}..0x{self.end:X}"
            )
        if not isinstance(self.label, str):
            raise ValidationError("바이트 범위의 label은 문자열이어야 합니다.")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "ByteRange") -> bool:
        return self.start < other.end and other.start < self.end


def validate_file_size(file_size: int) -> int:
    """파일 크기가 0 이상의 정수인지 검사하고 그대로 돌려준다."""

    if not _is_plain_int(file_size) or file_size < 0:
        raise AddressRangeError(f"파일 크기는 0 이상의 정수여야 합니다: {file_size!r}")
    return file_size


def validate_range(byte_range: ByteRange, file_size: int) -> ByteRange:
    """범위가 지정한 파일 안에 완전히 들어오는지 검사한다."""

    validate_file_size(file_size)
    if byte_range.end > file_size:
        raise AddressRangeError(
            f"{_display_label(byte_range.label)}의 범위 "
            f"0x{byte_range.start:X}..0x{byte_range.end:X}가 "
            f"파일 크기 0x{file_size:X}를 벗어납니다."
        )
    return byte_range


def validate_non_overlapping(ranges: Iterable[ByteRange]) -> tuple[ByteRange, ...]:
    """모든 범위가 겹치지 않는지 검사하고 시작 주소 순으로 돌려준다."""

    ordered = tuple(sorted(ranges, key=lambda item: (item.start, item.end)))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.overlaps(current):
            raise OverlapError(
                f"바이트 범위가 겹칩니다: {_display_label(previous.label)} "
                f"[0x{previous.start:X}, 0x{previous.end:X}) / "
                f"{_display_label(current.label)} "
                f"[0x{current.start:X}, 0x{current.end:X})"
            )
    return ordered


def validate_byte_length(actual: int, limit: int, *, label: str = "") -> int:
    """실제 바이트 길이가 0 이상이고 허용 길이를 넘지 않는지 검사한다."""

    if not _is_plain_int(actual) or actual < 0:
        raise ValidationError(
            f"{_display_label(label)}의 실제 바이트 길이는 0 이상의 정수여야 합니다: "
            f"{actual!r}"
        )
    if not _is_plain_int(limit) or limit < 0:
        raise ValidationError(
            f"{_display_label(label)}의 허용 바이트 길이는 0 이상의 정수여야 합니다: "
            f"{limit!r}"
        )
    if actual > limit:
        raise LengthOverflowError(
            f"{_display_label(label)}의 결과가 허용 길이를 초과합니다: "
            f"실제 {actual}바이트, 허용 {limit}바이트"
        )
    return actual


def normalize_sha256(expected: str) -> str:
    """SHA-256 문자열 형식을 검사하고 소문자로 정규화한다."""

    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise ValidationError(
            "SHA-256 기대값은 정확히 64자리인 16진수 문자열이어야 합니다."
        )
    return expected.lower()


def sha256_bytes(data: bytes | bytearray | memoryview) -> str:
    """바이트열의 SHA-256을 소문자 16진수로 계산한다."""

    try:
        view = memoryview(data)
    except TypeError as exc:
        raise ValidationError("SHA-256 입력은 바이트 계열이어야 합니다.") from exc
    return hashlib.sha256(view).hexdigest()


def verify_sha256(
    data: bytes | bytearray | memoryview,
    expected: str,
    *,
    label: str = "원본",
) -> str:
    """바이트열의 SHA-256이 기대값과 같은지 검사한다."""

    normalized = normalize_sha256(expected)
    actual = sha256_bytes(data)
    if actual != normalized:
        raise HashMismatchError(
            f"{_display_label(label)}의 SHA-256이 다릅니다: "
            f"기대 {normalized}, 실제 {actual}"
        )
    return actual
