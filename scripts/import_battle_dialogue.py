#!/usr/bin/env python3
"""SRVC 전투 대사 XLSX와 적용 BIN을 공개용 JSON으로 정리한다.

원본 XLSX·BIN은 지정한 외부 경로에서 읽기만 한다. JSON에는 일본어 원문과
한국어 번역의 쌍, 고정 길이 델타 페이로드, 원본 페이로드 해시를 함께 기록한다.
게임 원본·완성 BIN 자체는 ``translations/`` 아래에 복사하지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys
import unicodedata
from typing import Iterable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from siok_patch.xlsx_reader import XlsxRow, XlsxWorkbook, open_xlsx  # noqa: E402
from siok_patch.text_normalization import GAME_HALF_WIDTH_SPACE_BYTES  # noqa: E402


FORMAT = "siok.srvc-battle-dialogue"
FORMAT_VERSION = 2
NORMALIZATION_PROFILE = "srvc-hangul-wreplace-v1"
_BINARY_SUFFIXES = {".bin", ".cpk", ".vpk", ".xdelta", ".exe", ".dll"}
_SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".xlsm"}

_MASTER_HEADERS = (
    "ID",
    "최초 오프셋",
    "원문(일본어)",
    "새 번역(한국어)",
    "원문 바이트",
    "번역 바이트",
    "여유 바이트",
    "반복 횟수",
    "상태",
    "검수 메모",
)
_SLOT_HEADERS = (
    "슬롯 번호",
    "오프셋",
    "마스터 ID",
    "원문",
    "원문 바이트",
    "1차 반영",
)


class BattleDialogueError(ValueError):
    """전투 대사 입력이 공개용 매니페스트로 안전하게 정리되지 않을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class MasterRow:
    """마스터 번역표의 내부 행 표현."""

    row_number: int
    identifier: int
    offset: int
    source_text: str
    translation: str
    source_bytes: int
    translation_bytes: int
    spare_bytes: int
    repeat_count: int
    status: str
    note: str

    def public_dict(
        self,
        *,
        original_payload: bytes,
        applied_payload: bytes,
    ) -> dict[str, object]:
        """원문·번역 쌍과 재현에 필요한 고정 길이 델타를 공개한다."""

        return {
            "id": self.identifier,
            "offset": self.offset,
            "sourceText": self.source_text,
            "translation": self.translation,
            "sourceTextSha256": sha256(self.source_text.encode("utf-8")).hexdigest(),
            "sourceByteLength": self.source_bytes,
            "translationByteLength": self.translation_bytes,
            "spareBytes": self.spare_bytes,
            "repeatCount": self.repeat_count,
            "status": self.status,
            "reviewNote": self.note,
            "originalPayloadSha256": sha256(original_payload).hexdigest(),
            "appliedPayloadSha256": sha256(applied_payload).hexdigest(),
            "appliedPayloadHex": applied_payload.hex(),
        }


@dataclass(frozen=True, slots=True)
class SlotRow:
    """슬롯 위치 표의 내부 행 표현."""

    slot_number: int
    offset: int
    master_id: int
    source_text: str
    source_bytes: int
    applied: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_number,
            "offset": self.offset,
            "masterId": self.master_id,
            "sourceByteLength": self.source_bytes,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class _Decoder:
    """반복 검증에서 재사용하는 인코딩·치환 표."""

    table: Mapping[bytes, str]
    keys_by_first_byte: Mapping[int, tuple[bytes, ...]]
    character_replacements: Mapping[int, str]
    remaining_replacements: tuple[tuple[str, str], ...]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_file():
        raise BattleDialogueError(f"{label} 파일을 찾을 수 없습니다: {candidate}")
    return candidate


