#!/usr/bin/env python3
"""원본 STAGE CPK의 화자 슬롯 경계를 뷰어용 카탈로그로 만든다.

시나리오 JSON에는 번역에 필요한 문자열만 보존하고 CPK의 물리 태그는 넣지
않는다. 이 스크립트는 사용자가 소유한 게임에서 추출한 ``IDxxxxx`` 파일을
읽어 ``SP/SB/SF/SG`` 화자 슬롯의 sourceRow만 별도 JSON으로 기록한다. 원문
문장·번역문은 카탈로그에 복사하지 않으므로 저장소에 게임 대사를 추가하지
않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
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

from siok_patch.stage_script import StageScriptError, parse_stage_script


CATALOG_FORMAT = "siok.scenario-speaker-catalog"
SPEAKER_TAGS = frozenset({"SP", "SB", "SF", "SG"})
ASSET_PREFIX_RE = re.compile(r"^(STG\d{4}[A-Za-z]?)", re.IGNORECASE)
ID_RE = re.compile(r"ID\d{5}", re.IGNORECASE)


def _asset_key(data: dict[str, Any], filename: str) -> str:
    asset = data.get("asset")
    if isinstance(asset, dict) and asset.get("assetKey"):
        return str(asset["assetKey"])
    if data.get("assetKey"):
        return str(data["assetKey"])
    for entry in data.get("entries", []):
        location = entry.get("location") if isinstance(entry, dict) else None
        if isinstance(location, dict) and location.get("assetKey"):
            return str(location["assetKey"])
    return Path(filename).stem.removeprefix("scenario_")


def _asset_prefix(asset_key: str) -> str:
    match = ASSET_PREFIX_RE.match(asset_key.strip())
    return match.group(1).upper() if match else ""


def _file_id(internal_id: str) -> str:
    """``ID00003@FILE-ID00004``는 실제 CPK 파일 ID00004를 반환한다."""

    matches = ID_RE.findall(internal_id.upper())
    return matches[-1] if "@FILE-" in internal_id.upper() and matches else (matches[0] if matches else "")


def _source_dirs(source_root: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for path in source_root.iterdir():
        if not path.is_dir() or "!!!" in path.name:
            continue
        key = _asset_prefix(path.name)
        if key:
            result.setdefault(key, []).append(path)
    for paths in result.values():
        paths.sort(key=lambda item: item.name.casefold())
    return result


def _id_files(path: Path) -> dict[str, Path]:
    return {
        item.name.upper(): item
        for item in path.iterdir()
        if item.is_file() and re.fullmatch(r"ID\d{5}", item.name, re.IGNORECASE)
    }


def _descriptor_key(value: str) -> str:
    """분기 폴더·sourceArtifact의 괄호 설명을 비교하기 위한 키."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("후", "")
    return re.sub(r"[\s_\-()]+", "", text)


def _descriptor_from_name(value: str) -> str:
    match = re.search(r"\(([^()]*)\)", Path(str(value).replace("\\", "/")).name)
    return _descriptor_key(match.group(1)) if match else ""


def _source_text_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip(" \u3000")
    # 일부 이전 추출본은 CP932 왕복 과정에서 마침표만 ASCII로 남긴다.
    return text.replace(".", "。")


def _choose_source_directory(
    asset: str,
    entries: list[dict[str, Any]],
    source_dirs: dict[str, list[Path]],
    all_dirs: list[Path],
) -> Path | None:
    """분기 asset의 실제 ID 파일 폴더를 원문 일치율로 선택한다."""

    prefix = _asset_prefix(asset)
    if not prefix:
        return None
    descriptor = ""
    for entry in entries:
        location = entry.get("location") if isinstance(entry, dict) else None
        if isinstance(location, dict) and location.get("sourceArtifact"):
            descriptor = _descriptor_from_name(str(location["sourceArtifact"]))
            if descriptor:
                break
    candidates = list(source_dirs.get(prefix, []))
    if descriptor:
        candidates.extend(
            path for path in all_dirs
            if path not in candidates and _descriptor_from_name(path.name) == descriptor
        )
    if not candidates:
        return None

    samples = [
        entry for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("location"), dict)
        and _file_id(str(entry["location"].get("internalId") or ""))
    ][:160]
    scored: list[tuple[int, int, str, Path]] = []
    for path in candidates:
        files = _id_files(path)
        cache: dict[str, Any] = {}
        exact = normalized = 0
        for entry in samples:
            location = entry["location"]
            file_id = _file_id(str(location.get("internalId") or ""))
            try:
                source_row = int(location.get("sourceRow") or 0)
            except (TypeError, ValueError):
                continue
            source_path = files.get(file_id)
            if source_path is None:
                continue
            if file_id not in cache:
                try:
                    cache[file_id] = parse_stage_script(source_path.read_bytes())
                except StageScriptError:
                    cache[file_id] = None
            parsed = cache[file_id]
            if parsed is None:
                continue
            slot = next((item for item in parsed.slots if item.ordinal == source_row - 1), None)
            if slot is None:
                continue
            source = str(entry.get("sourceText") or "")
            if slot.source_text == source:
                exact += 1
            elif _source_text_key(slot.source_text) == _source_text_key(source):
                normalized += 1
        scored.append((exact, normalized, path.name.casefold(), path))
    if not scored:
        return candidates[0]
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return scored[0][3]


