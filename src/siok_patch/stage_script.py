"""STAGE 대사 payload를 CP932 바이트 단위로 손실 없이 분해하고 재조립한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .text_normalization import encode_game_dialogue_spaces


class StageScriptError(ValueError):
    """대사 payload가 기존 형식 또는 안전 조건을 만족하지 못할 때 발생한다."""


class StageScriptDecodeError(StageScriptError):
    """원본 payload를 CP932로 엄격하게 읽을 수 없을 때 발생한다."""


class StageScriptRowCountError(StageScriptError):
    """추출 슬롯 수와 교체 행 수가 다를 때 발생한다."""


class StageScriptEncodeError(StageScriptError):
    """교체 문장을 CP932로 엄격하게 쓸 수 없을 때 발생한다."""


_SPEAKER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("  [[", "SP"),
    ("      [[", "SB"),
    ("          [[", "SF"),
    ("              [[", "SG"),
)
_DIALOGUE_TAGS = frozenset(
    {"SP", "SB", "SF", "SG", "S1", "S2", "S3", "SM", "SE", "ST"}
)
_FULLWIDTH_SPACE = "\u3000"


@dataclass(frozen=True, slots=True)
class _PhysicalLine:
    """본문과 줄바꿈을 분리해 보관하는 원본 물리 행."""

    body: bytes
    ending: bytes

    @property
    def raw(self) -> bytes:
        return self.body + self.ending


@dataclass(frozen=True, slots=True)
class DialogueSlot:
    """번역 XLSX의 한 행과 일대일로 대응하는 대사 슬롯."""

    ordinal: int
    line_index: int
    tag: str
    source_text: str
    raw_line: bytes
    ending: bytes

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise StageScriptError("대사 슬롯 번호는 1 이상이어야 합니다.")
        if self.line_index < 0:
            raise StageScriptError("원본 행 인덱스는 0 이상이어야 합니다.")
        if self.tag not in _DIALOGUE_TAGS:
            raise StageScriptError(f"알 수 없는 대사 태그입니다: {self.tag!r}")

    @property
    def line_number(self) -> int:
        """사람이 확인하기 쉬운 1부터 시작하는 원본 행 번호."""

        return self.line_index + 1

    @property
    def text(self) -> str:
        """`source_text`의 짧은 읽기 전용 별칭."""

        return self.source_text


@dataclass(frozen=True, slots=True)
class ParsedStageScript:
    """원본 바이트와 대사 슬롯을 함께 보존한 파싱 결과."""

    original_bytes: bytes
    lines: tuple[_PhysicalLine, ...]
    slots: tuple[DialogueSlot, ...]
    encoding: str = "cp932"

    @property
    def source_rows(self) -> tuple[str, ...]:
        """XLSX 원문 열과 순서대로 비교할 문자열 목록."""

        return tuple(slot.source_text for slot in self.slots)

    @property
    def dialogue_rows(self) -> tuple[str, ...]:
        """`source_rows`의 호환용 읽기 전용 별칭."""

        return self.source_rows


def _split_physical_lines(data: bytes) -> tuple[_PhysicalLine, ...]:
    """CRLF/LF와 마지막 줄바꿈 유무를 바이트 그대로 분리한다."""

    if not data:
        return ()

    lines: list[_PhysicalLine] = []
    start = 0
    while True:
        newline = data.find(b"\n", start)
        if newline < 0:
            if start < len(data):
                lines.append(_PhysicalLine(data[start:], b""))
            break

        if newline > start and data[newline - 1] == 0x0D:
            lines.append(_PhysicalLine(data[start : newline - 1], b"\r\n"))
        else:
            lines.append(_PhysicalLine(data[start:newline], b"\n"))
        start = newline + 1
        if start == len(data):
            break
    return tuple(lines)


def _classify_line(text: str) -> tuple[str, str] | None:
    """기존 시옥편 스크립트와 같은 태그 및 추출 문자열을 돌려준다."""

    for prefix, tag in _SPEAKER_PREFIXES:
        if text.startswith(prefix):
            return tag, text[len(prefix) :]

    if text.startswith("「"):
        if text.endswith("」]],"):
            return "S1", text[1:-4]
        # 기존 후처리의 `～」 → ～`가 적용된 생성물도 손실 없이 재검증한다.
        if text.endswith("]],"):
            return "S1", text[1:-3]
        return "S2", text[1:]

    if text.startswith("（"):
        if text.endswith("]],"):
            return "S3", text[:-3]
        # 생각 대사의 여러 줄 첫 행은 여는 괄호를 원문 셀에 남긴다.
        return "S2", text

    if text.startswith(_FULLWIDTH_SPACE):
        if text.endswith("」]],"):
            return "SE", text[1:-4]
        if text.endswith("]],"):
            return "ST", text[1:-3]
        # 전각 공백 하나뿐이어도 빈 source_text인 실제 슬롯으로 유지한다.
        return "SM", text[1:]

    return None


def parse_stage_script(
    data: bytes | bytearray | memoryview,
    *,
    encoding: str = "cp932",
) -> ParsedStageScript:
    """CP932 payload에서 대사 슬롯을 추출하되 원본 바이트를 모두 보존한다."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("대사 payload는 바이트 계열이어야 합니다.")
    if not isinstance(encoding, str) or not encoding:
        raise TypeError("encoding은 비어 있지 않은 문자열이어야 합니다.")

    raw = bytes(data)
    lines = _split_physical_lines(raw)
    slots: list[DialogueSlot] = []

    for line_index, line in enumerate(lines):
        try:
            text = line.body.decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            fragment = line.body[exc.start : exc.end].hex().upper()
            raise StageScriptDecodeError(
                f"원본 {line_index + 1}행을 {encoding.upper()}로 읽을 수 없습니다: "
                f"행 안의 바이트 위치 0x{exc.start:X}, 값 {fragment}"
            ) from exc

        classified = _classify_line(text)
        if classified is None:
            continue
        tag, source_text = classified
        slots.append(
            DialogueSlot(
                ordinal=len(slots) + 1,
                line_index=line_index,
                tag=tag,
                source_text=source_text,
                raw_line=line.raw,
                ending=line.ending,
            )
        )

    return ParsedStageScript(
        original_bytes=raw,
        lines=lines,
        slots=tuple(slots),
        encoding=encoding,
    )


