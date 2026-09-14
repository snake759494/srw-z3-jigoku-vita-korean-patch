"""``MtZkn_KW.cpk`` 사전 데이터의 추출·검증·고정 슬롯 코덱.

이 모듈은 게임 파일을 저장하거나 재패킹하지 않는다. 사용자가 제공한 자기
소유의 CPK를 읽고, 멤버의 위치와 크기를 확인한 뒤 ``0x5E`` XOR로 보호된
``ZKANKYWD`` 레코드를 해석한다. 적용 단계에서는 JSON에 보관된 고정 길이
페이로드만 원본 CPK의 해당 멤버에 덮어 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
from typing import Iterable, Mapping, Sequence


XOR_KEY = 0x5E
MEMBER_MAGIC = b"ZKANKYWD"
SUPPORTED_FIELDS = (b"WORD", b"SRCE", b"DSCR", b"DSC2")
ALIGNMENT = 0x10


class DictionaryError(ValueError):
    """사전 CPK가 예상한 형식·보안 조건을 만족하지 않을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class CpkMember:
    """ITOC에 기록된 한 멤버."""

    identifier: int
    offset: int
    size: int
    extract_size: int


@dataclass(frozen=True, slots=True)
class DictionaryField:
    """XOR 해제된 멤버 안의 태그 페이로드."""

    member_id: int
    tag: str
    offset: int
    payload_offset: int
    capacity: int
    payload: bytes


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _be16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise DictionaryError(f"@UTF 16비트 값이 파일 밖입니다: 0x{offset:X}")
    return struct.unpack_from(">H", data, offset)[0]


def _be32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise DictionaryError(f"@UTF 32비트 값이 파일 밖입니다: 0x{offset:X}")
    return struct.unpack_from(">I", data, offset)[0]


def _find_utf(data: bytes, start: int) -> int:
    offset = data.find(b"@UTF", start)
    if offset < 0:
        raise DictionaryError("CPK에서 @UTF 표를 찾지 못했습니다.")
    return offset


def _parse_itoc_rows(data: bytes) -> tuple[int, tuple[tuple[int, int, int], ...]]:
    """첫 ITOC의 CpkItocL @UTF 표에서 ID/크기 행을 읽는다.

    CRI @UTF 헤더의 오프셋 기준은 ``base + 8``이다. ITOC 표의 첫 번째
    하위 표는 파일 ID, 압축 파일 크기, 추출 크기를 각각 big-endian
    uint16으로 보관한다. 이 게임의 사전은 압축되지 않아 두 크기가 같다.
    """

    itoc = data.find(b"ITOC")
    if itoc < 0:
        raise DictionaryError("CPK에서 ITOC 청크를 찾지 못했습니다.")
    root = _find_utf(data, itoc + 4)
    child = _find_utf(data, root + 4)
    rows_offset = _be16(data, child + 0x0A)
    row_length = _be16(data, child + 0x1A)
    row_count = _be32(data, child + 0x1C)
    if row_length != 6:
        raise DictionaryError(
            f"사전 ITOC 행 폭이 6바이트가 아닙니다: {row_length}"
        )
    if row_count <= 0 or row_count > 0x10000:
        raise DictionaryError(f"사전 ITOC 행 수가 비정상입니다: {row_count}")
    rows_base = child + 8 + rows_offset
    rows: list[tuple[int, int, int]] = []
    for index in range(row_count):
        row = rows_base + index * row_length
        identifier = _be16(data, row)
        file_size = _be16(data, row + 2)
        extract_size = _be16(data, row + 4)
        if file_size <= 0 or extract_size <= 0:
            raise DictionaryError(f"ITOC {identifier}행의 크기가 0입니다.")
        rows.append((identifier, file_size, extract_size))
    return child, tuple(rows)


def _itoc_row_size_offsets(data: bytes, count: int) -> tuple[tuple[int, int], ...]:
    """ITOC 하위 표의 FileSize/ExtractSize 셀 위치를 반환한다."""

    _child, rows = _parse_itoc_rows(data)
    if len(rows) != count:
        raise DictionaryError("ITOC 행 수와 멤버 수가 다릅니다.")
    itoc = data.find(b"ITOC")
    root = _find_utf(data, itoc + 4)
    child = _find_utf(data, root + 4)
    rows_offset = _be16(data, child + 0x0A)
    row_length = _be16(data, child + 0x1A)
    rows_base = child + 8 + rows_offset
    return tuple(
        (rows_base + index * row_length + 2, rows_base + index * row_length + 4)
        for index in range(count)
    )


