#!/usr/bin/env python3
"""저장소 JSON만으로 자기 소유 SRVC.BIN에 전투 대사를 적용한다.

게임 원본은 ``--original``으로 읽기만 하고, 결과는 저장소의 ``output/`` 또는
``work/`` 아래 별도 파일로 만든다. CPK 도구·네트워크·엑셀·게임 설치 폴더를
사용하지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = REPOSITORY_ROOT / "translations" / "dialogue" / "battle_SRVC.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "output" / "battle-dialogue" / "SRVC.BIN"
_HASH = re.compile(r"^[0-9a-f]{64}$")


class BattleDialoguePatchError(ValueError):
    """전투 대사 JSON 또는 패치 대상이 안전 조건을 만족하지 않을 때 발생한다."""


@dataclass(frozen=True, slots=True)
class MasterPatch:
    identifier: int
    offset: int
    source_bytes: int
    source_text: str
    translation: str
    original_payload_sha256: str
    applied_payload_sha256: str
    applied_payload: bytes


@dataclass(frozen=True, slots=True)
class SlotPatch:
    slot: int
    offset: int
    master_id: int
    source_bytes: int


def _sha256(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BattleDialoguePatchError(f"{label}은 객체여야 합니다.")
    return value


def _string(value: Mapping[str, object], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise BattleDialoguePatchError(f"{label}.{key}는 비어 있지 않은 문자열이어야 합니다.")
    return result


def _integer(value: Mapping[str, object], key: str, label: str, *, minimum: int = 0) -> int:
    result = value.get(key)
    if type(result) is not int or result < minimum:
        raise BattleDialoguePatchError(f"{label}.{key}는 {minimum} 이상 정수여야 합니다.")
    return result


def _hash(value: Mapping[str, object], key: str, label: str) -> str:
    result = _string(value, key, label).lower()
    if _HASH.fullmatch(result) is None:
        raise BattleDialoguePatchError(f"{label}.{key}가 SHA-256 형식이 아닙니다.")
    return result


def _inside_repository(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.is_relative_to(REPOSITORY_ROOT):
        raise BattleDialoguePatchError(f"저장소 밖의 경로는 사용할 수 없습니다: {candidate}")
    return candidate


def _common_battle_document(root: Mapping[str, object]) -> dict[str, object]:
    """공통 검수 JSON의 battle 행을 기존 고정 슬롯 계약으로 변환한다."""

    asset = _mapping(root.get("asset"), "공통 JSON.asset")
    source = _mapping(asset.get("source"), "공통 JSON.asset.source")
    localized = _mapping(asset.get("localized"), "공통 JSON.asset.localized")
    masters: list[dict[str, object]] = []
    slots: list[dict[str, object]] = []
    for index, raw in enumerate(root.get("entries", [])):
        item = _mapping(raw, f"공통 JSON.entries[{index}]")
        entry_id = _string(item, "entryId", f"공통 JSON.entries[{index}]")
        translation = _string(item, "translation", entry_id)
        translation_hash = _hash(item, "translationTextSha256", entry_id)
        if _sha256(translation.encode("utf-8")) != translation_hash:
            raise BattleDialoguePatchError(f"{entry_id}의 번역을 수정했지만 translationTextSha256가 갱신되지 않았습니다.")
        location = _mapping(item.get("location"), f"{entry_id}.location")
        source_payload = _mapping(item.get("sourcePayload"), f"{entry_id}.sourcePayload")
        applied_payload = _mapping(item.get("appliedPayload"), f"{entry_id}.appliedPayload")
        applied_hex = _string(applied_payload, "hex", f"{entry_id}.appliedPayload")
        try:
            bytes.fromhex(applied_hex)
        except ValueError as error:
            raise BattleDialoguePatchError(f"{entry_id}.appliedPayload.hex가 16진수가 아닙니다.") from error
        metadata = _mapping(item.get("metadata"), f"{entry_id}.metadata")
        master_id = _integer(location, "masterId", f"{entry_id}.location", minimum=1)
        master = {
            "id": master_id,
            "offset": _integer(location, "offset", f"{entry_id}.location"),
            "sourceText": _string(item, "sourceText", entry_id),
            "translation": translation,
            "sourceTextSha256": _hash(item, "sourceTextSha256", entry_id),
            "sourceByteLength": _integer(source_payload, "bytes", f"{entry_id}.sourcePayload", minimum=1),
            "translationByteLength": _integer(metadata, "translationByteLength", f"{entry_id}.metadata", minimum=0),
            "spareBytes": _integer(metadata, "spareBytes", f"{entry_id}.metadata", minimum=0),
            "repeatCount": _integer(location, "repeatCount", f"{entry_id}.location", minimum=0),
            "status": str(item.get("translationStatus", "draft")),
            "reviewNote": str(item.get("notes", "")),
            "originalPayloadSha256": _hash(source_payload, "sha256", f"{entry_id}.sourcePayload"),
            "appliedPayloadSha256": _hash(applied_payload, "sha256", f"{entry_id}.appliedPayload"),
            "appliedPayloadHex": applied_hex,
        }
        masters.append(master)
        raw_slots = location.get("slots")
        if not isinstance(raw_slots, list) or not raw_slots:
            raise BattleDialoguePatchError(f"{entry_id}.location.slots가 비어 있습니다.")
        for raw_slot in raw_slots:
            slot = _mapping(raw_slot, f"{entry_id}.location.slots")
            slots.append(
                {
                    "slot": _integer(slot, "slot", entry_id, minimum=1),
                    "offset": _integer(slot, "offset", entry_id),
                    "masterId": master_id,
                    "sourceByteLength": _integer(slot, "bytes", entry_id, minimum=1),
                    "applied": slot.get("applied") is True,
                }
            )
    return {
        "format": "siok.srvc-battle-dialogue",
        "formatVersion": 2,
        "policy": root.get("policy", {}),
        "asset": {
            "fileName": str(asset.get("fileName", "SRVC.BIN")),
            "bytes": _integer(source, "bytes", "공통 JSON.asset.source", minimum=1),
            "originalSha256": _hash(source, "sha256", "공통 JSON.asset.source"),
            "appliedSha256": _hash(localized, "sha256", "공통 JSON.asset.localized"),
        },
        "counts": {
            "masterRows": len(masters),
            "slotRows": len(slots),
        },
        "master": masters,
        "slots": sorted(slots, key=lambda item: int(item["slot"])),
    }


def _load_document(path: Path) -> tuple[dict[str, object], tuple[MasterPatch, ...], tuple[SlotPatch, ...]]:
    document_path = _inside_repository(path)
    if not document_path.is_file():
        raise BattleDialoguePatchError(f"패치 JSON을 찾을 수 없습니다: {document_path}")
    try:
        document = json.loads(document_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BattleDialoguePatchError(f"패치 JSON을 읽을 수 없습니다: {document_path}") from error
    root = _mapping(document, "JSON")
    if root.get("format") == "siok.dialogue-review" and root.get("dialogueType") == "battle":
        root = _common_battle_document(root)
    if root.get("format") != "siok.srvc-battle-dialogue" or root.get("formatVersion") != 2:
        raise BattleDialoguePatchError("지원하지 않는 SRVC 전투 대사 JSON 형식입니다.")
    policy = _mapping(root.get("policy"), "JSON.policy")
    if policy.get("requiresUserOwnedGame") is not True:
        raise BattleDialoguePatchError("자기 소유 게임 입력을 요구하는 정책이 없습니다.")
    asset = _mapping(root.get("asset"), "JSON.asset")
    asset_name = _string(asset, "fileName", "JSON.asset")
    if asset_name.upper() != "SRVC.BIN":
        raise BattleDialoguePatchError(f"SRVC.BIN만 지원합니다: {asset_name}")
    asset_size = _integer(asset, "bytes", "JSON.asset", minimum=1)
    original_sha256 = _hash(asset, "originalSha256", "JSON.asset")
    applied_sha256 = _hash(asset, "appliedSha256", "JSON.asset")

    raw_masters = root.get("master")
    raw_slots = root.get("slots")
    if not isinstance(raw_masters, list) or not raw_masters:
        raise BattleDialoguePatchError("JSON.master가 비어 있거나 배열이 아닙니다.")
    if not isinstance(raw_slots, list) or not raw_slots:
        raise BattleDialoguePatchError("JSON.slots가 비어 있거나 배열이 아닙니다.")

    masters: list[MasterPatch] = []
    for expected_id, raw in enumerate(raw_masters, start=1):
        item = _mapping(raw, f"JSON.master[{expected_id - 1}]")
        label = f"JSON.master[{expected_id - 1}]"
        identifier = _integer(item, "id", label, minimum=1)
        if identifier != expected_id:
            raise BattleDialoguePatchError(f"{label}.id가 연속되지 않습니다.")
        source_text = _string(item, "sourceText", label)
        translation = _string(item, "translation", label)
        expected_source_hash = _hash(item, "sourceTextSha256", label)
        if _sha256(source_text.encode("utf-8")) != expected_source_hash:
            raise BattleDialoguePatchError(f"{label}.sourceTextSha256가 원문과 다릅니다.")
        source_bytes = _integer(item, "sourceByteLength", label, minimum=1)
        applied_hex = _string(item, "appliedPayloadHex", label)
        try:
            applied_payload = bytes.fromhex(applied_hex)
        except ValueError as error:
            raise BattleDialoguePatchError(f"{label}.appliedPayloadHex가 16진수가 아닙니다.") from error
        if len(applied_payload) != source_bytes:
            raise BattleDialoguePatchError(f"{label} 페이로드 길이가 슬롯 크기와 다릅니다.")
        applied_payload_sha256 = _hash(item, "appliedPayloadSha256", label)
        if _sha256(applied_payload) != applied_payload_sha256:
            raise BattleDialoguePatchError(f"{label}.appliedPayloadSha256가 페이로드와 다릅니다.")
        masters.append(
            MasterPatch(
                identifier=identifier,
                offset=_integer(item, "offset", label),
                source_bytes=source_bytes,
                source_text=source_text,
                translation=translation,
                original_payload_sha256=_hash(item, "originalPayloadSha256", label),
                applied_payload_sha256=applied_payload_sha256,
                applied_payload=applied_payload,
            )
        )

    slots: list[SlotPatch] = []
    for expected_slot, raw in enumerate(raw_slots, start=1):
        item = _mapping(raw, f"JSON.slots[{expected_slot - 1}]")
        label = f"JSON.slots[{expected_slot - 1}]"
        slot = _integer(item, "slot", label, minimum=1)
        if slot != expected_slot:
            raise BattleDialoguePatchError(f"{label}.slot가 연속되지 않습니다.")
        master_id = _integer(item, "masterId", label, minimum=1)
        if master_id > len(masters):
            raise BattleDialoguePatchError(f"{label}.masterId가 범위를 벗어났습니다.")
        source_bytes = _integer(item, "sourceByteLength", label, minimum=1)
        if source_bytes != masters[master_id - 1].source_bytes:
            raise BattleDialoguePatchError(f"{label} 슬롯 크기가 마스터와 다릅니다.")
        if item.get("applied") is not True:
            raise BattleDialoguePatchError(f"{label}.applied가 true가 아닙니다.")
        slots.append(
            SlotPatch(
                slot=slot,
                offset=_integer(item, "offset", label),
                master_id=master_id,
                source_bytes=source_bytes,
            )
        )

    declared_counts = _mapping(root.get("counts"), "JSON.counts")
    if _integer(declared_counts, "masterRows", "JSON.counts") != len(masters):
        raise BattleDialoguePatchError("JSON.counts.masterRows가 실제 행 수와 다릅니다.")
    if _integer(declared_counts, "slotRows", "JSON.counts") != len(slots):
        raise BattleDialoguePatchError("JSON.counts.slotRows가 실제 행 수와 다릅니다.")
    return (
        {
            "documentPath": document_path,
            "assetSize": asset_size,
            "originalSha256": original_sha256,
            "appliedSha256": applied_sha256,
        },
        tuple(masters),
        tuple(slots),
    )


def _validate_slots(slots: Sequence[SlotPatch], masters: Sequence[MasterPatch], size: int) -> None:
    ordered = sorted(slots, key=lambda item: (item.offset, item.slot))
    previous_end = 0
    for slot in ordered:
        end = slot.offset + slot.source_bytes
        if end > size:
            raise BattleDialoguePatchError(f"슬롯 {slot.slot}이 원본 파일 범위를 벗어났습니다.")
        if slot.offset < previous_end:
            raise BattleDialoguePatchError(f"슬롯 {slot.slot}이 다른 슬롯과 겹칩니다.")
        master = masters[slot.master_id - 1]
        if len(master.applied_payload) != slot.source_bytes:
            raise BattleDialoguePatchError(f"슬롯 {slot.slot}의 페이로드 크기가 다릅니다.")
        previous_end = end


def _safe_original(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=True)
    if candidate.is_symlink():
        raise BattleDialoguePatchError(f"링크인 게임 원본은 사용할 수 없습니다: {candidate}")
    return candidate


def _safe_output(path: Path, original: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    allowed_roots = (REPOSITORY_ROOT / "output", REPOSITORY_ROOT / "work")
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise BattleDialoguePatchError("패치 출력은 저장소의 output/ 또는 work/ 안이어야 합니다.")
    if candidate == original:
        raise BattleDialoguePatchError("게임 원본에 직접 덮어쓸 수 없습니다.")
    if candidate.name.startswith("."):
        raise BattleDialoguePatchError("숨김 파일을 출력할 수 없습니다.")
    return candidate


def _write_atomic(path: Path, data: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise BattleDialoguePatchError(f"출력 파일이 이미 있습니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_patch(
    *,
    document_path: Path,
    original_path: Path,
    output_path: Path,
    report_path: Path,
    overwrite: bool,
) -> dict[str, object]:
    metadata, masters, slots = _load_document(document_path)
    original = _safe_original(original_path).read_bytes()
    if len(original) != metadata["assetSize"]:
        raise BattleDialoguePatchError("게임 원본 크기가 JSON의 기준과 다릅니다.")
    original_sha256 = _sha256(original)
    if original_sha256 != metadata["originalSha256"]:
        raise BattleDialoguePatchError(
            "게임 원본 SHA-256이 JSON의 기준과 다릅니다. 자기 소유 원본을 확인하세요."
        )
    _validate_slots(slots, masters, len(original))

    result = bytearray(original)
    for slot in slots:
        master = masters[slot.master_id - 1]
        current = original[slot.offset : slot.offset + slot.source_bytes]
        if _sha256(current) != master.original_payload_sha256:
            raise BattleDialoguePatchError(
                f"슬롯 {slot.slot}의 원본 페이로드가 다릅니다. 다른 버전의 게임 파일일 수 있습니다."
            )
        end = slot.offset + slot.source_bytes
        result[slot.offset:end] = master.applied_payload

    output = bytes(result)
    output_sha256 = _sha256(output)
    if output_sha256 != metadata["appliedSha256"]:
        raise BattleDialoguePatchError(
            "패치 결과 SHA-256이 JSON의 기준과 다릅니다. JSON 또는 원본을 바꾸지 않았는지 확인하세요."
        )
    safe_output = _safe_output(output_path, original_path.resolve())
    safe_report = _safe_output(report_path, original_path.resolve())
    if safe_report.suffix.lower() != ".json":
        raise BattleDialoguePatchError("검증 보고서는 JSON 파일이어야 합니다.")
    _write_atomic(safe_output, output, overwrite=overwrite)
    report = {
        "format": "siok.srvc-battle-patch-report",
        "formatVersion": 1,
        "requiresUserOwnedGame": True,
        "json": {
            "fileName": Path(metadata["documentPath"]).name,
            "sha256": _sha256_file(Path(metadata["documentPath"])),
            "masterRows": len(masters),
            "slotRows": len(slots),
        },
        "input": {
            "fileName": original_path.name,
            "bytes": len(original),
            "sha256": original_sha256,
        },
        "output": {
            "fileName": safe_output.name,
            "bytes": len(output),
            "sha256": output_sha256,
        },
        "verification": {
            "allSlotsPatched": True,
            "originalPayloadHashesChecked": len(slots),
            "targetSha256Matched": True,
            "outputIsSeparateFromOriginal": True,
        },
    }
    _write_atomic(
        safe_report,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        overwrite=overwrite,
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True, help="자기 소유 원본 SRVC.BIN")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="저장소의 번역 JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output/ 아래 새 BIN")
    parser.add_argument("--report", type=Path, help="검증 보고서 경로(JSON)")
    parser.add_argument("--overwrite", action="store_true", help="기존 output 파일을 교체")
    args = parser.parse_args(argv)
    output = args.output
    report = args.report or output.with_name(output.name + ".report.json")
    try:
        result = apply_patch(
            document_path=args.json,
            original_path=args.original,
            output_path=output,
            report_path=report,
            overwrite=args.overwrite,
        )
    except (BattleDialoguePatchError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
