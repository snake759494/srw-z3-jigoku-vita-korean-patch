#!/usr/bin/env python3
"""시나리오·전투·사전 대사를 공통 검수용 JSON으로 묶는다.

이 스크립트는 원본 게임 바이너리를 읽지 않는다. 시나리오 원문은 로컬
``work/normalized/translations.tsv``에서 읽고, 전투·사전은 저장소에 이미
있는 공개 JSON에서 읽는다. 결과는 ``translations/dialogue/`` 아래의
자산별 ``siok.dialogue-review`` JSON이며, 모든 종류가 같은 행 구조를
사용한다.
"""

from __future__ import annotations

import argparse
import ast
import csv
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NORMALIZED = REPOSITORY_ROOT / "work" / "normalized" / "translations.tsv"
DEFAULT_RETRANSLATION = REPOSITORY_ROOT / "translations" / "retranslation"
DEFAULT_BATTLE = REPOSITORY_ROOT / "translations" / "battle-dialogue" / "srvc" / "SRVC_BATTLE.json"
DEFAULT_DICTIONARY = REPOSITORY_ROOT / "translations" / "dictionary" / "MtZkn_KW.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "translations" / "dialogue"
FORMAT = "siok.dialogue-review"
FORMAT_VERSION = 1
TOKEN = re.compile(r"(?:\\n|\\r|<BR>|<NUL>|<B[0-9A-Fa-f]{2}>|\$[nlcF]|⑲|⑳|㊥|㊦|㊧|㊨)")


class DialogueReviewError(ValueError):
    """검수 JSON을 만들 수 없는 입력을 만났을 때 발생한다."""


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DialogueReviewError(f"JSON을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict):
        raise DialogueReviewError(f"JSON 최상위가 객체가 아닙니다: {path}")
    return value


def _integer(value: str, label: str, *, allow_empty: bool = True) -> int | None:
    if not value.strip():
        if allow_empty:
            return None
        raise DialogueReviewError(f"{label}이 비어 있습니다.")
    try:
        return int(value, 0)
    except ValueError as error:
        raise DialogueReviewError(f"{label}이 정수가 아닙니다: {value!r}") from error


def _tokens_from_signature(value: str) -> list[str]:
    if not value or value == "[]":
        return []
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, (list, tuple)) and all(isinstance(item, str) for item in parsed):
        return list(parsed)
    return TOKEN.findall(value)


def _tokens_from_text(value: str) -> list[str]:
    return TOKEN.findall(value)


def _controls(source_tokens: Iterable[str], translation: str) -> dict[str, Any]:
    source = list(source_tokens)
    target = _tokens_from_text(translation)
    return {
        "sourceTokens": source,
        "translationTokens": target,
        "sourceSignature": " ".join(source),
        "translationSignature": " ".join(target),
    }


def _payload(*, data: bytes | None = None, byte_length: int | None = None, digest: str | None = None) -> dict[str, Any]:
    return {
        "bytes": len(data) if data is not None else byte_length,
        "sha256": sha256_bytes(data) if data is not None else digest,
        "hex": data.hex() if data is not None else None,
    }