def _align(value: int, alignment: int = ALIGNMENT) -> int:
    return (value + alignment - 1) // alignment * alignment


def parse_cpk_members(data: bytes) -> tuple[CpkMember, ...]:
    """사전 CPK의 멤버 오프셋을 ITOC 크기와 XOR 매직으로 검증한다."""

    if not isinstance(data, bytes):
        raise TypeError("CPK 데이터는 bytes여야 합니다.")
    if len(data) < 0x20 or data[:4] != b"CPK ":
        raise DictionaryError("CPK 헤더가 없습니다.")
    _child, rows = _parse_itoc_rows(data)
    ordered = sorted(rows, key=lambda row: row[0])
    expected_ids = list(range(len(ordered)))
    identifiers = [row[0] for row in ordered]
    if identifiers != expected_ids:
        raise DictionaryError(
            "사전 ID가 0부터 연속하지 않습니다: "
            f"첫 ID={identifiers[:3]!r}, 마지막 ID={identifiers[-3:]!r}"
        )

    raw_magic = bytes(byte ^ XOR_KEY for byte in MEMBER_MAGIC)
    first_candidates = [
        index for index in range(len(data)) if data.startswith(raw_magic, index)
    ]
    if not first_candidates:
        raise DictionaryError("XOR 해제 후 ZKANKYWD 멤버를 찾지 못했습니다.")

    for first in first_candidates:
        members: list[CpkMember] = []
        offset = first
        valid = True
        for identifier, file_size, extract_size in ordered:
            if extract_size != file_size:
                valid = False
                break
            if offset + file_size > len(data):
                valid = False
                break
            if not data.startswith(raw_magic, offset):
                valid = False
                break
            members.append(
                CpkMember(
                    identifier=identifier,
                    offset=offset,
                    size=file_size,
                    extract_size=extract_size,
                )
            )
            offset = _align(offset + file_size)
        if valid:
            return tuple(members)
    raise DictionaryError(
        "ITOC 크기 행과 XOR ZKANKYWD 멤버 위치가 일치하지 않습니다."
    )


def xor_view(raw: bytes, *, key: int = XOR_KEY) -> bytes:
    """원본 멤버를 XOR 해제해 표시용 바이트열로 만든다."""

    if not 0 <= key <= 0xFF:
        raise ValueError("XOR 키는 0..255여야 합니다.")
    return bytes(byte ^ key for byte in raw)


def logical_view(raw: bytes, *, key: int = XOR_KEY) -> bytes:
    """XOR 표시열을 논리 바이트로 복원한다.

    이 포맷에서는 논리 NUL은 표시열의 ``0x5E``로, 실제 Shift-JIS
    ``0x5E``는 표시열의 ``0x00``으로 나타난다. 따라서 두 값 모두를
    보존해야 ``83 5E``(예: ``タ``)가 제어 코드로 오인되지 않는다.
    """

    return bytes(
        0 if byte == key else key if byte == 0 else byte
        for byte in xor_view(raw, key=key)
    )


def encode_logical(data: bytes, *, key: int = XOR_KEY) -> bytes:
    """논리 페이로드를 게임의 ``0x5E`` XOR 표현으로 만든다."""

    if not 0 <= key <= 0xFF:
        raise ValueError("XOR 키는 0..255여야 합니다.")
    view = bytes(0 if byte == key else key if byte == 0 else byte for byte in data)
    return bytes(byte ^ key for byte in view)


def _tag_fields(logical: bytes, member_id: int) -> tuple[DictionaryField, ...]:
    fields: list[DictionaryField] = []
    cursor = 0
    while cursor + 8 <= len(logical):
        matched = next((tag for tag in SUPPORTED_FIELDS if logical.startswith(tag, cursor)), None)
        if matched is None:
            cursor += 1
            continue
        capacity = int.from_bytes(logical[cursor + 4 : cursor + 8], "little")
        payload_start = cursor + 8
        payload_end = payload_start + capacity
        if capacity <= 0 or payload_end > len(logical):
            raise DictionaryError(
                f"ID{member_id:05d} {matched.decode()} 길이가 멤버 범위를 벗어납니다: "
                f"offset=0x{cursor:X}, capacity={capacity}"
            )
        fields.append(
            DictionaryField(
                member_id=member_id,
                tag=matched.decode("ascii"),
                offset=cursor,
                payload_offset=payload_start,
                capacity=capacity,
                payload=logical[payload_start:payload_end],
            )
        )
        cursor = payload_end
    if not fields:
        raise DictionaryError(f"ID{member_id:05d}에서 사전 태그를 찾지 못했습니다.")
    return tuple(fields)