def _safe_output(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_relative_to(REPOSITORY_ROOT):
        raise BattleDialogueError(
            "출력은 이 저장소 안에서만 만들 수 있습니다: " f"{candidate}"
        )
    if candidate.suffix.lower() in _BINARY_SUFFIXES | _SPREADSHEET_SUFFIXES:
        raise BattleDialogueError(
            "게임 바이너리나 XLSX를 출력할 수 없습니다: " f"{candidate.name}"
        )
    if candidate.name.startswith("."):
        raise BattleDialogueError("숨김 파일을 공개 산출물로 만들 수 없습니다.")
    return candidate


def _parse_integer(value: str, label: str, *, minimum: int = 0) -> int:
    text = value.strip()
    if not text:
        raise BattleDialogueError(f"{label} 값이 비어 있습니다.")
    try:
        base = 16 if text.lower().startswith("0x") else 10
        number = int(text, base)
    except ValueError as error:
        raise BattleDialogueError(f"{label} 값이 정수가 아닙니다: {value!r}") from error
    if number < minimum:
        raise BattleDialogueError(f"{label} 값이 {minimum}보다 작습니다: {value!r}")
    return number


def _header_index(row: XlsxRow, headers: Sequence[str], label: str) -> dict[str, int]:
    positions: dict[str, int] = {}
    for header in headers:
        matches = [index for index, value in enumerate(row.values) if value == header]
        if len(matches) != 1:
            raise BattleDialogueError(
                f"{label} 헤더에서 {header!r} 열을 정확히 하나 찾지 못했습니다."
            )
        positions[header] = matches[0]
    return positions


def _find_sheet(
    workbook: XlsxWorkbook,
    headers: Sequence[str],
    label: str,
) -> tuple[str, int, dict[str, int]]:
    matches: list[tuple[str, int, dict[str, int]]] = []
    for sheet in workbook.sheets:
        for row in workbook.iter_rows(sheet):
            if all(header in row.values for header in headers):
                matches.append(
                    (sheet.name, row.number, _header_index(row, headers, label))
                )
                break
    if len(matches) != 1:
        names = ", ".join(item[0] for item in matches) or "(없음)"
        raise BattleDialogueError(
            f"{label} 시트를 하나만 인식해야 합니다. 인식 결과: {names}"
        )
    return matches[0]


def _row_value(row: XlsxRow, positions: Mapping[str, int], header: str, label: str) -> str:
    index = positions[header]
    if index >= len(row.values):
        raise BattleDialogueError(f"{label}에서 {header!r} 열이 누락되었습니다.")
    return row.values[index]


def _iter_data_rows(
    workbook: XlsxWorkbook,
    sheet_name: str,
    header_row: int,
) -> Iterable[XlsxRow]:
    for row in workbook.iter_rows(sheet_name):
        if row.number <= header_row:
            continue
        if not any(value.strip() for value in row.values):
            continue
        yield row


def _read_master_rows(
    workbook: XlsxWorkbook,
    sheet_name: str,
    header_row: int,
    positions: Mapping[str, int],
) -> tuple[MasterRow, ...]:
    result: list[MasterRow] = []
    for expected_id, row in enumerate(
        _iter_data_rows(workbook, sheet_name, header_row), start=1
    ):
        label = f"마스터 번역표 {row.number}행"
        identifier = _parse_integer(_row_value(row, positions, "ID", label), f"{label} ID", minimum=1)
        if identifier != expected_id:
            raise BattleDialogueError(
                f"{label} ID가 연속되지 않습니다: 기대 {expected_id}, 실제 {identifier}"
            )
        source = _row_value(row, positions, "원문(일본어)", label)
        translation = _row_value(row, positions, "새 번역(한국어)", label)
        if not source:
            raise BattleDialogueError(f"{label} 원문이 비어 있습니다.")
        if not translation:
            raise BattleDialogueError(f"{label} 번역이 비어 있습니다.")
        source_bytes = _parse_integer(
            _row_value(row, positions, "원문 바이트", label), f"{label} 원문 바이트"
        )
        translation_bytes = _parse_integer(
            _row_value(row, positions, "번역 바이트", label), f"{label} 번역 바이트"
        )
        spare_bytes = _parse_integer(
            _row_value(row, positions, "여유 바이트", label), f"{label} 여유 바이트"
        )
        if translation_bytes > source_bytes:
            raise BattleDialogueError(f"{label} 번역 바이트가 원문보다 큽니다.")
        if source_bytes - translation_bytes != spare_bytes:
            raise BattleDialogueError(
                f"{label} 여유 바이트가 원문·번역 바이트 차이와 다릅니다."
            )
        result.append(
            MasterRow(
                row_number=row.number,
                identifier=identifier,
                offset=_parse_integer(
                    _row_value(row, positions, "최초 오프셋", label),
                    f"{label} 최초 오프셋",
                ),
                source_text=source,
                translation=translation,
                source_bytes=source_bytes,
                translation_bytes=translation_bytes,
                spare_bytes=spare_bytes,
                repeat_count=_parse_integer(
                    _row_value(row, positions, "반복 횟수", label), f"{label} 반복 횟수"
                ),
                status=_row_value(row, positions, "상태", label),
                note=_row_value(row, positions, "검수 메모", label),
            )
        )
    if not result:
        raise BattleDialogueError("마스터 번역표에 데이터 행이 없습니다.")
    return tuple(result)


def _read_slot_rows(
    workbook: XlsxWorkbook,
    sheet_name: str,
    header_row: int,
    positions: Mapping[str, int],
    masters: Sequence[MasterRow],
) -> tuple[SlotRow, ...]:
    result: list[SlotRow] = []
    for expected_slot, row in enumerate(
        _iter_data_rows(workbook, sheet_name, header_row), start=1
    ):
        label = f"슬롯 위치 {row.number}행"
        slot_number = _parse_integer(
            _row_value(row, positions, "슬롯 번호", label), f"{label} 슬롯 번호", minimum=1
        )
        if slot_number != expected_slot:
            raise BattleDialogueError(
                f"{label} 슬롯 번호가 연속되지 않습니다: 기대 {expected_slot}, 실제 {slot_number}"
            )
        master_id = _parse_integer(
            _row_value(row, positions, "마스터 ID", label), f"{label} 마스터 ID", minimum=1
        )
        if master_id > len(masters):
            raise BattleDialogueError(f"{label} 마스터 ID가 범위를 벗어났습니다: {master_id}")
        source = _row_value(row, positions, "원문", label)
        if source != masters[master_id - 1].source_text:
            raise BattleDialogueError(
                f"{label} 원문이 마스터 ID {master_id}의 원문과 다릅니다."
            )
        source_bytes = _parse_integer(
            _row_value(row, positions, "원문 바이트", label), f"{label} 원문 바이트"
        )
        if source_bytes != masters[master_id - 1].source_bytes:
            raise BattleDialogueError(
                f"{label} 원문 바이트가 마스터 ID {master_id}와 다릅니다."
            )
        applied_text = _row_value(row, positions, "1차 반영", label).strip()
        if applied_text != "예":
            raise BattleDialogueError(f"{label} 반영 상태가 '예'가 아닙니다: {applied_text!r}")
        result.append(
            SlotRow(
                slot_number=slot_number,
                offset=_parse_integer(
                    _row_value(row, positions, "오프셋", label), f"{label} 오프셋"
                ),
                master_id=master_id,
                source_text=source,
                source_bytes=source_bytes,
                applied=True,
            )
        )
    if not result:
        raise BattleDialogueError("슬롯 위치 표에 데이터 행이 없습니다.")
    return tuple(result)


def _load_encoding_table(path: Path) -> dict[bytes, str]:
    try:
        lines = path.read_text(encoding="utf-16").splitlines()
    except UnicodeError as error:
        raise BattleDialogueError(f"인코딩 표를 UTF-16으로 읽을 수 없습니다: {path}") from error
    table: dict[bytes, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if "=" not in line:
            continue
        key_text, value = line.split("=", 1)
        try:
            key = bytes.fromhex(key_text.strip())
        except ValueError as error:
            raise BattleDialogueError(
                f"인코딩 표 {line_number}행의 바이트 키가 잘못되었습니다: {key_text!r}"
            ) from error
        if not key or not value:
            raise BattleDialogueError(f"인코딩 표 {line_number}행이 비어 있습니다.")
        table[key] = value
    if not table:
        raise BattleDialogueError(f"인코딩 표에 항목이 없습니다: {path}")
    return table


def _load_wreplace(path: Path) -> tuple[dict[str, str], int]:
    try:
        lines = path.read_text(encoding="utf-16").splitlines()
    except UnicodeError as error:
        raise BattleDialogueError(f"wReplace 표를 UTF-16으로 읽을 수 없습니다: {path}") from error
    candidates: dict[str, list[str]] = {}
    for line in lines:
        columns = line.split("\t")
        if len(columns) < 2 or columns[0].startswith("#"):
            continue
        left, right = columns[0], columns[1]
        if right:
            candidates.setdefault(right, []).append(left)

    def preference(value: str) -> tuple[int, int, str]:
        return (0 if all(ord(char) < 128 for char in value) else 1, len(value), value)

    chosen = {right: min(values, key=preference) for right, values in candidates.items()}
    duplicates = sum(1 for values in candidates.values() if len(set(values)) > 1)
    if not chosen:
        raise BattleDialogueError(f"wReplace 표에 항목이 없습니다: {path}")
    return chosen, duplicates


def _compile_decoder(
    table: Mapping[bytes, str], replacement: Mapping[str, str]
) -> _Decoder:
    by_first: dict[int, list[bytes]] = {}
    for key in table:
        by_first.setdefault(key[0], []).append(key)
    keys_by_first = {
        first: tuple(sorted(keys, key=len, reverse=True))
        for first, keys in by_first.items()
    }
    character_replacements = {
        ord(key): value for key, value in replacement.items() if len(key) == 1
    }
    remaining = tuple(
        sorted(
            (
                (key, value)
                for key, value in replacement.items()
                if len(key) != 1
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
    return _Decoder(table, keys_by_first, character_replacements, remaining)


def _decode_payload(payload: bytes, decoder: _Decoder) -> str:
    decoded: list[str] = []
    position = 0
    while position < len(payload):
        if payload.startswith(GAME_HALF_WIDTH_SPACE_BYTES, position):
            decoded.append(" ")
            position += len(GAME_HALF_WIDTH_SPACE_BYTES)
            continue
        byte = payload[position]
        if byte == 0:
            break
        for key in decoder.keys_by_first_byte.get(byte, ()):
            if payload.startswith(key, position):
                decoded.append(decoder.table[key])
                position += len(key)
                break
        else:
            decoded.append(chr(byte) if 0x20 <= byte <= 0x7E else f"<{byte:02X}>")
            position += 1
    value = "".join(decoded)
    if decoder.character_replacements:
        value = value.translate(decoder.character_replacements)
    for key, replacement_value in decoder.remaining_replacements:
        value = value.replace(key, replacement_value)
    return value


def _normalise_for_comparison(value: str, *, drop_spaces: bool) -> str:
    """게임용 표기 차이를 제거하되 번역 내용은 바꾸지 않는다."""

    normalised = unicodedata.normalize("NFKC", value)
    normalised = (
        normalised.replace("\u3001", ",")
        .replace("\u3002", ".")
        .replace("\u30fb", "\u00b7")
        .replace("\u3000", " ")
        .replace("\n", "\\n")
        .replace("\u25bd", "~")
    )
    if drop_spaces:
        normalised = normalised.replace(" ", "")
    return normalised


def _changed_byte_summary(original: bytes, applied: bytes) -> dict[str, object]:
    if len(original) != len(applied):
        raise BattleDialogueError(
            f"원본·적용 BIN 크기가 다릅니다: {len(original)} != {len(applied)}"
        )
    ranges: list[tuple[int, int]] = []
    changed_bytes = 0
    start: int | None = None
    for index, (left, right) in enumerate(zip(original, applied, strict=True)):
        if left != right:
            changed_bytes += 1
            if start is None:
                start = index
        elif start is not None:
            ranges.append((start, index - 1))
            start = None
    if start is not None:
        ranges.append((start, len(original) - 1))
    return {
        "changedByteCount": changed_bytes,
        "changedRangeCount": len(ranges),
        "firstChangedRanges": [
            {"start": start, "end": end, "length": end - start + 1}
            for start, end in ranges[:10]
        ],
    }


def _verify_applied_bin(
    masters: Sequence[MasterRow],
    slots: Sequence[SlotRow],
    applied: bytes,
    table: Mapping[bytes, str],
    replacement: Mapping[str, str],
) -> dict[str, object]:
    decoder = _compile_decoder(table, replacement)

    def compare(offset: int, source_bytes: int, translation: str, label: str) -> tuple[bool, bool]:
        end = offset + source_bytes
        if offset < 0 or end > len(applied):
            raise BattleDialogueError(
                f"{label}의 BIN 범위가 파일 밖입니다: 0x{offset:X}..0x{end:X}"
            )
        actual = _decode_payload(applied[offset:end], decoder)
        strict_expected = _normalise_for_comparison(translation, drop_spaces=False)
        strict_actual = _normalise_for_comparison(actual, drop_spaces=False)
        semantic_expected = _normalise_for_comparison(translation, drop_spaces=True)
        semantic_actual = _normalise_for_comparison(actual, drop_spaces=True)
        return strict_expected == strict_actual, semantic_expected == semantic_actual

    strict_matches = 0
    semantic_matches = 0
    for master in masters:
        strict, semantic = compare(
            master.offset,
            master.source_bytes,
            master.translation,
            f"마스터 ID {master.identifier}",
        )
        strict_matches += strict
        semantic_matches += semantic
    slot_strict_matches = 0
    slot_semantic_matches = 0
    for slot in slots:
        master = masters[slot.master_id - 1]
        strict, semantic = compare(
            slot.offset,
            slot.source_bytes,
            master.translation,
            f"슬롯 {slot.slot_number}",
        )
        slot_strict_matches += strict
        slot_semantic_matches += semantic
    return {
        "strictLayoutMatches": strict_matches,
        "strictLayoutRows": len(masters),
        "semanticMatches": semantic_matches,
        "semanticRows": len(masters),
        "slotStrictLayoutMatches": slot_strict_matches,
        "slotStrictLayoutRows": len(slots),
        "slotSemanticMatches": slot_semantic_matches,
        "slotSemanticRows": len(slots),
        "normalizationProfile": NORMALIZATION_PROFILE,
        "normalizationRules": [
            "NFKC 호환 정규화",
            "일본어 쉼표·마침표·중간점과 게임용 전각 표기를 대응",
            "실제 줄바꿈과 게임 문자열의 '\\n' 표기를 대응",
            "게임 인코딩의 ▽와 번역표의 물결표를 대응",
            "의미 비교에서 ASCII·전각 공백을 제거",
        ],
    }


def build_public_document(
    *,
    workbook_path: Path,
    applied_bin_path: Path,
    original_bin_path: Path,
    encoding_table_path: Path,
    wreplace_path: Path,
) -> dict[str, object]:
    workbook_path = _require_file(workbook_path, "번역 XLSX")
    applied_bin_path = _require_file(applied_bin_path, "적용 BIN")
    original_bin_path = _require_file(original_bin_path, "원본 BIN")
    encoding_table_path = _require_file(encoding_table_path, "인코딩 표")
    wreplace_path = _require_file(wreplace_path, "wReplace 표")

    workbook = open_xlsx(workbook_path)
    master_sheet, master_header, master_positions = _find_sheet(
        workbook, _MASTER_HEADERS, "마스터 번역표"
    )
    slot_sheet, slot_header, slot_positions = _find_sheet(
        workbook, _SLOT_HEADERS, "슬롯 위치"
    )
    masters = _read_master_rows(workbook, master_sheet, master_header, master_positions)
    slots = _read_slot_rows(workbook, slot_sheet, slot_header, slot_positions, masters)

    applied = applied_bin_path.read_bytes()
    original = original_bin_path.read_bytes()
    table = _load_encoding_table(encoding_table_path)
    replacement, duplicate_targets = _load_wreplace(wreplace_path)
    verification = _verify_applied_bin(masters, slots, applied, table, replacement)
    verification.update(_changed_byte_summary(original, applied))
    verification["wReplaceDuplicateTargets"] = duplicate_targets

    public_masters: list[dict[str, object]] = []
    for master in masters:
        end = master.offset + master.source_bytes
        if end > len(original) or end > len(applied):
            raise BattleDialogueError(
                f"마스터 ID {master.identifier}의 페이로드 범위가 파일 밖입니다: "
                f"0x{master.offset:X}..0x{end:X}"
            )
        original_payload = original[master.offset:end]
        applied_payload = applied[master.offset:end]
        public_masters.append(
            master.public_dict(
                original_payload=original_payload,
                applied_payload=applied_payload,
            )
        )

    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "gameId": "PCSG00264",
        "languages": {
            "source": "ja",
            "target": "ko",
        },
        "payloadFormat": "srvc-fixed-slot-delta-v1",
        "asset": {
            "fileName": "SRVC.BIN",
            "bytes": len(applied),
            "originalSha256": _sha256_file(original_bin_path),
            "appliedSha256": _sha256_file(applied_bin_path),
        },
        "workbook": {
            "fileName": workbook_path.name,
            "sha256": _sha256_file(workbook_path),
            "masterSheet": master_sheet,
            "masterHeaderRow": master_header,
            "slotSheet": slot_sheet,
            "slotHeaderRow": slot_header,
        },
        "encoding": {
            "tableFileName": encoding_table_path.name,
            "tableSha256": _sha256_file(encoding_table_path),
            "wReplaceFileName": wreplace_path.name,
            "wReplaceSha256": _sha256_file(wreplace_path),
        },
        "policy": {
            "originalJapaneseTextIncluded": True,
            "gameBinaryIncluded": False,
            "sourcePathsIncluded": False,
            "translationStatusIsPreserved": True,
            "requiresUserOwnedGame": True,
            "securityNotice": (
                "본인이 합법적으로 보유·덤프한 PCSG00264 게임 파일에만 사용하며, "
                "원본 파일은 저장소에 업로드하지 않는다."
            ),
            "copyrightNotice": (
                "일본어 원문·게임 자료의 권리는 각 권리자에게 있으며, "
                "이 JSON은 자기 소유 게임의 개인 패치 작업을 위한 것이다."
            ),
        },
        "counts": {
            "masterRows": len(masters),
            "slotRows": len(slots),
            "uniqueSourceRows": len({master.source_text for master in masters}),
            "appliedSlots": sum(slot.applied for slot in slots),
            "translationByteMismatchRows": 0,
            "payloadRows": len(masters),
        },
        "verification": verification,
        "master": public_masters,
        "slots": [slot.public_dict() for slot in slots],
    }


def write_document(document: Mapping[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, required=True, help="외부 번역 XLSX")
    parser.add_argument("--applied-bin", type=Path, required=True, help="적용된 SRVC.BIN")
    parser.add_argument("--original-bin", type=Path, required=True, help="비교할 원본 SRVC BIN")
    parser.add_argument("--encoding-table", type=Path, required=True, help="shift-jis2.tbl")
    parser.add_argument("--wreplace", type=Path, required=True, help="Hangul-to-Kanji wReplace")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("translations/battle-dialogue/srvc/SRVC_BATTLE.json"),
        help="저장소 안의 공개 JSON 경로",
    )
    args = parser.parse_args(argv)
    output = _safe_output(args.output)
    try:
        document = build_public_document(
            workbook_path=args.workbook,
            applied_bin_path=args.applied_bin,
            original_bin_path=args.original_bin,
            encoding_table_path=args.encoding_table,
            wreplace_path=args.wreplace,
        )
        write_document(document, output)
    except (BattleDialogueError, OSError) as error:
        parser.error(str(error))
    summary = {
        "output": str(output),
        "masterRows": document["counts"]["masterRows"],
        "slotRows": document["counts"]["slotRows"],
        "semanticMatches": document["verification"]["semanticMatches"],
        "strictLayoutMatches": document["verification"]["strictLayoutMatches"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
