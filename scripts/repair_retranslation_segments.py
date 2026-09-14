"""제어 토큰 사이의 일본어 조각을 나누어 재번역한다."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import tempfile

from auto_translate_retranslation import (
    CONTROL_TOKEN,
    JAPANESE_TEXT,
    _translate_batch,
    _translate_one_safe,
)
from auto_translate_retranslation import _update_progress
from siok_patch.config import project_root
from siok_patch.hashes import sha256_file
from siok_patch.retranslation import publish_retranslation_task
from siok_patch.translation_io import read_tsv


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


def _split_controls(text: str) -> tuple[list[str], list[str]]:
    parts: list[str] = []
    tokens: list[str] = []
    last = 0
    for match in CONTROL_TOKEN.finditer(text):
        parts.append(text[last : match.start()])
        tokens.append(match.group(0))
        last = match.end()
    parts.append(text[last:])
    return parts, tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--scope", action="append")
    parser.add_argument("--asset", action="append")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=1200)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers는 1~16이어야 합니다.")
    if args.max_chars < 300 or args.max_chars > 5000:
        parser.error("--max-chars는 300~5000이어야 합니다.")

    root = project_root()
    source = args.source.resolve()
    task_root = args.task_root.resolve()
    output_root = args.output_root.resolve()
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

    segment_sources: dict[str, str] = {}
    entry_segments: dict[str, list[tuple[str, str]]] = {}
    for _, _, entry in jobs:
        entry_id = str(entry["entryId"])
        source_parts, source_tokens = _split_controls(str(entry.get("sourceText", "")))
        fresh_parts, fresh_tokens = _split_controls(str(entry.get("freshTranslation", "")))
        if source_tokens != fresh_tokens or len(source_parts) != len(fresh_parts):
            continue
        pieces: list[tuple[str, str]] = []
        for index, source_part in enumerate(source_parts):
            current_part = fresh_parts[index]
            if not JAPANESE_TEXT.search(source_part) and not JAPANESE_TEXT.search(current_part):
                pieces.append(("keep", current_part))
                continue
            key = source_part
            if key.strip():
                segment_sources.setdefault(key, source_part)
                pieces.append(("translate", key))
            else:
                pieces.append(("keep", current_part))
        entry_segments[entry_id] = pieces

    print(f"제어 토큰 사이 일본어 조각 재번역 대상: {len(segment_sources)}개", flush=True)
    translated_segments: dict[str, str] = {}
    segment_items = list(segment_sources.items())
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_size = 0
    for index, (_, text) in enumerate(segment_items):
        extra = len(text) + (1 if current else 0)
        if current and current_size + extra > args.max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append((index, text))
        current_size += len(text) + (1 if len(current) > 1 else 0)
    if current:
        batches.append(current)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_translate_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            result = future.result()
            for index, _ in batch:
                text = result.get(index)
                if text is None:
                    text = _translate_one_safe(segment_items[index][1])
                translated_segments[segment_items[index][0]] = text

    changed = 0
    unresolved = 0
    touched: dict[Path, dict] = {}
    for task_path, task, entry in jobs:
        pieces = entry_segments.get(str(entry["entryId"]))
        if pieces is None:
            continue
        source_parts, source_tokens = _split_controls(str(entry.get("sourceText", "")))
        output_parts: list[str] = []
        for kind, value in pieces:
            text = translated_segments.get(value, value) if kind == "translate" else value
            output_parts.append(text)
            if JAPANESE_TEXT.search(text):
                unresolved += 1
        fresh_parts, fresh_tokens = _split_controls(str(entry.get("freshTranslation", "")))
        output = ""
        for index, part in enumerate(output_parts):
            output += part
            if index < len(source_tokens):
                output += source_tokens[index]
        if output == entry.get("freshTranslation"):
            continue
        entry["freshTranslation"] = output
        previous = str(entry.get("translator", "")).strip()
        entry["translator"] = (
            f"{previous}; Codex 제어 토큰 단위 재번역"
            if previous
            else "Codex 제어 토큰 단위 재번역"
        )
        entry["notes"] = (
            str(entry.get("notes", "")).strip()
            + ("; " if entry.get("notes") else "")
            + "제어 토큰 사이 일본어 조각을 분리해 원문 기준 재번역"
        )
        touched[task_path] = task
        changed += 1

    for task_path, task in touched.items():
        _write_json_atomic(task, task_path)
        output_path = output_root / str(task["scope"]) / f"{task['assetKey']}.json"
        publish_retranslation_task(
            task_path,
            output_path=output_path,
            output_root=output_root,
            overwrite=True,
        )
    _update_progress(root, baseline_rows, sha256_file(source), output_root)
    print(f"조각 재번역 완료: 변경 {changed}행 / 조각 기준 잔류 {unresolved}개", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
