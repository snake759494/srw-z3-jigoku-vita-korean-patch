#!/usr/bin/env python3
"""용어집 기준 파일과 참전작 참고 자료의 재현 가능성을 검사한다.

게임 원본을 읽지 않고, 저장소에 허용된 JSON과 사용자가 지정한 XLSX 폴더의
SHA-256만 확인한다. Windows PowerShell에서 다음처럼 실행할 수 있다.

    python scripts/validate_translation_references.py \
      --excel-root "D:\\Z\\psvita\\시옥번역엑셀"
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "translations" / "retranslation" / "reference_rules.json"
DEFAULT_SERIES = ROOT / "translations" / "retranslation" / "series_reference.json"
DEFAULT_GLOSSARY = ROOT / "translations" / "retranslation" / "scenario_glossary_v2.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(rules_path: Path, series_path: Path, glossary_path: Path, excel_root: Path) -> dict[str, Any]:
    rules = load(rules_path)
    series = load(series_path)
    glossary = load(glossary_path)
    errors: list[str] = []

    files = rules.get("sourceFiles", [])
    checked_files = 0
    hash_mismatches: list[str] = []
    missing_files: list[str] = []
    for item in files:
        name = item.get("file", "")
        path = excel_root / name
        if not path.is_file():
            missing_files.append(name)
            continue
        checked_files += 1
        actual = sha256(path)
        if actual != item.get("sha256"):
            hash_mismatches.append(name)
    if missing_files:
        errors.append("missing XLSX: " + ", ".join(missing_files))
    if hash_mismatches:
        errors.append("SHA-256 mismatch: " + ", ".join(hash_mismatches))

    works = series.get("works", [])
    ids = [item.get("id") for item in works]
    if len(works) != 32:
        errors.append(f"참전작 수가 32가 아님: {len(works)}")
    if len(ids) != len(set(ids)):
        errors.append("참전작 id 중복")
    counts: dict[str, int] = {}
    for item in works:
        mark = item.get("participation")
        counts[mark] = counts.get(mark, 0) + 1
        for source_id in item.get("sourceIds", []):
            if not any(source.get("id") == source_id for source in series.get("sources", [])):
                errors.append(f"없는 source id: {item.get('id')} -> {source_id}")
    if counts != series.get("coverage", {}).get("participationCounts"):
        errors.append(f"참전 표식 집계 불일치: {counts}")

    for key in ("referenceRulesFile", "seriesReferenceFile"):
        if not glossary.get(key):
            errors.append(f"용어집 링크 누락: {key}")

    result = {
        "ok": not errors,
        "rules": str(rules_path),
        "series": str(series_path),
        "glossary": str(glossary_path),
        "excelRoot": str(excel_root),
        "sourceFileCount": len(files),
        "checkedFileCount": checked_files,
        "works": len(works),
        "participationCounts": counts,
        "glossaryRecords": len(glossary.get("records", [])),
        "errors": errors,
    }
    if errors:
        raise SystemExit(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--excel-root", type=Path, required=True)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--series", type=Path, default=DEFAULT_SERIES)
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    args = parser.parse_args()
    result = validate(args.rules, args.series, args.glossary, args.excel_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
