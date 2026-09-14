#!/usr/bin/env python3
"""전체 시나리오 JSON을 원본 CPK에 적용하고 Vita3K에 복사한다.

분기 원고는 실제 게임 CPK 이름(STG0210 등)에 합쳐서 빌드한다. 원본 CPK가
없는 DLC나 번역 JSON이 없는 STAGE는 임의로 덮어쓰지 않고 보고서에 남긴다.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from siok_patch.scenario_cpk import build_scenario_cpk  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _branch_target(asset_key: str) -> str:
    match = re.match(r"^(STG\d+[A-Za-z]?)", asset_key)
    if match is None:
        return asset_key
    # 8화 후 A분기는 실제 CPK가 STG0211이다.
    if asset_key.startswith("STG0210-8화후A"):
        return "STG0211"
    return match.group(1)


def _target_for_document(document: dict[str, Any]) -> str:
    asset = document.get("asset") or {}
    key = str(asset.get("assetKey") or "").strip()
    if key.startswith("STG") and "-" in key:
        return _branch_target(key)
    return key


def _find_main_sources(archive_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in (archive_root / "0_STAGE").rglob("*.cpk"):
        result.setdefault(path.stem, path.resolve())
    return result


def _find_dlc_sources(archive_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in (archive_root / "!DLC").rglob("*.cpk"):
        result.setdefault(path.stem, path.resolve())
    return result


def _merge_documents(target: str, paths: list[Path]) -> dict[str, Any]:
    first = _load(paths[0])
    first_asset = dict(first.get("asset") or {})
    is_dlc = target.startswith("DLC")
    first_asset.update(
        {
            "assetKey": target,
            "fileName": f"{target}.cpk",
            "target": (
                f"{target}/{target}/{target}.cpk"
                if is_dlc
                else f"DATA/STAGE/{target}.cpk"
            ),
        }
    )
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for path in paths:
        document = _load(path)
        for entry in document.get("entries", []):
            location = entry.get("location") or {}
            key = (
                str(location.get("internalId") or ""),
                int(location.get("sourceRow") or 0),
                str(entry.get("sourceText") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    entries.sort(
        key=lambda item: (
            str((item.get("location") or {}).get("internalId") or ""),
            int((item.get("location") or {}).get("sourceRow") or 0),
        )
    )
    merged = dict(first)
    merged["asset"] = first_asset
    merged["entries"] = entries
    merged["counts"] = dict(first.get("counts") or {})
    merged["counts"]["entries"] = len(entries)
    merged["counts"]["uniqueSourceTexts"] = len(
        {str(item.get("sourceText") or "") for item in entries}
    )
    condition_path = ROOT / "translations" / "conditions" / f"scenario_{target}.json"
    conditions: list[dict[str, Any]] = []
    if condition_path.is_file():
        condition_document = _load(condition_path)
        if str(condition_document.get("format") or "") != "siok.stage-conditions":
            raise ValueError(f"조건 JSON 형식이 올바르지 않습니다: {condition_path}")
        for entry in condition_document.get("entries", []):
            if not isinstance(entry, dict):
                raise ValueError(f"조건 JSON entries에 객체가 아닌 값이 있습니다: {condition_path}")
            conditions.append(entry)
        conditions.sort(key=lambda item: int(item.get("sourceId") or 0))
    if conditions:
        merged["conditions"] = conditions
        source_info = condition_document.get("source") or {}
        merged["conditionMemberId"] = str(source_info.get("memberId") or "ID00001")
        merged["counts"]["conditions"] = len(conditions)
    merged["buildInputs"] = [str(path.relative_to(ROOT)) for path in paths]
    if condition_path.is_file():
        merged["buildInputs"].append(str(condition_path.relative_to(ROOT)))
    return merged


def _resolve_vita_root(
    explicit: str | None, config: dict[str, Any], no_copy: bool
) -> tuple[Path | None, str]:
    """복사할 Vita3K ux0 위치를 정한다.

    우선순위는 ``--vita-root`` → ``project.local.json``의 ``vita3kRoot`` →
    현재 사용자의 ``%APPDATA%\\Vita3K\\Vita3K\\ux0``다. 어느 쪽도 실제로
    존재하지 않으면 복사를 건너뛴다. 없는 경로를 새로 만들면 엉뚱한 위치에
    파일이 쌓이므로 절대 만들지 않는다.
    """

    if no_copy:
        return None, "--no-copy 지정으로 복사하지 않습니다."

    candidates: list[tuple[Path, str]] = []
    if explicit:
        candidates.append((Path(explicit).expanduser(), "--vita-root"))
    configured = str(config.get("vita3kRoot") or "").strip()
    if configured:
        candidates.append((Path(configured).expanduser(), "project.local.json의 vita3kRoot"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            (Path(appdata) / "Vita3K" / "Vita3K" / "ux0", "현재 사용자 APPDATA 자동 탐지")
        )

    for candidate, origin in candidates:
        resolved = candidate.resolve()
        if not resolved.is_dir():
            if origin == "현재 사용자 APPDATA 자동 탐지":
                continue
            return None, f"{origin} 경로가 없습니다: {resolved}"
        if not (resolved / "app").is_dir() and not (resolved / "addcont").is_dir():
            return None, f"{origin} 경로가 Vita3K ux0 폴더가 아닙니다: {resolved}"
        return resolved, f"{origin}: {resolved}"

    return None, "Vita3K ux0 폴더를 찾지 못해 복사를 건너뜁니다."


def _vita_copy_ready(vita_root: Path, target: str) -> str | None:
    """복사 대상 게임이 실제로 설치돼 있는지 본다. 문제가 있으면 사유를 돌려준다."""

    if target.startswith("DLC"):
        if not (vita_root / "addcont").is_dir():
            return "Vita3K에 addcont 폴더가 없습니다"
        return None
    if not (vita_root / "app" / "PCSG00264").is_dir():
        return "Vita3K에 PCSG00264가 설치돼 있지 않습니다"
    return None


def _vita_target(vita_root: Path, target: str) -> Path:
    if target.startswith("DLC"):
        number = target.removeprefix("DLC")
        return vita_root / "addcont" / f"SRWZ3OF1DLC0{number}" / "DLC" / f"{target}.cpk"
    return vita_root / "app" / "PCSG00264" / "DATA" / "STAGE" / f"{target}.cpk"


def _copy_with_backup(source: Path, destination: Path, backup_root: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.is_file():
        backup = backup_root / destination.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
    return {
        "destination": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": __import__("hashlib").sha256(destination.read_bytes()).hexdigest(),
        "backup": str(backup) if backup else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="번역 JSON을 원본 시나리오 CPK에 일괄 적용하고 Vita3K에 복사합니다."
    )
    parser.add_argument(
        "--only",
        action="append",
        dest="only",
        metavar="ASSET",
        help="지정한 CPK 키만 처리합니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--vita-root",
        dest="vita_root",
        default=None,
        metavar="PATH",
        help="Vita3K ux0 폴더. 생략하면 project.local.json의 vita3kRoot, 그다음 현재 사용자 APPDATA를 찾습니다.",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="빌드만 하고 Vita3K로 복사하지 않습니다.",
    )
    args = parser.parse_args(argv)
    config = _load(ROOT / "private" / "project.local.json")
    configured_archive = Path(str(config["archiveRoot"])).expanduser().resolve()
    archive_candidates = [
        configured_archive,
        ROOT.parent / "PCSG00264",
        Path(r"D:\Z\psvita\PCSG00264"),
    ]
    archive_root = next(
        (
            candidate.resolve()
            for candidate in archive_candidates
            if (candidate / "0_STAGE").is_dir() and (candidate / "!DLC").is_dir()
        ),
        configured_archive,
    )
    vita_root, vita_note = _resolve_vita_root(args.vita_root, config, args.no_copy)
    print(f"Vita3K 복사 · {vita_note}", flush=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_dir = ROOT / "work" / "all-scenario-builds" / timestamp
    report_dir.mkdir(parents=True, exist_ok=False)
    backup_root = ROOT / "work" / "vita3k-backups" / timestamp

    documents: dict[str, list[Path]] = defaultdict(list)
    for path in sorted((ROOT / "translations" / "dialogue").glob("scenario_*.json")):
        document = _load(path)
        target = _target_for_document(document)
        if target:
            documents[target].append(path)
    if args.only:
        requested = {str(item).strip() for item in args.only if str(item).strip()}
        documents = defaultdict(
            list,
            {
                target: paths
                for target, paths in documents.items()
                if target in requested
            },
        )

    main_sources = _find_main_sources(archive_root)
    dlc_sources = _find_dlc_sources(archive_root)
    results: list[dict[str, Any]] = []
    built = 0
    copied = 0
    failed = 0
    for index, target in enumerate(sorted(documents), start=1):
        paths = documents[target]
        source_map = dlc_sources if target.startswith("DLC") else main_sources
        source = source_map.get(target)
        row: dict[str, Any] = {
            "target": target,
            "jsonFiles": [str(path.relative_to(ROOT)) for path in paths],
            "sourceCpk": str(source) if source else None,
        }
        print(f"[{index}/{len(documents)}] {target}", flush=True)
        if source is None:
            row.update({"status": "missing-source-cpk"})
            results.append(row)
            print("  원본 CPK 없음", flush=True)
            continue
        try:
            merged = _merge_documents(target, paths)
            build = build_scenario_cpk(
                merged,
                repository_root=ROOT,
                source_cpk=source,
            )
            output = Path(str(build["outputCpk"]))
            row.update(
                {
                    "status": "built",
                    "outputCpk": str(output),
                    "outputSha256": build["outputSha256"],
                    "outputBytes": build["outputBytes"],
                    "modifiedMembers": len(build.get("modified", [])),
                    "collisionTargetRemaps": build.get("collisionTargetRemaps", 0),
                    "fixedSlotEntries": build.get("fixedSlotEntries", 0),
                    "fixedSlotFallbackEntries": build.get("fixedSlotFallbackEntries", 0),
                    "unmatchedFixedSlotEntries": build.get("unmatchedFixedSlotEntries", 0),
                    "skippedFixedSlotEntries": build.get("skippedFixedSlotEntries", 0),
                    "conditionEntries": build.get("conditionEntries", 0),
                    "patchedConditionEntries": build.get("patchedConditionEntries", 0),
                    "unmatchedConditionEntries": build.get("unmatchedConditionEntries", 0),
                    "operationWReplaceFallback": build.get("operationWReplaceFallback", False),
                }
            )
            built += 1
            if vita_root is None:
                row["vita3kSkipped"] = vita_note
            else:
                blocked = _vita_copy_ready(vita_root, target)
                if blocked:
                    row["vita3kSkipped"] = blocked
                else:
                    destination = _vita_target(vita_root, target)
                    row["vita3k"] = _copy_with_backup(
                        output, destination, backup_root / target
                    )
                    copied += 1
            done = "빌드·복사 완료" if row.get("vita3k") else "빌드 완료"
            print(
                f"  {done} ({build['outputBytes']} bytes, "
                f"고정 슬롯 {row['fixedSlotEntries']}개, "
                f"컴파일 폴백 {row['fixedSlotFallbackEntries']}개, "
                f"미대응 {row['unmatchedFixedSlotEntries']}개)",
                f" 조건 {row['patchedConditionEntries']}/{row['conditionEntries']}",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001 - 전체 배치 보고서에 기록하고 계속한다.
            row.update({"status": "failed", "error": f"{type(error).__name__}: {error}"})
            failed += 1
            print(f"  실패: {error}", flush=True)
        results.append(row)

    report = {
        "format": "siok.all-scenario-cpk-build",
        "formatVersion": 1,
        "timestamp": timestamp,
        "requestedTargets": sorted(args.only or []),
        "policy": {
            "requiresUserOwnedGame": True,
            "gameBinaryIncluded": False,
            "fixedSlotId00007": "patched-with-replacementText-fallback",
            "stageConditions": "OPERATE_TBL.str_tbl in ID00001",
        },
        "counts": {
            "jsonTargetGroups": len(documents),
            "built": built,
            "copiedToVita3K": copied,
            "missingSourceCpk": sum(item["status"] == "missing-source-cpk" for item in results),
            "failed": failed,
        },
        "vita3kRoot": str(vita_root) if vita_root else None,
        "vita3kNote": vita_note,
        "backupRoot": str(backup_root),
        "results": results,
    }
    report_path = report_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), **report["counts"]}, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
