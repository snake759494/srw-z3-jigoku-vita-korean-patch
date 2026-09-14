"""TBL 및 표준 Shift-JIS 문자열을 손실 없이 변환하는 엄격한 코덱."""

from __future__ import annotations

from dataclasses import dataclass

from .tables import CharacterTable
from .validation import LengthOverflowError, validate_byte_length


# U+3000 IDEOGRAPHIC SPACE의 Shift-JIS 값이다. 한 바이트 이스케이프 뒤에
# ASCII 숫자를 붙이는 실수를 막기 위해 두 바이트를 각각 정수로 명시한다.
SHIFT_JIS_FULLWIDTH_SPACE = bytes((0x81, 0x40))


class CodecError(ValueError):
    """문자 또는 바이트를 손실 없이 변환할 수 없을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class UnmappedTextError(CodecError):
    """TBL에 없는 문자 토큰을 만났다는 정보."""

    text: str
    position: int
    table_source: str

    def __str__(self) -> str:
        fragment = self.text[self.position : self.position + 8]
        character = self.text[self.position : self.position + 1]
        return (
            f"{self.table_source}에 등록되지 않은 문자가 위치 {self.position}에 있습니다: "
            f"문자 {character!r}, 주변 {fragment!r}"
        )


@dataclass(frozen=True, slots=True)
class UnmappedBytesError(CodecError):
    """TBL에 없는 바이트열을 만났다는 정보."""

    data: bytes
    position: int
    table_source: str

    def __str__(self) -> str:
        fragment = self.data[self.position : self.position + 8].hex().upper()
        return (
            f"{self.table_source}에 등록되지 않은 바이트가 위치 0x{self.position:X}에 "
            f"있습니다: {fragment}"
        )


class TableCodec:
    """가장 긴 토큰을 우선하여 TBL을 양방향으로 변환한다."""

    __slots__ = ("table", "_text_by_initial", "_bytes_by_initial")

    def __init__(self, table: CharacterTable) -> None:
        if not isinstance(table, CharacterTable):
            raise TypeError("table은 CharacterTable이어야 합니다.")
        self.table = table

        text_by_initial: dict[str, list[str]] = {}
        for token in table.text_tokens_longest_first:
            text_by_initial.setdefault(token[0], []).append(token)
        self._text_by_initial = {
            initial: tuple(tokens) for initial, tokens in text_by_initial.items()
        }

        bytes_by_initial: dict[int, list[bytes]] = {}
        for token in table.byte_tokens_longest_first:
            bytes_by_initial.setdefault(token[0], []).append(token)
        self._bytes_by_initial = {
            initial: tuple(tokens) for initial, tokens in bytes_by_initial.items()
        }

    def encode(self, text: str, *, byte_limit: int | None = None) -> bytes:
        """문자열 전체를 변환하며 미등록 문자를 절대로 건너뛰지 않는다."""

        if not isinstance(text, str):
            raise TypeError("인코딩 입력은 문자열이어야 합니다.")

        output = bytearray()
        position = 0
        while position < len(text):
            candidates = self._text_by_initial.get(text[position], ())
            matched = next(
                (token for token in candidates if text.startswith(token, position)),
                None,
            )
            if matched is None:
                raise UnmappedTextError(text, position, self.table.source)
            output.extend(self.table.text_to_bytes[matched])
            position += len(matched)

        encoded = bytes(output)
        if byte_limit is not None:
            validate_byte_length(len(encoded), byte_limit, label="TBL 인코딩 결과")
        return encoded

    def decode(self, data: bytes | bytearray | memoryview) -> str:
        """바이트열 전체를 변환하며 미등록 바이트를 절대로 건너뛰지 않는다."""

        try:
            raw = bytes(data)
        except (TypeError, ValueError) as exc:
            raise TypeError("디코딩 입력은 바이트 계열이어야 합니다.") from exc

        output: list[str] = []
        position = 0
        while position < len(raw):
            candidates = self._bytes_by_initial.get(raw[position], ())
            matched = next(
                (token for token in candidates if raw.startswith(token, position)),
                None,
            )
            if matched is None:
                raise UnmappedBytesError(raw, position, self.table.source)
            output.append(self.table.byte_to_text[matched])
            position += len(matched)
        return "".join(output)

    def encode_fixed(
        self,
        text: str,
        capacity: int,
        *,
        padding: bytes = b"\x00",
    ) -> bytes:
        """문자열을 고정 크기로 인코딩하고 남은 부분을 정확히 채운다."""

        encoded = self.encode(text, byte_limit=capacity)
        if not isinstance(padding, bytes) or not padding:
            raise CodecError("padding은 비어 있지 않은 bytes여야 합니다.")
        remaining = capacity - len(encoded)
        if remaining % len(padding):
            raise CodecError(
                f"남은 {remaining}바이트를 {len(padding)}바이트 padding으로 정확히 채울 수 없습니다."
            )
        return encoded + padding * (remaining // len(padding))


def encode_shift_jis_strict(text: str, *, byte_limit: int | None = None) -> bytes:
    """표준 Shift-JIS로 변환하며 대체 문자나 무시 옵션을 사용하지 않는다."""

    if not isinstance(text, str):
        raise TypeError("인코딩 입력은 문자열이어야 합니다.")
    try:
        encoded = text.encode("shift_jis", errors="strict")
    except UnicodeEncodeError as exc:
        character = text[exc.start : exc.end]
        raise CodecError(
            f"Shift-JIS에 등록되지 않은 문자가 위치 {exc.start}에 있습니다: {character!r}"
        ) from exc

    # 구현 또는 플랫폼이 바뀌어도 전각 공백을 잘못된 3바이트로 만들지 못하게 한다.
    if "\u3000" in text and "\u3000".encode("shift_jis") != SHIFT_JIS_FULLWIDTH_SPACE:
        raise CodecError("현재 환경의 Shift-JIS 전각 공백 매핑이 81 40이 아닙니다.")
    if byte_limit is not None:
        validate_byte_length(len(encoded), byte_limit, label="Shift-JIS 인코딩 결과")
    return encoded


def decode_shift_jis_strict(data: bytes | bytearray | memoryview) -> str:
    """표준 Shift-JIS 바이트열을 대체 없이 변환한다."""

    try:
        raw = bytes(data)
    except (TypeError, ValueError) as exc:
        raise TypeError("디코딩 입력은 바이트 계열이어야 합니다.") from exc
    try:
        return raw.decode("shift_jis", errors="strict")
    except UnicodeDecodeError as exc:
        fragment = raw[exc.start : exc.end].hex().upper()
        raise CodecError(
            f"올바르지 않은 Shift-JIS 바이트가 위치 0x{exc.start:X}에 있습니다: {fragment}"
        ) from exc


__all__ = [
    "CodecError",
    "LengthOverflowError",
    "SHIFT_JIS_FULLWIDTH_SPACE",
    "TableCodec",
    "UnmappedBytesError",
    "UnmappedTextError",
    "decode_shift_jis_strict",
    "encode_shift_jis_strict",
]