def parse_member_fields(raw: bytes, member_id: int) -> tuple[DictionaryField, ...]:
    """한 암호화 멤버의 WORD/SRCE/DSCR/DSC2 필드를 추출한다."""

    logical = logical_view(raw)
    if not logical.startswith(MEMBER_MAGIC):
        raise DictionaryError(f"ID{member_id:05d}의 XOR 매직이 ZKANKYWD가 아닙니다.")
    return _tag_fields(logical, member_id)


def replace_field_payload(raw: bytes, field: DictionaryField, payload: bytes) -> bytes:
    """멤버의 한 고정 슬롯을 새 논리 페이로드로 교체한다."""

    if len(payload) > field.capacity:
        raise DictionaryError(
            f"ID{field.member_id:05d} {field.tag} 번역이 슬롯을 초과합니다: "
            f"{len(payload)} > {field.capacity}"
        )
    logical = bytearray(logical_view(raw))
    start = field.payload_offset
    logical[start : start + field.capacity] = payload + b"\0" * (field.capacity - len(payload))
    encoded = bytearray(raw)
    encoded[start : start + field.capacity] = encode_logical(
        bytes(logical[start : start + field.capacity])
    )
    return bytes(encoded)


def replace_member_fields(
    raw: bytes,
    fields: Sequence[DictionaryField],
    replacements: Mapping[str, bytes],
) -> bytes:
    """태그 길이를 갱신하면서 멤버를 가변 길이로 다시 조합한다."""

    logical = logical_view(raw)
    result = logical
    for field in sorted(fields, key=lambda item: item.offset, reverse=True):
        if field.tag not in replacements:
            continue
        payload = replacements[field.tag]
        start = field.payload_offset
        end = start + field.capacity
        if len(payload) > 0xFFFFFFFF:
            raise DictionaryError("번역 페이로드가 32비트 길이를 초과합니다.")
        length = len(payload).to_bytes(4, "little")
        result = result[: field.offset + 4] + length + payload + result[end:]
    return encode_logical(result)


def rebuild_cpk(
    data: bytes,
    replacements: Mapping[int, Mapping[str, bytes]],
) -> bytes:
    """CPK mode 0의 순차 멤버를 재조합하고 ITOC 크기 행을 갱신한다."""

    members = parse_cpk_members(data)
    if not members:
        raise DictionaryError("CPK 멤버가 없습니다.")
    first_offset = members[0].offset
    prefix = bytearray(data[:first_offset])
    size_cells = _itoc_row_size_offsets(data, len(members))
    body = bytearray()
    new_sizes: list[int] = []
    for index, member in enumerate(members):
        raw = data[member.offset : member.offset + member.size]
        field_replacements = replacements.get(member.identifier, {})
        if field_replacements:
            fields = parse_member_fields(raw, member.identifier)
            raw = replace_member_fields(raw, fields, field_replacements)
        if len(raw) > 0xFFFF:
            raise DictionaryError(f"ID{member.identifier:05d} 멤버가 uint16 크기를 초과합니다.")
        new_sizes.append(len(raw))
        body.extend(raw)
        if index + 1 < len(members):
            # CPK 멤버 정렬은 멤버 영역이 아닌 파일 절대 오프셋 기준이다.
            next_offset = first_offset + len(body)
            body.extend(b"\0" * (_align(next_offset) - next_offset))
    for index, size in enumerate(new_sizes):
        file_cell, extract_cell = size_cells[index]
        if not 0 <= file_cell + 2 <= len(prefix) or not 0 <= extract_cell + 2 <= len(prefix):
            raise DictionaryError("ITOC 크기 셀이 CPK 머리 영역 밖입니다.")
        struct.pack_into(">H", prefix, file_cell, size)
        struct.pack_into(">H", prefix, extract_cell, size)
    return bytes(prefix + body)


def find_field(fields: Sequence[DictionaryField], tag: str) -> DictionaryField:
    matches = [field for field in fields if field.tag == tag]
    if len(matches) != 1:
        raise DictionaryError(f"태그 {tag}가 정확히 한 번이어야 합니다: {len(matches)}")
    return matches[0]


__all__ = [
    "ALIGNMENT",
    "CpkMember",
    "DictionaryError",
    "DictionaryField",
    "MEMBER_MAGIC",
    "SUPPORTED_FIELDS",
    "XOR_KEY",
    "encode_logical",
    "find_field",
    "logical_view",
    "parse_cpk_members",
    "parse_member_fields",
    "replace_field_payload",
    "replace_member_fields",
    "rebuild_cpk",
    "sha256_bytes",
    "sha256_file",
    "xor_view",
]
