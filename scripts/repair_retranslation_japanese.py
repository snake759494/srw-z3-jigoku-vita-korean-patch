"""새 번역 초안에 남은 일본어 문자를 원문 기준으로 다시 번역한다."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import tempfile
from pathlib import Path

from auto_translate_retranslation import JAPANESE_TEXT, _translate_one_safe
from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import publish_retranslation_task
from siok_patch.translation_io import read_tsv
from auto_translate_retranslation import _update_progress


def _write_json_atomic(value: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".candidate", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="번역 기준 TSV. 생략하면 기본 재번역 TSV를 사용합니다.",
    )
    parser.add_argument(
        "--task-root",
        type=Path,
        help="로컬 재번역 작업 JSON 루트",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="공개 재번역 오버레이 루트",
    )
    parser.add_argument(
        "--scope", action="append", help="지정하면 해당 범위만 처리합니다. 여러 번 지정할 수 있습니다."
    )
    parser.add_argument(
        "--asset", action="append", help="지정하면 해당 자산만 처리합니다. 여러 번 지정할 수 있습니다."
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers는 1~8이어야 합니다.")
    root = project_root()
    source = (
        args.source.resolve()
        if args.source is not None
        else (root / "work" / "normalized" / "translations.tsv").resolve()
    )
    task_root = (
        args.task_root.resolve()
        if args.task_root is not None
        else (root / "work" / "retranslation" / "tasks").resolve()
    )
    output_root = (
        args.output_root.resolve()
        if args.output_root is not None
        else (root / "translations" / "retranslation").resolve()
    )
    baseline_rows = read_tsv(source)
    scopes = set(args.scope or (row.scope for row in baseline_rows))
    assets = set(args.asset or ())
    jobs: list[tuple[Path, dict, dict]] = []
    for task_path in sorted(task_root.glob("*/*.json")):
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task.get("scope") not in scopes:
            continue
        if assets and task.get("assetKey") not in assets:
            continue
        for entry in task.get("entries", []):
            if JAPANESE_TEXT.search(str(entry.get("freshTranslation", ""))):
                jobs.append((task_path, task, entry))
    print(f"일본어 잔류 재번역 대상: {len(jobs)}행", flush=True)

    def translate(item: tuple[Path, dict, dict]) -> tuple[str, str]:
        _, _, entry = item
        return entry["entryId"], _translate_one_safe(str(entry.get("sourceText", "")))

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(translate, item) for item in jobs]
        for future in as_completed(futures):
            entry_id, text = future.result()
            results[entry_id] = text

    changed = 0
    unresolved = 0
    touched: dict[Path, dict] = {}
    for task_path, task, entry in jobs:
        text = results[entry["entryId"]]
        if not text.strip():
            continue
        if JAPANESE_TEXT.search(text):
            unresolved += 1
        if text == entry.get("freshTranslation"):
            continue
        entry["freshTranslation"] = text
        previous = str(entry.get("translator", "")).strip()
        entry["translator"] = (
            f"{previous}; Codex 일본어 잔류 재번역"
            if previous
            else "Codex 일본어 잔류 재번역"
        )
        entry["notes"] = (
            str(entry.get("notes", "")).strip()
            + ("; " if entry.get("notes") else "")
            + "일본어 잔류 행을 원문 기준으로 재번역"
        )
        touched[task_path] = task
        changed += 1
    for task_path, task in touched.items():
        _write_json_atomic(task, task_path)
        output = output_root / str(task["scope"]) / f"{task['assetKey']}.json"
        publish_retranslation_task(
            task_path,
            output_path=output,
            output_root=output_root,
            overwrite=True,
        )
    _update_progress(root, baseline_rows, sha256_file(source), output_root)
    print(f"일본어 잔류 재번역 완료: 변경 {changed}행 / 미해결 {unresolved}행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