def _normalize_inserted_text(text: str) -> str:
    """기존 빌드의 제한된 보정을 새로 삽입한 한 행에만 적용한다."""

    replacements = (
        ("$ｎ", "$n"),
        ("$ｌ", "$l"),
        ("$ｃ", "$c"),
        ("$Ｆ", "$F"),
        ("「（", "（"),
        ("）」", "）"),
        ("～」", "～"),
    )
    normalized = text
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    return normalized


def normalize_replacement_source(replacement: str, tag: str) -> str:
    """재조립한 행을 다시 읽었을 때 기대되는 대사 문자열을 반환한다.

    기존 스크립트 빌드가 새로 삽입한 문자열에 적용하는 제한된 전각 제어문자
    보정을 한 곳에서 공유한다. 화자 슬롯의 빈 문자열은 실제로 전각 공백 다섯
    칸으로 기록되므로 그 값까지 반영한다.
    """

    if not isinstance(replacement, str):
        raise TypeError("replacement는 문자열이어야 합니다.")
    if tag not in _DIALOGUE_TAGS:
        raise StageScriptError(f"알 수 없는 대사 태그입니다: {tag!r}")
    normalized = _normalize_inserted_text(encode_game_dialogue_spaces(replacement))
    if tag in {"SP", "SB", "SF", "SG"} and normalized == "":
        return _FULLWIDTH_SPACE * 5
    return normalized


def _render_slot(slot: DialogueSlot, replacement: str) -> str:
    speaker_prefixes = {
        "SP": "  [[",
        "SB": "      [[",
        "SF": "          [[",
        "SG": "              [[",
    }
    # 화자·줄 구조를 만드는 전각 공백은 게임 스크립트 문법의 일부다.
    # 번역문 공백만 FE FE 게임 글리프로 바꾸고 이 접두부는 그대로 보존한다.
    replacement = encode_game_dialogue_spaces(replacement)
    if slot.tag in speaker_prefixes:
        prefix = speaker_prefixes[slot.tag]
        rendered = prefix + replacement
        if not replacement:
            rendered += _FULLWIDTH_SPACE * 5
    elif slot.tag == "S1":
        rendered = f"「{replacement}」]],"
    elif slot.tag == "S2":
        rendered = f"「{replacement}"
    elif slot.tag == "S3":
        rendered = f"{replacement}]],"
    elif slot.tag == "SM":
        rendered = f"{_FULLWIDTH_SPACE}{replacement}"
    elif slot.tag == "SE":
        rendered = f"{_FULLWIDTH_SPACE}{replacement}」]],"
    elif slot.tag == "ST":
        rendered = f"{_FULLWIDTH_SPACE}{replacement}]],"
    else:  # pragma: no cover - DialogueSlot 자체 검증의 방어선이다.
        raise StageScriptError(f"알 수 없는 대사 태그입니다: {slot.tag!r}")
    return _normalize_inserted_text(rendered)


