#!/usr/bin/env python3
"""검수를 마친 ``newTranslation``을 확정본 ``translation``으로 올린다.

뷰어 편집창과 재번역 작업은 ``newTranslation``에 쌓이지만 CPK 빌드는
``translation``만 읽는다. 재번역본을 실제 패치에 반영하려면 이 승격이
필요하다.

올리기 전에 CPK 빌드가 실제로 쓰는 검사를 그대로 돌린다.

* 원문과 제어문자(``$n``, ``㊦`` 등) 순서·개수가 같은가
* wReplace 문자표를 거쳐 CP932로 인코딩되는가
* ``《 》`` 용어 괄호가 짝을 이루는가

하나라도 걸리는 행이 있으면 기본적으로 중단한다. 문제 행만 빼고 나머지를
올리려면 ``--skip-invalid``를 쓴다.

    python scripts/promote_new_translations.py                # 미리보기
    python scripts/promote_new_translations.py --apply        # 실제 승격
"""

from __future__ import annotations

import argparse
import hashlib
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

from siok_patch.dialogue_speakers import control_tokens  # noqa: E402
from siok_patch.scenario_cpk import (  # noqa: E402
    ScenarioCpkBuildError,
    _encode_translation,
    _load_wreplace,
    _resolve_wreplace,
)

DIALOGUE_DIR = ROOT / "translations" / "dialogue"
TERM_OPEN = "《"
TERM_CLOSE = "》"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_mapping() -> dict[str, str]:
    config_path = ROOT / "private" / "project.local.json"
    config: dict[str, Any] = {}
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    table = _resolve_wreplace(ROOT, config, None)
    return _load_wreplace(table)


def check_entry(entry: dict[str, Any], mapping: dict[str, str]) -> str | None:
    """승격했을 때 CPK 빌드가 막힐 이유를 돌려준다. 문제 없으면 ``None``."""

    candidate = entry.get("newTranslation")
    if not isinstance(candidate, str):
        return None
    source = entry.get("sourceText")
    if control_tokens(source) != control_tokens(candidate):
        return (
            f"제어문자 불일치 (원문 {' '.join(control_tokens(source)) or '없음'}"
            f" → 신규 {' '.join(control_tokens(candidate)) or '없음'})"
        )
    if candidate.count(TERM_OPEN) != candidate.count(TERM_CLOSE):
        return "《 》 용어 괄호가 짝이 맞지 않음"
    try:
        _encode_translation(candidate, mapping)
    except ScenarioCpkBuildError as error:
        detail = str(error).splitlines()[0]
        return f"인코딩 불가 · {detail[:110]}"
    return None


