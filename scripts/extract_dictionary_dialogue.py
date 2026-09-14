#!/usr/bin/env python3
"""자기 소유 게임의 ``MtZkn_KW.cpk``에서 사전 대사를 추출한다.

CPK는 읽기 전용으로 열고, ITOC/@UTF의 ID·크기 표와 각 멤버의
``ZKANKYWD`` 매직을 함께 검증한다. 출력 JSON에는 일본어 원문, 정확한
고정 슬롯 정보, 제어 토큰과 원본 해시만 기록하며 게임 파일은 저장하지
않는다. 번역·페이로드 생성은 ``translate_dictionary.py``가 담당한다.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from siok_patch.dictionary import (
    DictionaryError,
    find_field,
    parse_cpk_members,
    parse_member_fields,
    sha256_bytes,
    sha256_file,
)
from siok_patch.text_normalization import GAME_HALF_WIDTH_SPACE_BYTES


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENCODING = REPOSITORY_ROOT / "config" / "encoding" / "dictionary.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "work" / "dictionary-source.json"
XOR_MARKER = 0x5E


class ExtractionError(ValueError):
    """사전 추출 입력이 예상한 형식이 아닐 때 발생한다."""


def _load_encoding(path: Path) -> tuple[dict[bytes, str], dict[str, str]]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExtractionError(f"인코딩 JSON을 읽을 수 없습니다: {path}") from error
    if root.get("format") != "siok.dictionary-encoding":
        raise ExtractionError("지원하지 않는 사전 인코딩 JSON입니다.")
    raw_table = root.get("table")
    raw_replacement = root.get("replacement")
    if not isinstance(raw_table, dict) or not isinstance(raw_replacement, dict):
        raise ExtractionError("인코딩 JSON의 table/replacement가 객체가 아닙니다.")
    table: dict[bytes, str] = {}
    for key, text in raw_table.items():
        try:
            sequence = bytes.fromhex(str(key))
        except ValueError as error:
            raise ExtractionError(f"인코딩 JSON의 바이트 키가 잘못되었습니다: {key!r}") from error
        if not sequence or not isinstance(text, str) or not text:
            raise ExtractionError("인코딩 JSON에 빈 매핑이 있습니다.")
        table[sequence] = text
    replacement = {
        str(key): str(value)
        for key, value in raw_replacement.items()
        if str(key) and str(value)
    }
    return table, replacement


def _decoder(table: Mapping[bytes, str]):
    by_first: dict[int, list[bytes]] = {}
    for sequence in table:
        by_first.setdefault(sequence[0], []).append(sequence)
    for values in by_first.values():
        values.sort(key=len, reverse=True)
    return by_first


def decode_payload(payload: bytes, table: Mapping[bytes, str]) -> tuple[str, list[str]]:
    """TBL 문자와 사전 제어 바이트를 사람이 읽는 토큰으로 바꾼다."""

    by_first = _decoder(table)
    output: list[str] = []
    controls: list[str] = []
    position = 0
    while position < len(payload):
        if payload.startswith(GAME_HALF_WIDTH_SPACE_BYTES, position):
            output.append(" ")
            position += len(GAME_HALF_WIDTH_SPACE_BYTES)
            continue
        byte = payload[position]
        if byte == 0:
            token = "<NUL>"
            output.append(token)
            controls.append(token)
            position += 1
            continue
        if byte == 0x0A:
            token = "<BR>"
            output.append(token)
            controls.append(token)
            position += 1
            continue
        matched = next(
            (sequence for sequence in by_first.get(byte, ()) if payload.startswith(sequence, position)),
            None,
        )
        if matched is not None:
            output.append(table[matched])
            position += len(matched)
            continue
        token = f"<B{byte:02X}>"
        output.append(token)
        controls.append(token)
        position += 1
    return "".join(output), controls


def _control_signature(controls: Sequence[str]) -> str:
    return " ".join(controls) if controls else ""


def _entry(member_id: int, field, table: Mapping[bytes, str]) -> dict[str, object]:
    source_text, controls = decode_payload(field.payload, table)
    return {
        "entryId": f"MtZkn_KW/ID{member_id:05d}/{field.tag}",
        "memberId": member_id,
        "field": field.tag,
        "memberPayloadOffset": field.offset,
        "payloadOffset": field.payload_offset,
        "capacityBytes": field.capacity,
        "sourceText": source_text,
        "sourceTextSha256": sha256(source_text.encode("utf-8")).hexdigest(),
        "sourcePayloadHex": field.payload.hex(),
        "sourcePayloadSha256": sha256_bytes(field.payload),
        "controlTokens": controls,
        "controlSignature": _control_signature(controls),
        "translation": "",
        "translationStatus": "untranslated",
        "translator": "",
        "reviewer": "",
        "notes": "게임 CPK에서 0x5E XOR 해제 후 직접 추출한 일본어 원문.",
    }


def extract(cpk_path: Path, encoding_path: Path, expected_sha256: str | None = None) -> dict[str, object]:
    cpk_path = cpk_path.expanduser().resolve(strict=True)
    encoding_path = encoding_path.expanduser().resolve(strict=True)
    if expected_sha256 and sha256_file(cpk_path) != expected_sha256.lower():
        raise ExtractionError("CPK SHA-256이 지정한 값과 다릅니다.")
    try:
        data = cpk_path.read_bytes()
    except OSError as error:
        raise ExtractionError(f"CPK를 읽을 수 없습니다: {cpk_path}") from error
    table, _replacement = _load_encoding(encoding_path)
    try:
        members = parse_cpk_members(data)
    except DictionaryError as error:
        raise ExtractionError(str(error)) from error
    entries: list[dict[str, object]] = []
    member_rows: list[dict[str, object]] = []
    for member in members:
        raw = data[member.offset : member.offset + member.size]
        try:
            fields = parse_member_fields(raw, member.identifier)
        except DictionaryError as error:
            raise ExtractionError(str(error)) from error
        if member.identifier != len(member_rows):
            raise ExtractionError("CPK 멤버 ID가 연속하지 않습니다.")
        member_rows.append(
            {
                "id": member.identifier,
                "offset": member.offset,
                "size": member.size,
                "extractSize": member.extract_size,
                "sha256": sha256_bytes(raw),
                "fields": [field.tag for field in fields],
            }
        )
        entries.extend(_entry(member.identifier, field, table) for field in fields)
    return {
        "format": "siok.dictionary-dialogue-source",
        "formatVersion": 1,
        "gameId": "PCSG00264",
        "languages": {"source": "ja", "target": "ko"},
        "payloadFormat": "mtzkn-kw-fixed-xor-v1",
        "asset": {
            "fileName": "MtZkn_KW.cpk",
            "bytes": len(data),
            "sha256": sha256_bytes(data),
            "memberCount": len(members),
        },
        "encoding": {
            "fileName": encoding_path.name,
            "sha256": sha256_file(encoding_path),
        },
        "policy": {
            "originalJapaneseTextIncluded": True,
            "gameBinaryIncluded": False,
            "requiresUserOwnedGame": True,
            "securityNotice": "본인이 합법적으로 보유·덤프한 게임 파일에만 사용한다.",
            "copyrightNotice": "게임 원문과 게임 데이터의 권리는 각 권리자에게 있다.",
        },
        "counts": {
            "members": len(members),
            "fields": len(entries),
            "translatedFields": 0,
        },
        "members": member_rows,
        "entries": entries,
    }


def _safe_output(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    allowed = (REPOSITORY_ROOT / "work", REPOSITORY_ROOT / "translations")
    if not any(candidate.is_relative_to(root) for root in allowed):
        raise ExtractionError("출력은 저장소의 work/ 또는 translations/ 안이어야 합니다.")
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpk", type=Path, required=True, help="자기 소유 원본 MtZkn_KW.cpk")
    parser.add_argument("--encoding", type=Path, default=DEFAULT_ENCODING)
    parser.add_argument("--expected-sha256", help="선택적 원본 SHA-256")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = extract(args.cpk, args.encoding, args.expected_sha256)
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ExtractionError, OSError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(output), **document["counts"], "assetSha256": document["asset"]["sha256"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
