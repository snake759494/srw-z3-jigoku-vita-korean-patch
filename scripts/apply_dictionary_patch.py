#!/usr/bin/env python3
"""사전 대사 JSON을 자기 소유 ``MtZkn_KW.cpk``에 적용하고 CPK를 리팩한다.

원본 CPK는 절대로 덮어 쓰지 않는다. 입력의 SHA-256과 모든 원문 페이로드를
확인한 뒤, 결과를 저장소의 ``output/`` 또는 ``work/`` 아래 새 파일로 만든다.
긴 한국어 설명은 멤버의 태그 길이와 ITOC 크기 행을 함께 갱신한다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from siok_patch.dictionary import (
    DictionaryError,
    logical_view,
    parse_cpk_members,
    parse_member_fields,
    rebuild_cpk,
    sha256_bytes,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = REPOSITORY_ROOT / "translations" / "dialogue" / "dictionary_MtZkn_KW.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "output" / "dictionary" / "MtZkn_KW.cpk"


class DictionaryPatchError(ValueError):
    """사전 패치 입력이 검증을 통과하지 못할 때 발생한다."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DictionaryPatchError(f"{label}은 객체여야 합니다.")
    return value


def _string(value: Mapping[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DictionaryPatchError(f"{label}.{key}는 비어 있지 않은 문자열이어야 합니다.")
    return item


def _integer(value: Mapping[str, object], key: str, label: str, minimum: int = 0) -> int:
    item = value.get(key)
    if type(item) is not int or item < minimum:
        raise DictionaryPatchError(f"{label}.{key}는 {minimum} 이상 정수여야 합니다.")
    return item


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _hash(value: Mapping[str, object], key: str, label: str) -> str:
    result = _string(value, key, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise DictionaryPatchError(f"{label}.{key}가 SHA-256 형식이 아닙니다.")
    return result


def _hex(value: Mapping[str, object], key: str, label: str) -> bytes:
    text = _string(value, key, label)
    try:
        return bytes.fromhex(text)
    except ValueError as error:
        raise DictionaryPatchError(f"{label}.{key}가 16진수가 아닙니다.") from error


def _safe_output(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_relative_to(REPOSITORY_ROOT / "output") and not candidate.is_relative_to(REPOSITORY_ROOT / "work"):
        raise DictionaryPatchError("패치 출력은 저장소의 output/ 또는 work/ 안이어야 합니다.")
    return candidate


def _safe_original(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=True)
    if candidate.is_symlink():
        raise DictionaryPatchError(f"링크인 게임 원본은 사용할 수 없습니다: {candidate}")
    return candidate


def _common_dictionary_document(root: Mapping[str, object]) -> dict[str, object]:
    """공통 검수 JSON의 dictionary 행을 기존 CPK 계약으로 변환한다."""

    asset = _mapping(root.get("asset"), "공통 JSON.asset")
    source = _mapping(asset.get("source"), "공통 JSON.asset.source")
    localized = _mapping(asset.get("localized"), "공통 JSON.asset.localized")
    entries: list[dict[str, object]] = []
    for index, raw in enumerate(root.get("entries", [])):
        item = _mapping(raw, f"공통 JSON.entries[{index}]")
        label = f"공통 JSON.entries[{index}]"
        entry_id = _string(item, "entryId", label)
        translation = _string(item, "translation", entry_id)
        translation_hash = _string(item, "translationTextSha256", entry_id)
        if _hash_text(translation) != translation_hash:
            raise DictionaryPatchError(f"{entry_id}의 번역을 수정했지만 translationTextSha256가 갱신되지 않았습니다.")
        location = _mapping(item.get("location"), f"{entry_id}.location")
        source_payload = _mapping(item.get("sourcePayload"), f"{entry_id}.sourcePayload")
        applied_payload = _mapping(item.get("appliedPayload"), f"{entry_id}.appliedPayload")
        source_hex = _string(source_payload, "hex", f"{entry_id}.sourcePayload")
        applied_hex = _string(applied_payload, "hex", f"{entry_id}.appliedPayload")
        entries.append(
            {
                "entryId": entry_id,
                "memberId": _integer(location, "memberId", f"{entry_id}.location"),
                "field": _string(location, "field", f"{entry_id}.location"),
                "memberPayloadOffset": _integer(location, "memberPayloadOffset", f"{entry_id}.location"),
                "payloadOffset": _integer(location, "payloadOffset", f"{entry_id}.location"),
                "capacityBytes": _integer(location, "capacityBytes", f"{entry_id}.location"),
                "sourceText": _string(item, "sourceText", entry_id),
                "sourceTextSha256": _string(item, "sourceTextSha256", entry_id),
                "sourcePayloadHex": source_hex,
                "sourcePayloadSha256": _hash(source_payload, "sha256", f"{entry_id}.sourcePayload"),
                "translation": translation,
                "translationStatus": str(item.get("translationStatus", "draft")),
                "translator": str(item.get("translator", "")),
                "reviewer": str(item.get("reviewer", "")),
                "referenceConsulted": bool(item.get("referenceConsulted", False)),
                "notes": str(item.get("notes", "")),
                "translationTextSha256": translation_hash,
                "translationByteLength": _integer(applied_payload, "bytes", f"{entry_id}.appliedPayload"),
                "appliedPayloadHex": applied_hex,
                "appliedPayloadSha256": _hash(applied_payload, "sha256", f"{entry_id}.appliedPayload"),
            }
        )
    return {
        "format": "siok.dictionary-dialogue",
        "formatVersion": 1,
        "policy": root.get("policy", {}),
        "asset": {
            "fileName": str(asset.get("fileName", "MtZkn_KW.cpk")),
            "bytes": _integer(source, "bytes", "공통 JSON.asset.source", minimum=1),
            "sha256": _hash(source, "sha256", "공통 JSON.asset.source"),
            "memberCount": int(root.get("counts", {}).get("members", 0)) if isinstance(root.get("counts"), dict) else 0,
        },
        "localizedAsset": {
            "bytes": _integer(localized, "bytes", "공통 JSON.asset.localized", minimum=1),
            "sha256": _hash(localized, "sha256", "공통 JSON.asset.localized"),
        },
        "counts": {
            "members": int(root.get("counts", {}).get("members", 0)) if isinstance(root.get("counts"), dict) else 0,
            "fields": len(entries),
            "translatedFields": len(entries),
            "uniqueSourceTexts": len({str(item["sourceText"]) for item in entries}),
        },
        "entries": entries,
    }


@dataclass(frozen=True, slots=True)
class EntryPatch:
    entry_id: str
    member_id: int
    field: str
    source_payload: bytes
    applied_payload: bytes


def _load_document(path: Path) -> tuple[dict[str, object], tuple[EntryPatch, ...]]:
    document_path = path.expanduser().resolve(strict=True)
    if not document_path.is_relative_to(REPOSITORY_ROOT / "translations"):
        raise DictionaryPatchError("패치 JSON은 저장소 translations/ 안이어야 합니다.")
    try:
        root = json.loads(document_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DictionaryPatchError(f"패치 JSON을 읽을 수 없습니다: {document_path}") from error
    if root.get("format") == "siok.dialogue-review" and root.get("dialogueType") == "dictionary":
        root = _common_dictionary_document(root)
    if root.get("format") != "siok.dictionary-dialogue" or root.get("formatVersion") != 1:
        raise DictionaryPatchError("지원하지 않는 사전 대사 JSON 형식입니다.")
    policy = _mapping(root.get("policy"), "JSON.policy")
    if policy.get("requiresUserOwnedGame") is not True or policy.get("gameBinaryIncluded") is not False:
        raise DictionaryPatchError("자기 소유 게임·게임 바이너리 제외 정책이 없습니다.")
    asset = _mapping(root.get("asset"), "JSON.asset")
    if _string(asset, "fileName", "JSON.asset").upper() != "MTZKN_KW.CPK":
        raise DictionaryPatchError("MtZkn_KW.cpk만 지원합니다.")
    asset_size = _integer(asset, "bytes", "JSON.asset", 1)
    original_sha256 = _string(asset, "sha256", "JSON.asset").lower()
    entries = root.get("entries")
    if not isinstance(entries, list) or len(entries) != 564:
        raise DictionaryPatchError("JSON.entries는 564개여야 합니다.")
    result: list[EntryPatch] = []
    for index, raw in enumerate(entries):
        item = _mapping(raw, f"JSON.entries[{index}]")
        label = f"JSON.entries[{index}]"
        source_text = _string(item, "sourceText", label)
        if _string(item, "sourceTextSha256", label) != _hash_text(source_text):
            raise DictionaryPatchError(f"{label}.sourceTextSha256가 원문과 다릅니다.")
        translation = _string(item, "translation", label)
        if not translation:
            raise DictionaryPatchError(f"{label}.translation이 비어 있습니다.")
        entry_id = _string(item, "entryId", label)
        member_id = _integer(item, "memberId", label)
        field = _string(item, "field", label)
        source_payload = _hex(item, "sourcePayloadHex", label)
        applied_payload = _hex(item, "appliedPayloadHex", label)
        if sha256_bytes(source_payload) != _string(item, "sourcePayloadSha256", label):
            raise DictionaryPatchError(f"{label}.sourcePayloadSha256가 원문 페이로드와 다릅니다.")
        if sha256_bytes(applied_payload) != _string(item, "appliedPayloadSha256", label):
            raise DictionaryPatchError(f"{label}.appliedPayloadSha256가 적용 페이로드와 다릅니다.")
        if _integer(item, "translationByteLength", label) != len(applied_payload):
            raise DictionaryPatchError(f"{label}.translationByteLength가 페이로드와 다릅니다.")
        result.append(EntryPatch(entry_id, member_id, field, source_payload, applied_payload))
    return ({"assetSize": asset_size, "originalSha256": original_sha256, "documentPath": document_path}, tuple(result))


def apply_patch(document_path: Path, original_path: Path, output_path: Path) -> dict[str, object]:
    metadata, entries = _load_document(document_path)
    original_path = _safe_original(original_path)
    output_path = _safe_output(output_path)
    original = original_path.read_bytes()
    if len(original) != metadata["assetSize"]:
        raise DictionaryPatchError("원본 CPK 크기가 JSON과 다릅니다.")
    if sha256_bytes(original) != metadata["originalSha256"]:
        raise DictionaryPatchError("원본 CPK SHA-256이 JSON과 다릅니다.")
    try:
        members = parse_cpk_members(original)
    except DictionaryError as error:
        raise DictionaryPatchError(str(error)) from error
    replacements: dict[int, dict[str, bytes]] = {}
    seen: set[tuple[int, str]] = set()
    for entry in entries:
        key = (entry.member_id, entry.field)
        if key in seen:
            raise DictionaryPatchError(f"중복된 사전 필드입니다: {entry.entry_id}")
        seen.add(key)
        if entry.member_id >= len(members):
            raise DictionaryPatchError(f"멤버 ID가 범위를 벗어납니다: {entry.entry_id}")
        member = members[entry.member_id]
        raw = original[member.offset : member.offset + member.size]
        fields = parse_member_fields(raw, entry.member_id)
        field = next((item for item in fields if item.tag == entry.field), None)
        if field is None:
            raise DictionaryPatchError(f"원본 CPK에서 태그를 찾지 못했습니다: {entry.entry_id}")
        actual_source = field.payload
        if actual_source != entry.source_payload:
            raise DictionaryPatchError(f"원문 페이로드가 JSON과 다릅니다: {entry.entry_id}")
        replacements.setdefault(entry.member_id, {})[entry.field] = logical_view(entry.applied_payload)
    try:
        applied = rebuild_cpk(original, replacements)
    except DictionaryError as error:
        raise DictionaryPatchError(str(error)) from error
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_bytes(applied)
    temporary.replace(output_path)

    # 리팩 결과에서 태그별 페이로드가 실제로 JSON과 일치하는지 다시 읽는다.
    rebuilt_members = parse_cpk_members(applied)
    for entry in entries:
        member = rebuilt_members[entry.member_id]
        raw = applied[member.offset : member.offset + member.size]
        field = next(item for item in parse_member_fields(raw, entry.member_id) if item.tag == entry.field)
        if raw[field.payload_offset : field.payload_offset + field.capacity] != entry.applied_payload:
            raise DictionaryPatchError(f"리팩 후 페이로드 검증에 실패했습니다: {entry.entry_id}")
    return {
        "output": str(output_path),
        "originalSha256": metadata["originalSha256"],
        "appliedSha256": sha256_bytes(applied),
        "originalBytes": len(original),
        "appliedBytes": len(applied),
        "members": len(members),
        "fields": len(entries),
        "repackedMembers": len(replacements),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, help="선택적 JSON 검증 보고서")
    args = parser.parse_args(argv)
    try:
        report = apply_patch(args.json, args.original, args.output)
        if args.report:
            report_path = _safe_output(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (DictionaryPatchError, DictionaryError, OSError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