def scenario_key(path: Path) -> str:
    head = path.open("rb").read(4096).decode("utf-8", "ignore")
    match = re.search(r'"assetKey"\s*:\s*"([^"]+)"', head)
    return match.group(1) if match else path.stem.replace("scenario_", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="newTranslation을 translation으로 승격합니다. 기본은 미리보기입니다."
    )
    parser.add_argument("--only", action="append", default=[], metavar="ASSET")
    parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="검사에 걸린 행은 올리지 않고 나머지만 승격합니다.",
    )
    parser.add_argument(
        "--keep-new",
        action="store_true",
        help="승격 뒤에도 newTranslation을 남깁니다. (기본은 같은 값이 되므로 지웁니다)",
    )
    parser.add_argument("--status", default="reviewed", help="승격된 행의 translationStatus (기본 reviewed)")
    parser.add_argument("--apply", action="store_true", help="실제로 파일을 고칩니다.")
    args = parser.parse_args(argv)

    wanted = {item.strip().upper() for item in args.only if item.strip()}
    try:
        mapping = load_mapping()
    except ScenarioCpkBuildError as error:
        print(f"[!] 문자표를 찾지 못했습니다: {error}")
        return 2
    print(f"wReplace 문자표 항목 {len(mapping):,}개로 검사합니다.")
    print()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = ROOT / "work" / "promote-translations" / stamp
    backup_dir = run_dir / "backup"

    results: list[dict[str, Any]] = []
    documents: dict[Path, dict[str, Any]] = {}
    total_candidates = 0
    total_promoted = 0
    problems: list[dict[str, Any]] = []

    for path in sorted(DIALOGUE_DIR.glob("scenario_*.json")):
        key = scenario_key(path)
        if wanted and key.upper() not in wanted:
            continue
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = document.get("entries") or []
        candidates = 0
        promoted = 0
        blocked: list[dict[str, Any]] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = entry.get("newTranslation")
            if not isinstance(candidate, str):
                continue
            if candidate == str(entry.get("translation") or ""):
                continue
            candidates += 1
            reason = check_entry(entry, mapping)
            if reason:
                blocked.append(
                    {
                        "entryId": entry.get("entryId"),
                        "reason": reason,
                        "sourceText": entry.get("sourceText"),
                        "translation": entry.get("translation"),
                        "newTranslation": candidate,
                    }
                )
                continue
            entry["translation"] = candidate
            entry["translationStatus"] = args.status
            # 다른 도구들이 이 해시로 번역 갱신 여부를 검사한다. 지우지 말고 다시 계산한다.
            entry["translationTextSha256"] = sha256_text(candidate)
            if not args.keep_new:
                entry.pop("newTranslation", None)
                entry.pop("newTranslationStatus", None)
                entry.pop("newTranslationTextSha256", None)
            promoted += 1

        total_candidates += candidates
        total_promoted += promoted
        problems.extend(blocked)
        results.append(
            {
                "assetKey": key,
                "file": path.name,
                "candidates": candidates,
                "promoted": promoted,
                "blocked": blocked,
            }
        )
        if promoted:
            documents[path] = document

    touched = [item for item in results if item["candidates"]]
    print(f"승격 대상 : {total_candidates:,}행 · 시나리오 {len(touched)}편")
    print(f"검사 통과 : {total_promoted:,}행")
    print(f"검사 실패 : {len(problems):,}행")

    if problems:
        print()
        print("[!] 승격하면 CPK 빌드가 막히는 행:")
        by_reason: dict[str, int] = {}
        for item in problems:
            head = item["reason"].split("·")[0].strip()
            by_reason[head] = by_reason.get(head, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda pair: -pair[1]):
            print(f"    {reason}: {count}행")
        print()
        for item in problems[:8]:
            print(f"    {item['entryId']}")
            print(f"      원문 : {item['sourceText']}")
            print(f"      기존 : {item['translation']}")
            print(f"      신규 : {item['newTranslation']}")
            print(f"      사유 : {item['reason']}")
        if len(problems) > 8:
            print(f"    … 외 {len(problems) - 8}행")
        if not args.skip_invalid:
            print()
            print("    중단합니다. 문제 행을 빼고 진행하려면 --skip-invalid 를 붙이세요.")
            return 1

    if not args.apply:
        print()
        print("미리보기입니다. 실제로 승격하려면 --apply 를 붙이세요.")
        return 0
    if not documents:
        print()
        print("승격할 내용이 없어 파일을 건드리지 않았습니다.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    for path, document in documents.items():
        shutil.copy2(path, backup_dir / path.name)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    report = {
        "format": "siok.promote-new-translations",
        "formatVersion": 1,
        "timestamp": stamp,
        "options": {
            "only": sorted(wanted),
            "skipInvalid": bool(args.skip_invalid),
            "keepNew": bool(args.keep_new),
            "status": args.status,
        },
        "counts": {
            "scenarios": len(results),
            "changedScenarios": len(documents),
            "candidates": total_candidates,
            "promoted": total_promoted,
            "blocked": len(problems),
        },
        "backupDir": str(backup_dir),
        "results": results,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"{len(documents)}편 · {total_promoted:,}행을 확정본으로 올렸습니다.")
    print(f"원본 백업 : {backup_dir}")
    print(f"보고서    : {run_dir / 'report.json'}")
    print("다음 단계 : python scripts/build_all_scenario_cpks.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
