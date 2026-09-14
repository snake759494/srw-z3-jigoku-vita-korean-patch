"""사용자 정의 ``HEX=문자`` TBL 파일을 손실 없이 엄격하게 읽는다."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
import hashlib
from pathlib import Path
from types import MappingProxyType


class TableError(ValueError):
    """TBL 파일을 안전하게 사용할 수 없을 때 발생한다."""


class TableFormatError(TableError):
    """TBL 행 형식이 잘못되었을 때 발생한다."""


class DuplicateByteSequenceError(TableError):
    """하나의 바이트열이 두 번 정의되었을 때 발생한다."""


class DuplicateTextError(TableError):
    """하나의 문자 토큰이 두 바이트열에 연결되었을 때 발생한다."""


def _location(source: str, line_number: int) -> str:
    return f"{source}:{line_number}"


class CharacterTable:
    """바이트열과 문자 토큰 사이의 일대일 불변 매핑."""

    __slots__ = (
        "_byte_to_text",
        "_text_to_bytes",
        "_source",
        "_fingerprint_sha256",
        "_byte_tokens",
        "_text_tokens",
    )

    def __init__(
        self,
        entries: Iterable[tuple[bytes, str]],
        *,
        source: str = "<메모리>",
    ) -> None:
        byte_to_text: dict[bytes, str] = {}
        text_to_bytes: dict[str, bytes] = {}

        for entry_number, entry in enumerate(entries, start=1):
            try:
                byte_sequence, text = entry
            except (TypeError, ValueError) as exc:
                raise TableFormatError(
                    f"{source}의 매핑 #{entry_number}는 (bytes, str) 쌍이어야 합니다."
                ) from exc

            if not isinstance(byte_sequence, bytes) or not byte_sequence:
                raise TableFormatError(
                    f"{source}의 매핑 #{entry_number} 바이트열은 비어 있지 않은 bytes여야 합니다."
                )
            if not isinstance(text, str) or text == "":
                raise TableFormatError(
                    f"{source}의 매핑 #{entry_number} 문자 토큰은 비어 있지 않은 문자열이어야 합니다."
                )
            if byte_sequence in byte_to_text:
                previous = byte_to_text[byte_sequence]
                raise DuplicateByteSequenceError(
                    f"{source}에 바이트열 {byte_sequence.hex().upper()}이 중복 정의되었습니다: "
                    f"{previous!r}, {text!r}"
                )
            if text in text_to_bytes:
                previous = text_to_bytes[text]
                raise DuplicateTextError(
                    f"{source}에 문자 토큰 {text!r}이 중복 정의되었습니다: "
                    f"{previous.hex().upper()}, {byte_sequence.hex().upper()}"
                )

            byte_to_text[byte_sequence] = text
            text_to_bytes[text] = byte_sequence

        if not byte_to_text:
            raise TableFormatError(f"{source}에 사용할 수 있는 매핑이 하나도 없습니다.")

        canonical = "".join(
            f"{sequence.hex().upper()}={byte_to_text[sequence]}\n"
            for sequence in sorted(byte_to_text)
        ).encode("utf-8")
        self._byte_to_text = MappingProxyType(byte_to_text)
        self._text_to_bytes = MappingProxyType(text_to_bytes)
        self._source = source
        self._fingerprint_sha256 = hashlib.sha256(canonical).hexdigest()
        self._byte_tokens = tuple(sorted(byte_to_text, key=lambda item: (-len(item), item)))
        self._text_tokens = tuple(sorted(text_to_bytes, key=lambda item: (-len(item), item)))

    @property
    def byte_to_text(self) -> Mapping[bytes, str]:
        return self._byte_to_text

    @property
    def text_to_bytes(self) -> Mapping[str, bytes]:
        return self._text_to_bytes

    @property
    def byte_tokens_longest_first(self) -> tuple[bytes, ...]:
        return self._byte_tokens

    @property
    def text_tokens_longest_first(self) -> tuple[str, ...]:
        return self._text_tokens

    @property
    def source(self) -> str:
        return self._source

    @property
    def fingerprint_sha256(self) -> str:
        """행 순서와 16진수 대소문자에 영향받지 않는 매핑 지문."""

        return self._fingerprint_sha256

    def __len__(self) -> int:
        return len(self._byte_to_text)

    def __iter__(self) -> Iterator[tuple[bytes, str]]:
        return iter(self._byte_to_text.items())


def parse_tbl_lines(
    lines: Iterable[str],
    *,
    source: str = "<메모리>",
) -> CharacterTable:
    """TBL 행을 읽는다. 빈 행과 ``#``, ``;``, ``//`` 주석만 건너뛴다."""

    entries: list[tuple[bytes, str]] = []
    byte_locations: dict[bytes, int] = {}
    text_locations: dict[str, int] = {}

    for line_number, raw_line in enumerate(lines, start=1):
        if not isinstance(raw_line, str):
            raise TableFormatError(
                f"{_location(source, line_number)}의 행이 문자열이 아닙니다."
            )

        line = raw_line.rstrip("\r\n")
        if line_number == 1:
            line = line.removeprefix("\ufeff")
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "//")):
            continue
        if "=" not in line:
            raise TableFormatError(
                f"{_location(source, line_number)}에 '=' 구분자가 없습니다."
            )

        raw_hex, text = line.split("=", 1)
        hex_text = raw_hex.strip()
        if hex_text.lower().startswith("0x"):
            hex_text = hex_text[2:]
        if not hex_text:
            raise TableFormatError(
                f"{_location(source, line_number)}의 바이트열이 비어 있습니다."
            )
        if len(hex_text) % 2:
            raise TableFormatError(
                f"{_location(source, line_number)}의 16진수 길이는 짝수여야 합니다: "
                f"{hex_text!r}"
            )
        try:
            byte_sequence = bytes.fromhex(hex_text)
        except ValueError as exc:
            raise TableFormatError(
                f"{_location(source, line_number)}의 바이트열이 올바른 16진수가 아닙니다: "
                f"{hex_text!r}"
            ) from exc
        if not byte_sequence:
            raise TableFormatError(
                f"{_location(source, line_number)}의 바이트열이 비어 있습니다."
            )
        if text == "":
            raise TableFormatError(
                f"{_location(source, line_number)}의 문자 토큰이 비어 있습니다."
            )

        if byte_sequence in byte_locations:
            first = byte_locations[byte_sequence]
            raise DuplicateByteSequenceError(
                f"{_location(source, line_number)}의 바이트열 "
                f"{byte_sequence.hex().upper()}이 {source}:{first}에서 이미 정의되었습니다."
            )
        if text in text_locations:
            first = text_locations[text]
            raise DuplicateTextError(
                f"{_location(source, line_number)}의 문자 토큰 {text!r}이 "
                f"{source}:{first}에서 이미 정의되었습니다."
            )

        byte_locations[byte_sequence] = line_number
        text_locations[text] = line_number
        entries.append((byte_sequence, text))

    return CharacterTable(entries, source=source)


def parse_tbl(text: str, *, source: str = "<메모리>") -> CharacterTable:
    """문자열 전체에서 TBL 매핑을 읽는다."""

    if not isinstance(text, str):
        raise TableFormatError("TBL 내용은 문자열이어야 합니다.")
    return parse_tbl_lines(text.splitlines(keepends=True), source=source)


def load_tbl(
    path: str | Path,
    *,
    encoding: str = "utf-8-sig",
) -> CharacterTable:
    """디스크의 TBL 파일을 지정 인코딩으로 엄격하게 읽는다."""

    table_path = Path(path)
    try:
        with table_path.open("r", encoding=encoding, errors="strict", newline="") as stream:
            return parse_tbl_lines(stream, source=str(table_path))
    except UnicodeError as exc:
        raise TableFormatError(
            f"{table_path}을 {encoding} 인코딩으로 읽을 수 없습니다."
        ) from exc