def rebuild_stage_script(
    parsed: ParsedStageScript,
    replacements: Iterable[str],
    *,
    encoding: str | None = None,
) -> bytes:
    """대사 행을 정확히 일대일 치환하고 새 payload 바이트를 만든다.

    교체값이 원문과 같은 슬롯은 재인코딩하지 않고 원본 행 바이트를 그대로
    사용한다. 따라서 무수정 라운드트립은 줄바꿈을 포함해 완전히 동일하다.
    """

    if not isinstance(parsed, ParsedStageScript):
        raise TypeError("parsed는 ParsedStageScript여야 합니다.")
    if isinstance(replacements, (str, bytes, bytearray)):
        raise TypeError(
            "replacements는 문자열 한 개가 아니라 문자열 행의 반복값이어야 합니다."
        )
    try:
        rows = tuple(replacements)
    except TypeError as exc:
        raise TypeError("replacements는 문자열 행의 반복값이어야 합니다.") from exc

    expected = len(parsed.slots)
    actual = len(rows)
    if actual != expected:
        raise StageScriptRowCountError(
            f"대사 교체 행 수가 맞지 않습니다: 원본 슬롯 {expected}개, 교체 행 {actual}개"
        )

    selected_encoding = parsed.encoding if encoding is None else encoding
    if not isinstance(selected_encoding, str) or not selected_encoding:
        raise TypeError("encoding은 비어 있지 않은 문자열이어야 합니다.")

    slot_by_line = {slot.line_index: slot for slot in parsed.slots}
    replacement_by_line = {
        slot.line_index: replacement
        for slot, replacement in zip(parsed.slots, rows, strict=True)
    }
    output = bytearray()

    for line_index, line in enumerate(parsed.lines):
        slot = slot_by_line.get(line_index)
        if slot is None:
            output.extend(line.raw)
            continue

        replacement = replacement_by_line[line_index]
        if not isinstance(replacement, str):
            raise TypeError(
                f"대사 슬롯 #{slot.ordinal}(원본 {slot.line_number}행, {slot.tag})의 "
                "교체값은 문자열이어야 합니다."
            )
        if "\r" in replacement or "\n" in replacement:
            raise StageScriptError(
                f"대사 슬롯 #{slot.ordinal}(원본 {slot.line_number}행, {slot.tag})의 "
                "교체값에는 줄바꿈을 넣을 수 없습니다."
            )

        if replacement == slot.source_text:
            output.extend(line.raw)
            continue

        rendered = _render_slot(slot, replacement)
        try:
            encoded = rendered.encode(selected_encoding, errors="strict")
        except UnicodeEncodeError as exc:
            character = rendered[exc.start : exc.end]
            raise StageScriptEncodeError(
                f"대사 슬롯 #{slot.ordinal}(원본 {slot.line_number}행, {slot.tag})을 "
                f"{selected_encoding.upper()}로 쓸 수 없습니다: "
                f"문자 위치 {exc.start}, 값 {character!r}"
            ) from exc
        output.extend(encoded)
        output.extend(line.ending)

    result = bytes(output)
    if rows == parsed.source_rows and result != parsed.original_bytes:
        raise StageScriptError("내부 오류: 무수정 라운드트립 결과가 원본 바이트와 다릅니다.")
    return result


__all__ = [
    "DialogueSlot",
    "ParsedStageScript",
    "StageScriptDecodeError",
    "StageScriptEncodeError",
    "StageScriptError",
    "StageScriptRowCountError",
    "normalize_replacement_source",
    "parse_stage_script",
    "rebuild_stage_script",
]
