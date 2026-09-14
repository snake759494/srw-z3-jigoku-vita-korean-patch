#!/usr/bin/env python3
"""공통 대사 검수 JSON 폴더의 형식·행 결박·해시를 검사한다."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPOSITORY_ROOT / "translations" / "dialogue"
REQUIRED_ENTRY_KEYS = {
    "entryId",
    "sourceText",
    "translation",
    "sourceTextSha256",
    "translationTextSha256",
    "translationStatus",
    "translator",
    "reviewer",
    "referenceConsulted",
    "notes",
    "controls",
    "location",
    "sourcePayload",
    "appliedPayload",
    "references",
    "metadata",
}
OPTIONAL_ENTRY_KEYS = {
    # 1화 재번역 비교용 필드. 기존 translation 계약은 그대로 두고,
    # 신규 초안만 별도 값·해시·상태로 보관할 수 있다.
    "newTranslation",
    "newTranslationTextSha256",
    "newTranslationStatus",
    "newTranslationReference",
}


class DialogueReviewCheckError(ValueError):
    """검수 JSON이 공통 계약을 만족하지 않을 때 발생한다."""


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _check_payload(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"bytes", "sha256", "hex"}:
        raise DialogueReviewCheckError(f"{label} 페이로드 구조가 잘못되었습니다.")
    raw_hex = value["hex"]
    if raw_hex is None:
        return
    if not isinstance(raw_hex, str):
        raise DialogueReviewCheckError(f"{label}.hex가 문자열이 아닙니다.")
    try:
        data = bytes.fromhex(raw_hex)
    except ValueError as error:
        raise DialogueReviewCheckError(f"{label}.hex가 16진수가 아닙니다.") from error
    if value["bytes"] != len(data) or value["sha256"] != _hash_bytes(data):
        raise DialogueReviewCheckError(f"{label}의 바이트 길이·SHA-256이 일치하지 않습니다.")


def check_file(path: Path) -> dict[str, Any]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DialogueReviewCheckError(f"JSON을 읽을 수 없습니다: {path}") from error
    if not isinstance(root, dict) or root.get("format") != "siok.dialogue-review" or root.get("formatVersion") != 1:
        raise DialogueReviewCheckError(f"공통 대사 JSON 형식이 아닙니다: {path}")
    entries = root.get("entries")
    if not isinstance(entries, list) or not entries:
        raise DialogueReviewCheckError(f"entries가 비어 있습니다: {path}")
    seen: set[str] = set()
    for index, item in enumerate(entries):
        label = f"{path.name}.entries[{index}]"
        if not isinstance(item, dict) or not REQUIRED_ENTRY_KEYS.issubset(item) or (set(item) - REQUIRED_ENTRY_KEYS) - OPTIONAL_ENTRY_KEYS:
            raise DialogueReviewCheckError(f"{label}의 공통 필드가 다릅니다.")
        entry_id = item["entryId"]
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            raise DialogueReviewCheckError(f"{label}.entryId가 비어 있거나 중복됩니다.")
        seen.add(entry_id)
        source = item["sourceText"]
        translation = item["translation"]
        if not isinstance(source, str) or not isinstance(translation, str):
            raise DialogueReviewCheckError(f"{label} 원문·번역이 문자열이 아닙니다.")
        if item["sourceTextSha256"] != _hash_text(source) or item["translationTextSha256"] != _hash_text(translation):
            raise DialogueReviewCheckError(f"{label} 원문·번역 SHA-256이 내용과 다릅니다.")
        if "newTranslation" in item:
            new_translation = item["newTranslation"]
            if not isinstance(new_translation, str):
                raise DialogueReviewCheckError(f"{label}.newTranslation이 문자열이 아닙니다.")
            new_hash = item.get("newTranslationTextSha256")
            if new_hash != _hash_text(new_translation):
                raise DialogueReviewCheckError(f"{label}.newTranslationTextSha256가 내용과 다릅니다.")
            new_status = item.get("newTranslationStatus")
            if not isinstance(new_status, str) or not new_status:
                raise DialogueReviewCheckError(f"{label}.newTranslationStatus가 비어 있습니다.")
            if not isinstance(item.get("newTranslationReference"), dict):
                raise DialogueReviewCheckError(f"{label}.newTranslationReference가 객체가 아닙니다.")
        controls = item["controls"]
        if not isinstance(controls, dict) or not all(isinstance(controls.get(key), (str, list)) for key in ("sourceTokens", "translationTokens", "sourceSignature", "translationSignature")):
            raise DialogueReviewCheckError(f"{label}.controls가 잘못되었습니다.")
        _check_payload(item["sourcePayload"], f"{label}.sourcePayload")
        _check_payload(item["appliedPayload"], f"{label}.appliedPayload")
    counts = root.get("counts")
    if not isinstance(counts, dict) or counts.get("entries") != len(entries) or counts.get("uniqueSourceTexts") != len({item["sourceText"] for item in entries}):
        raise DialogueReviewCheckError(f"{path.name}.counts가 실제 행 수와 다릅니다.")
    return {"file": path.name, "dialogueType": root.get("dialogueType"), "entries": len(entries), "translated": sum(bool(item["translation"].strip()) for item in entries)}


def check_bundle(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    files = sorted(root.glob("*.json"))
    if not files:
        raise DialogueReviewCheckError(f"검수 JSON이 없습니다: {root}")
    reports = [check_file(path) for path in files]
    types = {str(report["dialogueType"]) for report in reports}
    if types != {"scenario", "battle", "dictionary"}:
        raise DialogueReviewCheckError(f"시나리오·전투·사전 세 종류가 모두 필요합니다: {sorted(types)}")
    return {
        "root": str(root),
        "files": len(files),
        "scenarioFiles": sum(item["dialogueType"] == "scenario" for item in reports),
        "battleFiles": sum(item["dialogueType"] == "battle" for item in reports),
        "dictionaryFiles": sum(item["dialogueType"] == "dictionary" for item in reports),
        "entries": sum(int(item["entries"]) for item in reports),
        "reports": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    try:
        report = check_bundle(args.root)
    except (DialogueReviewCheckError, OSError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
