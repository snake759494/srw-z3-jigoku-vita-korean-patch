#!/usr/bin/env python3
"""여러 병렬 번역 작업의 시나리오 체크포인트를 하나로 합친다."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True, help="retranslation-v2 작업 폴더")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def copy_unique(source: Path, target: Path) -> int:
    count = 0
    if not source.exists():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if not path.is_file():
            continue
        destination = target / path.name
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise SystemExit(f"서로 다른 체크포인트가 충돌합니다: {destination}")
        shutil.copy2(path, destination)
        count += 1
    return count


def main() -> int:
    args = parse_args()
    completed = args.output / "completed"
    results = args.output / "results"
    summaries: list[dict[str, Any]] = []
    for root in args.input:
        copy_unique(root / "completed", completed)
        copy_unique(root / "results", results)
        summary = root / "summary.json"
        if summary.exists():
            summaries.append(json.loads(summary.read_text(encoding="utf-8")))
    args.output.mkdir(parents=True, exist_ok=True)
    merged_assets: list[dict[str, Any]] = []
    for summary in summaries:
        merged_assets.extend(summary.get("assets", []))
    (args.output / "summary.json").write_text(
        json.dumps({"format": "siok.scenario-retranslation-v2-summary", "assets": merged_assets}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"inputs": len(args.input), "completed": len(list(completed.glob('*.json'))), "results": len(list(results.glob('*.jsonl')))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