def _entry(
    *,
    entry_id: str,
    source_text: str,
    translation: str,
    status: str,
    translator: str,
    reviewer: str,
    reference_consulted: bool,
    notes: str,
    controls: dict[str, Any],
    location: dict[str, Any],
    source_payload: dict[str, Any] | None = None,
    applied_payload: dict[str, Any] | None = None,
    references: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # 공통 JSON을 다시 만들 때도 번역 문자열의 공백 규칙을 보장한다.
    translation = translation.replace("\u3000", " ")
    return {
        "entryId": entry_id,
        "sourceText": source_text,
        "translation": translation,
        "sourceTextSha256": sha256_text(source_text),
        "translationTextSha256": sha256_text(translation),
        "translationStatus": status,
        "translator": translator,
        "reviewer": reviewer,
        "referenceConsulted": reference_consulted,
        "notes": notes,
        "controls": controls,
        "location": location,
        "sourcePayload": source_payload,
        "appliedPayload": applied_payload,
        "references": references or {},
        "metadata": metadata or {},
    }


def _base_document(
    *,
    dialogue_type: str,
    asset: dict[str, Any],
    entries: list[dict[str, Any]],
    encoding: dict[str, Any] | None = None,
    source_snapshot: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "$schema": "../../config/dialogue-review.schema.json",
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "gameId": "PCSG00264",
        "dialogueType": dialogue_type,
        "languages": {"source": "ja", "target": "ko"},
        "asset": asset,
        "sourceSnapshot": source_snapshot,
        "encoding": encoding or {},
        "policy": policy
        or {
            "requiresUserOwnedGame": True,
            "gameBinaryIncluded": False,
            "originalJapaneseTextIncluded": True,
        },
        "counts": {
            "entries": len(entries),
            "uniqueSourceTexts": len({str(item["sourceText"]) for item in entries}),
            "translatedEntries": sum(bool(str(item["translation"]).strip()) for item in entries),
        },
        "entries": entries,
    }


def _load_overlays(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*/*.json")):
        if path.name in {"progress.json", "glossary.json"}:
            continue
        document = _read_json(path)
        for item in document.get("entries", []):
            if not isinstance(item, dict) or not isinstance(item.get("entryId"), str):
                raise DialogueReviewError(f"오버레이 행이 잘못되었습니다: {path}")
            entry_id = item["entryId"]
            if entry_id in result:
                raise DialogueReviewError(f"오버레이 행이 중복됩니다: {entry_id}")
            result[entry_id] = item
    return result


def _scenario_target(scope: str, asset_key: str) -> str:
    if scope == "stage":
        return f"DATA/STAGE/{asset_key}.cpk"
    if scope == "dlc" and asset_key.startswith("DLC"):
        return f"{asset_key}/DLC/{asset_key}.cpk"
    return asset_key


def build_scenario(
    normalized_path: Path,
    overlay_root: Path,
) -> dict[str, dict[str, Any]]:
    overlays = _load_overlays(overlay_root)
    progress_path = overlay_root / "progress.json"
    progress = _read_json(progress_path) if progress_path.is_file() else {}
    baseline = progress.get("baseline") if isinstance(progress.get("baseline"), dict) else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    asset_meta: dict[str, dict[str, str]] = {}
    try:
        stream = normalized_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise DialogueReviewError(f"정규화 TSV를 열 수 없습니다: {normalized_path}") from error
    with stream:
        reader = csv.DictReader(stream, delimiter="\t")
        required = {"entry_id", "scope", "asset_key", "internal_id", "source_row", "source_text", "source_artifact", "source_artifact_sha256", "control_signature"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise DialogueReviewError(f"정규화 TSV 열이 없습니다: {sorted(missing)}")
        for row in reader:
            scope = str(row["scope"])
            if scope not in {"stage", "dlc"}:
                continue
            entry_id = str(row["entry_id"])
            overlay = overlays.get(entry_id)
            if overlay is None:
                raise DialogueReviewError(f"시나리오 오버레이에 행이 없습니다: {entry_id}")
            source = str(row["source_text"])
            if overlay.get("sourceTextSha256") != sha256_text(source):
                raise DialogueReviewError(f"시나리오 원문 해시가 다릅니다: {entry_id}")
            translation = str(overlay.get("freshTranslation", ""))
            controls = _controls(_tokens_from_signature(str(row.get("control_signature", ""))), translation)
            source_payload_digest = str(row.get("payload_sha256", "")) or None
            entry = _entry(
                entry_id=entry_id,
                source_text=source,
                translation=translation,
                status=str(overlay.get("translationStatus", "draft")),
                translator=str(overlay.get("translator", "")),
                reviewer=str(overlay.get("reviewer", "")),
                reference_consulted=bool(overlay.get("referenceConsulted", False)),
                notes=str(overlay.get("notes", "")),
                controls=controls,
                location={
                    "scope": scope,
                    "assetKey": str(row["asset_key"]),
                    "internalId": str(row["internal_id"]),
                    "sourceRow": _integer(str(row["source_row"]), f"{entry_id}.sourceRow", allow_empty=False),
                    "sourceArtifact": str(row["source_artifact"]),
                    "sourceArtifactSha256": str(row["source_artifact_sha256"]),
                    "sourceBindingSha256": str(overlay.get("sourceBindingSha256", "")),
                },
                source_payload=_payload(digest=source_payload_digest),
                applied_payload=_payload(byte_length=_integer(str(row.get("encoded_length", "")), f"{entry_id}.encodedLength")),
                references={
                    "googleTranslation": str(row.get("google_translation", "")),
                    "legacyTranslation": str(row.get("legacy_translation", "")),
                    "importedTranslation": str(row.get("translation", "")),
                },
                metadata={
                    "replacementText": str(row.get("replacement_text", "")),
                    "byteLimit": _integer(str(row.get("byte_limit", "")), f"{entry_id}.byteLimit"),
                    "encodedLength": _integer(str(row.get("encoded_length", "")), f"{entry_id}.encodedLength"),
                },
            )
            asset_key = str(row["asset_key"])
            grouped.setdefault(asset_key, []).append(entry)
            asset_meta[asset_key] = {"scope": scope, "artifact": str(row["source_artifact"])}
    documents: dict[str, dict[str, Any]] = {}
    for asset_key, entries in sorted(grouped.items()):
        meta = asset_meta[asset_key]
        documents[f"scenario_{asset_key}.json"] = _base_document(
            dialogue_type="scenario",
            asset={
                "assetKey": asset_key,
                "fileName": f"{asset_key}.cpk",
                "target": _scenario_target(meta["scope"], asset_key),
                "source": {"kind": "normalized-translations-tsv", "fileName": normalized_path.name},
                "localized": None,
            },
            entries=entries,
            source_snapshot={
                "fileName": str(progress.get("baseline", {}).get("fileName", normalized_path.name)),
                "sha256": str(baseline.get("sha256", "")),
                "rowCount": baseline.get("rowCount"),
            },
            policy={
                "requiresUserOwnedGame": True,
                "gameBinaryIncluded": False,
                "originalJapaneseTextIncluded": True,
                "method": "fresh-from-japanese",
                "existingTranslations": "reference-only",
            },
        )
    return documents


def build_battle(path: Path) -> dict[str, Any]:
    root = _read_json(path)
    asset = root.get("asset")
    if not isinstance(asset, dict):
        raise DialogueReviewError("전투 JSON에 asset이 없습니다.")
    slots_by_master: dict[int, list[dict[str, Any]]] = {}
    for slot in root.get("slots", []):
        if not isinstance(slot, dict):
            continue
        slots_by_master.setdefault(int(slot["masterId"]), []).append(
            {
                "slot": int(slot["slot"]),
                "offset": int(slot["offset"]),
                "masterId": int(slot["masterId"]),
                "bytes": int(slot["sourceByteLength"]),
                "applied": bool(slot.get("applied", False)),
            }
        )
    entries: list[dict[str, Any]] = []
    for item in root.get("master", []):
        if not isinstance(item, dict):
            raise DialogueReviewError("전투 master 행이 잘못되었습니다.")
        identifier = int(item["id"])
        translation = str(item["translation"])
        source = str(item["sourceText"])
        applied = bytes.fromhex(str(item["appliedPayloadHex"]))
        entries.append(
            _entry(
                entry_id=f"SRVC.BIN/master/{identifier:05d}",
                source_text=source,
                translation=translation,
                status=str(item.get("status", "draft")),
                translator="",
                reviewer="",
                reference_consulted=False,
                notes=str(item.get("reviewNote", "")),
                controls=_controls(_tokens_from_text(source), translation),
                location={
                    "masterId": identifier,
                    "offset": int(item["offset"]),
                    "repeatCount": int(item.get("repeatCount", 0)),
                    "slots": slots_by_master.get(identifier, []),
                },
                source_payload=_payload(byte_length=int(item["sourceByteLength"]), digest=str(item["originalPayloadSha256"])),
                applied_payload=_payload(data=applied),
                metadata={
                    "sourceByteLength": int(item["sourceByteLength"]),
                    "translationByteLength": int(item["translationByteLength"]),
                    "spareBytes": int(item["spareBytes"]),
                },
            )
        )
    return _base_document(
        dialogue_type="battle",
        asset={
            "assetKey": "SRVC",
            "fileName": str(asset["fileName"]),
            "target": "DATA/BTLC/SRVC.BIN",
            "source": {"bytes": int(asset["bytes"]), "sha256": str(asset["originalSha256"])},
            "localized": {"bytes": int(asset["bytes"]), "sha256": str(asset["appliedSha256"])},
            "originalSha256": str(asset["originalSha256"]),
            "appliedSha256": str(asset["appliedSha256"]),
        },
        entries=entries,
        encoding=root.get("encoding") if isinstance(root.get("encoding"), dict) else {},
        policy=root.get("policy") if isinstance(root.get("policy"), dict) else None,
    )


def build_dictionary(path: Path) -> dict[str, Any]:
    root = _read_json(path)
    asset = root.get("asset")
    localized = root.get("localizedAsset")
    if not isinstance(asset, dict) or not isinstance(localized, dict):
        raise DialogueReviewError("사전 JSON에 asset/localizedAsset이 없습니다.")
    entries: list[dict[str, Any]] = []
    for item in root.get("entries", []):
        if not isinstance(item, dict):
            raise DialogueReviewError("사전 entries 행이 잘못되었습니다.")
        source_payload = bytes.fromhex(str(item["sourcePayloadHex"]))
        applied_payload = bytes.fromhex(str(item["appliedPayloadHex"]))
        translation = str(item["translation"])
        entries.append(
            _entry(
                entry_id=str(item["entryId"]),
                source_text=str(item["sourceText"]),
                translation=translation,
                status=str(item.get("translationStatus", "draft")),
                translator=str(item.get("translator", "")),
                reviewer=str(item.get("reviewer", "")),
                reference_consulted=bool(item.get("referenceConsulted", False)),
                notes=str(item.get("notes", "")),
                controls=_controls(list(item.get("controlTokens", [])), translation),
                location={
                    "memberId": int(item["memberId"]),
                    "field": str(item["field"]),
                    "memberPayloadOffset": int(item["memberPayloadOffset"]),
                    "payloadOffset": int(item["payloadOffset"]),
                    "capacityBytes": int(item["capacityBytes"]),
                },
                source_payload=_payload(data=source_payload),
                applied_payload=_payload(data=applied_payload),
                metadata={"capacityBytes": int(item["capacityBytes"])},
            )
        )
    return _base_document(
        dialogue_type="dictionary",
        asset={
            "assetKey": "MtZkn_KW",
            "fileName": str(asset["fileName"]),
            "memberCount": int(asset.get("memberCount", 0)),
            "target": "CommonData/MtData/MtZkn_KW.cpk",
            "source": {"bytes": int(asset["bytes"]), "sha256": str(asset["sha256"])},
            "localized": {"bytes": int(localized["bytes"]), "sha256": str(localized["sha256"])},
            "originalSha256": str(asset["sha256"]),
            "appliedSha256": str(localized["sha256"]),
        },
        entries=entries,
        encoding=root.get("encoding") if isinstance(root.get("encoding"), dict) else {},
        policy=root.get("policy") if isinstance(root.get("policy"), dict) else None,
    )


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_bundle(
    *,
    normalized_path: Path,
    overlay_root: Path,
    battle_path: Path,
    dictionary_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    if not output_root.resolve().is_relative_to(REPOSITORY_ROOT / "translations"):
        raise DialogueReviewError("출력 폴더는 translations/ 아래여야 합니다.")
    documents = build_scenario(normalized_path, overlay_root)
    documents["battle_SRVC.json"] = build_battle(battle_path)
    documents["dictionary_MtZkn_KW.json"] = build_dictionary(dictionary_path)
    output_root.mkdir(parents=True, exist_ok=True)
    for name, document in sorted(documents.items()):
        _write(output_root / name, document)
    return {
        "output": str(output_root),
        "files": len(documents),
        "scenarioFiles": sum(name.startswith("scenario_") for name in documents),
        "battleFiles": 1,
        "dictionaryFiles": 1,
        "scenarioEntries": sum(len(doc["entries"]) for name, doc in documents.items() if name.startswith("scenario_")),
        "battleEntries": len(documents["battle_SRVC.json"]["entries"]),
        "dictionaryEntries": len(documents["dictionary_MtZkn_KW.json"]["entries"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalized", type=Path, default=DEFAULT_NORMALIZED)
    parser.add_argument("--retranslation-root", type=Path, default=DEFAULT_RETRANSLATION)
    parser.add_argument("--battle-json", type=Path, default=DEFAULT_BATTLE)
    parser.add_argument("--dictionary-json", type=Path, default=DEFAULT_DICTIONARY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        report = build_bundle(
            normalized_path=args.normalized.expanduser().resolve(strict=True),
            overlay_root=args.retranslation_root.expanduser().resolve(strict=True),
            battle_path=args.battle_json.expanduser().resolve(strict=True),
            dictionary_path=args.dictionary_json.expanduser().resolve(strict=True),
            output_root=args.output_root.expanduser().resolve(),
        )
    except (DialogueReviewError, OSError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