def build_catalog(repo_root: Path, source_root: Path, generated_at: str | None = None) -> dict[str, Any]:
    dialogue_dir = repo_root / "translations" / "dialogue"
    source_dirs = _source_dirs(source_root)
    all_dirs = sorted({path for paths in source_dirs.values() for path in paths}, key=lambda item: item.name.casefold())
    members: dict[str, list[int]] = {}
    stats: Counter[str] = Counter()

    for json_path in sorted(dialogue_dir.glob("scenario_STG*.json"), key=lambda item: item.name.casefold()):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if data.get("dialogueType") != "scenario" or not isinstance(data.get("entries"), list):
            continue
        asset = _asset_key(data, json_path.name)
        prefix = _asset_prefix(asset)
        source_directory = _choose_source_directory(asset, data["entries"], source_dirs, all_dirs)
        if source_directory is None:
            stats["missing_asset_directory"] += 1
            continue
        # 분기 CPK는 assetKey가 달라도 STG 번호가 같은 디렉터리에 보관된
        # 경우가 있으므로, 동일 prefix의 첫 디렉터리를 기본으로 사용한다.
        files = _id_files(source_directory)
        cache: dict[str, Any] = {}
        for entry in data["entries"]:
            location = entry.get("location") if isinstance(entry, dict) else None
            if not isinstance(location, dict):
                stats["missing_location"] += 1
                continue
            internal_id = str(location.get("internalId") or "")
            file_id = _file_id(internal_id)
            try:
                source_row = int(location.get("sourceRow") or 0)
            except (TypeError, ValueError):
                source_row = 0
            if not file_id or source_row < 2:
                stats["unmapped_entry"] += 1
                continue
            source_path = files.get(file_id)
            if source_path is None:
                stats["missing_id_file"] += 1
                continue
            if file_id not in cache:
                try:
                    cache[file_id] = parse_stage_script(source_path.read_bytes())
                except StageScriptError:
                    # ID00007 등 나레이션 전용/비대사 바이너리는 일반
                    # CP932 대사 슬롯이 아니므로 휴리스틱으로 되돌린다.
                    stats["unreadable_id_file"] += 1
                    cache[file_id] = None
            parsed = cache[file_id]
            if parsed is None:
                continue
            # sourceRow는 추출 슬롯 ordinal보다 1 큰 XLSX 행 번호다.
            slot = next((item for item in parsed.slots if item.ordinal == source_row - 1), None)
            if slot is None:
                stats["missing_slot"] += 1
                continue
            stats["mapped_entry"] += 1
            member_key = f"{asset}/{internal_id}".upper()
            if slot.tag in SPEAKER_TAGS:
                members.setdefault(member_key, []).append(source_row)
                stats["speaker_row"] += 1

    for rows in members.values():
        rows[:] = sorted(set(rows))
    return {
        "$schema": "./scenario-speakers.schema.json",
        "format": CATALOG_FORMAT,
        "formatVersion": 1,
        "generatedAt": generated_at or date.today().isoformat(),
        "source": {
            "kind": "owned-game CPK ID files",
            "field": "SP/SB/SF/SG slot boundaries",
            "textIncluded": False,
        },
        "counts": {
            "members": len(members),
            "speakerRows": sum(len(rows) for rows in members.values()),
            "mappedEntries": stats["mapped_entry"],
            "unreadableIdFiles": stats["unreadable_id_file"],
            "missingIdFiles": stats["missing_id_file"],
        },
        "members": {key: members[key] for key in sorted(members, key=str.casefold)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="원본 STAGE CPK에서 화자 슬롯 카탈로그 생성")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-root", type=Path, required=True, help="소유 게임에서 추출한 STAGE/IDxxxxx 폴더")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--generated-at", default=None, help="생성일(기본값: 오늘 날짜)")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    source_root = args.source_root.resolve()
    if not source_root.is_dir():
        raise SystemExit(f"STAGE 추출 폴더가 없습니다: {source_root}")
    output = (args.output or repo_root / "viewer" / "scenario-speakers.json").resolve()
    catalog = build_catalog(repo_root, source_root, args.generated_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"화자 슬롯 카탈로그 생성: {output}")
    print(f"멤버: {catalog['counts']['members']:,}개 · 화자 행: {catalog['counts']['speakerRows']:,}개")
    print(f"매핑 행: {catalog['counts']['mappedEntries']:,}개 · 읽지 못한 ID 파일: {catalog['counts']['unreadableIdFiles']:,}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
