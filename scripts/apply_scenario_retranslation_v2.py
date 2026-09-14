#!/usr/bin/env python3
"""오프라인/직접 재번역 결과를 공통 시나리오 JSON에 병합하고 QA한다.

번역 결과는 `work/retranslation-v2*/completed` 아래에 있어야 한다. 원문
SHA-256과 entryId가 일치하는 경우에만 병합하며, 원문·기존 참고 번역·패치
바이너리는 변경하지 않는다. 기본 동작은 검사만 하고 `--apply`를 지정해야
`translations/dialogue/scenario_*.json`을 갱신한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIALOGUE = ROOT / "translations" / "dialogue"
DEFAULT_WORK = ROOT / "work" / "retranslation-v2"
CONTROL_RE = re.compile(r"(⑲|⑳|㊥|㊦|㊧|㊨|(?:\$|＄)[nlcF])")
JAPANESE_RE = re.compile(r"[ぁ-ゖァ-ヺ一-龯々〆ヵヶ]")

CANONICAL_TERMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("時獄戦役", ("시점 전쟁", "감옥 전쟁", "감옥의 전쟁", "시옥 전쟁"), "시옥전쟁"),
    ("時獄篇", ("시점 편", "감옥 편", "시옥 편"), "시옥편"),
    ("破界事変", ("파괴 사건", "파계 사건"), "파계사변"),
    ("再世戦争", ("재생 전쟁", "재세 전쟁"), "재세전쟁"),
    ("大時空震", ("큰 시공진동", "대시공진동"), "대시공진동"),
    ("時空震", ("시공 진동",), "시공진동"),
    ("多元世界", ("다원 세계",), "다원세계"),
    ("平行世界", ("평행 세계",), "평행세계"),
    ("並行世界", ("평행 세계",), "평행세계"),
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tokens(value: str) -> list[str]:
    return CONTROL_RE.findall(value)


def canonicalize(source: str, value: str) -> str:
    """모델의 흔들리는 용어·문장부호를 프로젝트 표기로 정리한다."""

    # 번역문은 게임 삽입 규칙에 맞춰 ASCII 반각 공백을 사용한다.
    result = value.replace("\u3000", " ")
    for japanese, variants, canonical in CANONICAL_TERMS:
        if japanese not in source:
            continue
        for variant in variants:
            result = result.replace(variant, canonical)
    # 일본어식 공백이 남은 문장부호만 정리하고 제어코드 자체는 건드리지 않는다.
    result = re.sub(r"\s+([,.;:!?。！？，．：；])", r"\1", result)
    result = re.sub(r"[ \t]{2,}", " ", result)
    return result


def resolve_translation(entry: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, bool]:
    """빈 생성 결과만 기존 JSON의 참고 번역으로 보완한다.

    새 초안이 있는 경우에는 그 값을 그대로 사용한다. 생성 결과가 비어
    있을 때만 기존 `references`를 우선순위대로 참고하고, 마지막으로 현재
    entry의 값을 확인한다. 원문이 제어코드뿐인 경우에는 코드 자체를
    보존해 빈 문자열이 패치 데이터로 들어가지 않게 한다.
    """

    source = str(entry.get("sourceText", ""))
    value = str(candidate.get("translation", ""))
    marker_miss = int(candidate.get("markerMisses") or 0) > 0

    def finalize(raw: str) -> str:
        result = canonicalize(source, raw)
        if tokens(source) != tokens(result):
            # 모든 시나리오 제어코드는 현재 원문 행의 끝에 있으므로,
            # 번역 중 변형된 토큰을 제거하고 원문 수열을 끝에 복원한다.
            result = CONTROL_RE.sub("", result) + "".join(tokens(source))
        return result

    if (value.strip() and not marker_miss) or not source.strip():
        return finalize(value), False

    references = entry.get("references") if isinstance(entry.get("references"), dict) else {}
    for key in ("importedTranslation", "legacyTranslation", "googleTranslation"):
        reference = str(references.get(key, ""))
        if reference.strip():
            return finalize(reference), True

    current = str(entry.get("translation", ""))
    if current.strip():
        return finalize(current), True

    # 보호 용어 복원 실패인데 참고값도 없으면 생성 초안을 보존하고 QA에
    # markerMisses를 남긴다. 빈 결과는 아래에서 별도로 처리한다.
    if value.strip():
        return finalize(value), False

    # 일본어가 없는 제어코드/구분자 항목은 원문을 그대로 보존한다.
    if not JAPANESE_RE.search(source):
        return finalize(source), True
    return "", False


def exceeds_visual_limit(value: str, metadata: dict[str, Any]) -> bool:
    limit = metadata.get("byteLimit")
    if not limit:
        return False
    visual_slots = len(CONTROL_RE.sub("X", value))
    return visual_slots > int(limit)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_completed(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
        if isinstance(item, dict) and item.get("entryId"):
            result[str(item["entryId"])] = item
    return result


def source_entries_match(document: dict[str, Any], result: dict[str, dict[str, Any]]) -> bool:
    """번역을 한 번 적용한 뒤에도 원문 필드가 같은지 확인한다.

    첫 적용 후에는 JSON의 번역 메타데이터가 바뀌므로 파일 전체 SHA-256은
    달라진다. 이때 entry별 원문 SHA-256을 비교해 안전하게 재적용을
    허용한다.
    """

    for entry in document.get("entries", []):
        entry_id = str(entry.get("entryId", ""))
        candidate = result.get(entry_id)
        if candidate is None:
            return False
        source = str(entry.get("sourceText", ""))
        expected = entry.get("sourceTextSha256") or sha256_text(source)
        if candidate.get("sourceTextSha256") != expected or expected != sha256_text(source):
            return False
    return True


def check_asset(document: dict[str, Any], result: dict[str, dict[str, Any]], asset_name: str) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    stats = {
        "entries": 0,
        "updated": 0,
        "missing": 0,
        "sourceHashMismatch": 0,
        "controlMismatch": 0,
        "emptyTranslation": 0,
        "referenceFallback": 0,
        "japaneseRemainder": 0,
        "lengthOverflow": 0,
        "markerMisses": 0,
    }
    for entry in document.get("entries", []):
        stats["entries"] += 1
        entry_id = str(entry.get("entryId", ""))
        candidate = result.get(entry_id)
        if candidate is None:
            stats["missing"] += 1
            errors.append(f"{asset_name}:{entry_id}: 번역 결과 없음")
            continue
        source = str(entry.get("sourceText", ""))
        if candidate.get("sourceTextSha256") != (entry.get("sourceTextSha256") or sha256_text(source)):
            stats["sourceHashMismatch"] += 1
            errors.append(f"{asset_name}:{entry_id}: 원문 SHA-256 불일치")
            continue
        translation, fallback_used = resolve_translation(entry, candidate)
        if fallback_used:
            stats["referenceFallback"] += 1
        if not translation.strip() and source.strip():
            stats["emptyTranslation"] += 1
            errors.append(f"{asset_name}:{entry_id}: 번역 문자열이 비어 있음")
        if tokens(source) != tokens(translation):
            stats["controlMismatch"] += 1
            errors.append(f"{asset_name}:{entry_id}: 제어코드 종류·순서 불일치")
        if JAPANESE_RE.search(translation):
            stats["japaneseRemainder"] += 1
        length = candidate.get("length") if isinstance(candidate.get("length"), dict) else {}
        if length.get("overflow") or exceeds_visual_limit(translation, entry.get("metadata") or {}):
            stats["lengthOverflow"] += 1
        stats["markerMisses"] += int(candidate.get("markerMisses") or 0)
        stats["updated"] += 1
    return errors, stats


def apply_asset(document: dict[str, Any], result: dict[str, dict[str, Any]]) -> None:
    for entry in document.get("entries", []):
        candidate = result.get(str(entry.get("entryId", "")))
        if candidate is None:
            continue
        translation, fallback_used = resolve_translation(entry, candidate)
        entry["translation"] = translation
        entry["translationTextSha256"] = sha256_text(translation)
        entry["translationStatus"] = "draft"
        entry["translator"] = "직접 재번역 초안(문맥·용어집·대사창 길이 검수 대기)"
        entry["reviewer"] = ""
        entry["referenceConsulted"] = fallback_used
        entry["notes"] = "일본어 원문 기준 새 번역 초안; 기존 번역은 참고 자료로만 유지"
        if fallback_used and not str(candidate.get("translation", "")).strip():
            entry["notes"] += "; 생성 결과 공백으로 기존 참고 번역을 임시 유지(재검수 필요)"
        elif fallback_used and int(candidate.get("markerMisses") or 0) > 0:
            entry["notes"] += "; 보호 용어 복원 실패로 기존 참고 번역을 임시 유지(재검수 필요)"
        entry["controls"]["sourceTokens"] = tokens(entry["sourceText"])
        entry["controls"]["translationTokens"] = tokens(entry["translation"])
        entry["controls"]["sourceSignature"] = "|".join(entry["controls"]["sourceTokens"])
        entry["controls"]["translationSignature"] = "|".join(entry["controls"]["translationTokens"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogue-root", type=Path, default=DEFAULT_DIALOGUE)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--apply", action="store_true", help="QA 통과 여부와 관계없이 일치하는 결과를 JSON에 병합")
    parser.add_argument("--allow-missing", action="store_true", help="미완료 자산도 검사 보고서에 기록하고 종료코드는 0")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    completed_root = args.work_root / "completed"
    report: dict[str, Any] = {"format": "siok.scenario-retranslation-v2-qa", "assets": [], "errors": []}
    total = {"entries": 0, "updated": 0, "missing": 0, "sourceHashMismatch": 0, "controlMismatch": 0, "emptyTranslation": 0, "referenceFallback": 0, "japaneseRemainder": 0, "lengthOverflow": 0, "markerMisses": 0}
    for source_path in sorted(args.dialogue_root.glob("scenario_*.json")):
        completed_path = completed_root / source_path.name
        document = json.loads(source_path.read_text(encoding="utf-8"))
        if not completed_path.exists():
            report["errors"].append(f"{source_path.name}: 완료 파일 없음")
            continue
        completed = json.loads(completed_path.read_text(encoding="utf-8"))
        expected_sha = sha256_text(source_path.read_text(encoding="utf-8"))
        result = load_completed(completed_path)
        if completed.get("sourceFileSha256") != expected_sha and not source_entries_match(document, result):
            report["errors"].append(f"{source_path.name}: 원문 필드 SHA-256 불일치")
            continue
        errors, stats = check_asset(document, result, source_path.name)
        report["errors"].extend(errors)
        report["assets"].append({"asset": source_path.name, "stats": stats})
        for key in total:
            total[key] += stats[key]
        if args.apply:
            apply_asset(document, result)
            atomic_json(source_path, document)
    report["total"] = total
    report["ok"] = not report["errors"] and total["missing"] == 0 and total["controlMismatch"] == 0 and total["emptyTranslation"] == 0
    report_path = args.work_root / "qa-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(report_path, report)
    print(json.dumps({"ok": report["ok"], "total": total, "errors": len(report["errors"]), "report": str(report_path)}, ensure_ascii=False))
    if report["errors"] and not args.allow_missing:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
