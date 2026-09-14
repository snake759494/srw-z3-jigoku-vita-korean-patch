#!/usr/bin/env python3
"""시나리오 번역문에 찾아 바꾸기를 일괄 적용한다.

뷰어의 '찾아 바꾸기'는 한 번에 시나리오 하나만 다룬다. 용어를 174편 전체에
똑같이 반영하려면 이 스크립트를 쓴다. 화자명 행 판정은 뷰어와 같은 규칙
(``siok_patch.dialogue_speakers``)을 쓰므로 결과가 갈라지지 않는다.

기본은 미리보기다. 실제로 파일을 고치려면 ``--apply``를 붙인다.

    python scripts/replace_dialogue_text.py --find 신다원세기 --replace 新다원세기
    python scripts/replace_dialogue_text.py --find 신다원세기 --replace 新다원세기 --apply

바꾸기 전 원본은 ``work/text-replace/<시각>/backup/``에 복사해 둔다.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from siok_patch.dialogue_speakers import (  # noqa: E402
    control_tokens,
    is_speaker_entry,
    load_speaker_catalog,
)

DIALOGUE_DIR = ROOT / "translations" / "dialogue"
SPEAKER_CATALOG = ROOT / "viewer" / "scenario-speakers.json"
IDEOGRAPHIC_SPACE = "　"


class ReplaceError(RuntimeError):
    """일괄 바꾸기를 중단해야 할 때 발생한다."""


def build_pattern(query: str, use_regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    source = query if use_regex else re.escape(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(source, flags)
    except re.error as error:
        raise ReplaceError(f"정규식을 읽을 수 없습니다: {error}") from error


def expand_replacement(replacement: str, use_regex: bool) -> str:
    # 일반 찾기에서는 \\1 같은 기호를 글자 그대로 넣는다.
    return replacement if use_regex else replacement.replace("\\", "\\\\")


def scenario_files(only: list[str], exclude: list[str]) -> list[tuple[str, Path]]:
    wanted = {item.strip().upper() for item in only if item.strip()}
    unwanted = {item.strip().upper() for item in exclude if item.strip()}
    result: list[tuple[str, Path]] = []
    for path in sorted(DIALOGUE_DIR.glob("scenario_*.json")):
        head = path.open("rb").read(4096).decode("utf-8", "ignore")
        match = re.search(r'"assetKey"\s*:\s*"([^"]+)"', head)
        key = match.group(1) if match else path.stem.replace("scenario_", "")
        upper = key.upper()
        if wanted and upper not in wanted:
            continue
        if upper in unwanted:
            continue
        result.append((key, path))
    if wanted:
        found = {key.upper() for key, _ in result}
        missing = sorted(wanted - found)
        if missing:
            raise ReplaceError(f"찾지 못한 시나리오 키: {', '.join(missing)}")
    return result


def field_text(entry: dict[str, Any], field: str, seed: bool) -> str | None:
    """바꾸기 대상 문자열. 대상이 없으면 ``None``."""

    if field == "newTranslation":
        if isinstance(entry.get("newTranslation"), str):
            return entry["newTranslation"]
        if not seed:
            return None
        return str(entry.get("translation") or "")
    value = entry.get(field)
    return value if isinstance(value, str) else None


def process_document(
    key: str,
    document: dict[str, Any],
    pattern: re.Pattern[str],
    replacement: str,
    fields: list[str],
    speakers: str,
    seed: bool,
    catalog,
    sample_limit: int,
) -> dict[str, Any]:
    entries = document.get("entries") or []
    hits = 0
    changed_rows = 0
    seeded = 0
    control_breaks: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        speaker = is_speaker_entry(entry, index, entries, catalog)
        if speakers == "skip" and speaker:
            continue
        if speakers == "only" and not speaker:
            continue
        for field in fields:
            had_field = isinstance(entry.get(field), str)
            before = field_text(entry, field, seed)
            if before is None:
                continue
            after, count = pattern.subn(replacement, before)
            if not count or after == before:
                continue
            source_controls = control_tokens(entry.get("sourceText"))
            before_controls = control_tokens(before)
            after_controls = control_tokens(after)
            if before_controls == source_controls and after_controls != source_controls:
                control_breaks.append(
                    {
                        "entryId": entry.get("entryId"),
                        "field": field,
                        "before": before,
                        "after": after,
                    }
                )
            entry[field] = after
            if field == "newTranslation":
                entry["newTranslationStatus"] = "draft"
                if not had_field:
                    seeded += 1
            hits += count
            changed_rows += 1
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "entryId": entry.get("entryId"),
                        "field": field,
                        "speaker": speaker,
                        "before": before,
                        "after": after,
                    }
                )

    return {
        "assetKey": key,
        "rows": len(entries),
        "hits": hits,
        "changedRows": changed_rows,
        "seededNewTranslation": seeded,
        "controlBreaks": control_breaks,
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="시나리오 번역문에 찾아 바꾸기를 일괄 적용합니다. 기본은 미리보기입니다."
    )
    parser.add_argument("--find", required=True, help="찾을 내용")
    parser.add_argument("--replace", default="", help="바꿀 내용. 비우면 찾은 내용을 지웁니다.")
    parser.add_argument(
        "--field",
        choices=("newTranslation", "translation", "both"),
        default="newTranslation",
        help="대상 텍스트. translation은 CPK 빌드에 바로 쓰입니다. (기본: newTranslation)",
    )
    parser.add_argument("--regex", action="store_true", help="찾을 내용을 정규식으로 다룹니다.")
    parser.add_argument("--case-sensitive", action="store_true", help="대소문자를 구분합니다.")
    parser.add_argument(
        "--speakers",
        choices=("skip", "include", "only"),
        default="skip",
        help="화자명 행 처리. (기본: skip · 뷰어와 같음)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="신규 번역이 아직 없는 행은 건드리지 않습니다. (기본은 기존 번역을 옮겨 와서 바꿉니다)",
    )
    parser.add_argument("--only", action="append", default=[], metavar="ASSET", help="이 시나리오만 처리합니다.")
    parser.add_argument("--exclude", action="append", default=[], metavar="ASSET", help="이 시나리오를 뺍니다.")
    parser.add_argument(
        "--allow-control-change",
        action="store_true",
        help="바꾼 뒤 제어문자가 원문과 어긋나도 계속합니다.",
    )
    parser.add_argument("--samples", type=int, default=5, help="시나리오별로 보여 줄 예시 수 (기본 5)")
    parser.add_argument("--apply", action="store_true", help="실제로 파일을 고칩니다.")
    args = parser.parse_args(argv)

    try:
        pattern = build_pattern(args.find, args.regex, args.case_sensitive)
    except ReplaceError as error:
        print(f"[!] {error}")
        return 2
    replacement = expand_replacement(args.replace, args.regex)
    fields = ["newTranslation", "translation"] if args.field == "both" else [args.field]

    if IDEOGRAPHIC_SPACE in args.replace:
        print("[!] 바꿀 내용에 전각 공백(U+3000)이 들어 있습니다. 게임 표시가 달라질 수 있습니다.")

    catalog = load_speaker_catalog(SPEAKER_CATALOG)
    if not len(catalog):
        print("[!] 화자 카탈로그를 읽지 못했습니다. 추정 규칙만으로 화자를 가립니다.")

    try:
        targets = scenario_files(args.only, args.exclude)
    except ReplaceError as error:
        print(f"[!] {error}")
        return 2
    if not targets:
        print("[!] 처리할 시나리오가 없습니다.")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = ROOT / "work" / "text-replace" / stamp
    backup_dir = run_dir / "backup"

    results: list[dict[str, Any]] = []
    documents: dict[str, tuple[Path, dict[str, Any]]] = {}
    for key, path in targets:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        summary = process_document(
            key, document, pattern, replacement, fields,
            args.speakers, not args.no_seed, catalog, max(0, args.samples),
        )
        results.append(summary)
        if summary["hits"]:
            documents[key] = (path, document)

    total_hits = sum(item["hits"] for item in results)
    total_rows = sum(item["changedRows"] for item in results)
    total_seeded = sum(item["seededNewTranslation"] for item in results)
    touched = [item for item in results if item["hits"]]
    breaks = [item for item in results if item["controlBreaks"]]
    break_count = sum(len(item["controlBreaks"]) for item in breaks)

    print(f"찾을 내용 : {args.find}{' (정규식)' if args.regex else ''}")
    print(f"바꿀 내용 : {args.replace or '(지웁니다)'}")
    print(f"대상      : {args.field} · 화자명 행 {args.speakers} · 시나리오 {len(targets)}편")
    print()
    for item in touched:
        print(f"  {item['assetKey']:<24} {item['hits']:>5}개 · {item['changedRows']}행")
        for sample in item["samples"]:
            print(f"      - {sample['before']}")
            print(f"        → {sample['after']}")
    if not touched:
        print("  일치하는 내용이 없습니다.")
    print()
    print(f"합계 : {total_hits:,}개 일치 · {total_rows:,}행 변경 · 시나리오 {len(touched)}편")
    if total_seeded:
        print(f"       기존 번역에서 신규 번역으로 옮겨 온 행 {total_seeded:,}개")

    if break_count:
        print()
        print(f"[!] 제어문자가 원문과 어긋나는 행 {break_count}개가 생깁니다:")
        for item in breaks[:5]:
            for entry in item["controlBreaks"][:3]:
                print(f"    {entry['entryId']}")
                print(f"      전: {entry['before']}")
                print(f"      후: {entry['after']}")
        if not args.allow_control_change:
            print("    CPK 빌드가 실패할 수 있어 중단합니다. 그래도 진행하려면 --allow-control-change 를 붙이세요.")
            return 1

    if not args.apply:
        print()
        print("미리보기입니다. 실제로 고치려면 --apply 를 붙이세요.")
        return 0
    if not documents:
        print()
        print("바꿀 내용이 없어 파일을 건드리지 않았습니다.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    for key, (path, document) in documents.items():
        shutil.copy2(path, backup_dir / path.name)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    report = {
        "format": "siok.dialogue-text-replace",
        "formatVersion": 1,
        "timestamp": stamp,
        "query": {
            "find": args.find,
            "replace": args.replace,
            "regex": bool(args.regex),
            "caseSensitive": bool(args.case_sensitive),
            "field": args.field,
            "speakers": args.speakers,
            "seedNewTranslation": not args.no_seed,
        },
        "counts": {
            "scenarios": len(targets),
            "changedScenarios": len(documents),
            "hits": total_hits,
            "changedRows": total_rows,
            "seededNewTranslation": total_seeded,
            "controlBreaks": break_count,
        },
        "backupDir": str(backup_dir),
        "results": results,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"{len(documents)}편을 고쳤습니다.")
    print(f"원본 백업 : {backup_dir}")
    print(f"보고서    : {run_dir / 'report.json'}")
    if args.field in ("translation", "both"):
        print("CPK를 다시 만들려면 : python scripts/build_all_scenario_cpks.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
