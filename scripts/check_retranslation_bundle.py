"""추가 자산 재번역 TSV와 공개 오버레이의 전체 결박을 검사한다."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from auto_translate_retranslation import JAPANESE_TEXT
from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import validate_retranslation_overlays
from siok_patch.translation_io import read_tsv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="기준 translations.tsv")
    parser.add_argument("--output-root", type=Path, required=True, help="공개 오버레이 루트")
    parser.add_argument("--report", type=Path, help="검사 보고서 JSON 출력 경로")
    parser.add_argument(
        "--fail-on-japanese",
        action="store_true",
        help="번역 결과에 가나가 남아 있으면 실패합니다.",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    output_root = args.output_root.resolve()
    rows = read_tsv(source)
    overlays = sorted(output_root.glob("*/*.json"))
    if not overlays:
        print("오버레이를 찾지 못했습니다.", file=sys.stderr)
        return 2

    structural = validate_retranslation_overlays(source, overlays)
    expected_ids = {row.entry_id for row in rows}
    observed_ids: list[str] = []
    japanese_rows = 0
    japanese_chars = 0
    status_counts: Counter[str] = Counter()
    for overlay_path in overlays:
        value = json.loads(overlay_path.read_text(encoding="utf-8"))
        for entry in value.get("entries", []):
            entry_id = str(entry.get("entryId", ""))
            observed_ids.append(entry_id)
            status_counts[str(entry.get("translationStatus", ""))] += 1
            text = str(entry.get("freshTranslation", ""))
            matches = JAPANESE_TEXT.findall(text)
            if matches:
                japanese_rows += 1
                japanese_chars += len(matches)
    observed_set = set(observed_ids)
    duplicate_ids = sorted(
        entry_id for entry_id, count in Counter(observed_ids).items() if count > 1
    )
    missing_ids = sorted(expected_ids - observed_set)
    extra_ids = sorted(observed_set - expected_ids)
    report = {
        "format": "siok-retranslation-bundle-report",
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "rowCount": len(rows),
            "assetCount": len({(row.scope, row.asset_key) for row in rows}),
        },
        "outputRoot": str(output_root),
        "overlayCount": len(overlays),
        "overlayRowCount": len(observed_ids),
        "statusCounts": dict(sorted(status_counts.items())),
        "japaneseResidual": {
            "rows": japanese_rows,
            "characters": japanese_chars,
        },
        "coverage": {
            "missingEntryIds": missing_ids,
            "extraEntryIds": extra_ids,
            "duplicateEntryIds": duplicate_ids,
        },
        "structural": structural,
    }
    if args.report:
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    structural_ok = bool(structural.get("ok"))
    coverage_ok = not (missing_ids or extra_ids or duplicate_ids)
    japanese_ok = not args.fail_on_japanese or japanese_rows == 0
    print(
        f"검사: 오버레이 {len(overlays)}개 / {len(observed_ids):,}행, "
        f"기준 {len(rows):,}행, 일본어 잔류 {japanese_rows:,}행 "
        f"({japanese_chars:,}자), 구조={'OK' if structural_ok else 'FAIL'}, "
        f"범위={'OK' if coverage_ok else 'FAIL'}"
    )
    if not structural_ok:
        print(f"구조 오류: {len(structural.get('errors', []))}개", file=sys.stderr)
    if missing_ids:
        print(f"누락 entryId: {len(missing_ids)}개", file=sys.stderr)
    if extra_ids:
        print(f"기준 외 entryId: {len(extra_ids)}개", file=sys.stderr)
    if duplicate_ids:
        print(f"중복 entryId: {len(duplicate_ids)}개", file=sys.stderr)
    if args.fail_on_japanese and japanese_rows:
        print("일본어 잔류 검사를 통과하지 못했습니다.", file=sys.stderr)
    return 0 if structural_ok and coverage_ok and japanese_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
